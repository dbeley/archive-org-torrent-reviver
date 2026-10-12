"""Tests for the downloader: resume, verification, containment and failure reporting.

Everything runs against a real HTTP server on localhost that implements byte
ranges the way archive.org does, so the resume path is exercised for real rather
than mocked away.
"""

from __future__ import annotations

import hashlib
import http.server
import tempfile
import threading
import time
import unittest
from pathlib import Path

from ia_reviver.archive import ArchiveOrg
from ia_reviver.downloader import PART_SUFFIX, Downloader, RateLimiter
from ia_reviver.planner import FETCH, TOUCH, PlannedFile, TorrentPlan
from ia_reviver.progress import Console
from ia_reviver.qbittorrent import Torrent
from ia_reviver.util import md5_file
from tests.fakes import qbt_torrent

IDENT = "fixture_item"
PAYLOADS = {
    "alpha.txt": b"A" * 5000,
    "beta.txt": bytes(range(256)) * 12,
}


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    """Serves item files, honouring ``Range`` when the server is told to."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        server = self.server
        server.seen.append((self.path, self.headers.get("Range")))

        failure = server.fail_paths.get(self.path)
        if failure:
            self.send_response(failure)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        payload = server.content.get(self.path)
        if payload is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        start = 0
        range_header = self.headers.get("Range")
        if range_header and range_header.startswith("bytes="):
            start = int(range_header[len("bytes=") :].split("-")[0] or 0)

        if start and server.honour_range:
            body = payload[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(payload) - 1}/{len(payload)}")
        else:
            # Range not requested, or this mirror ignores it: full body, 200.
            body = payload
            self.send_response(200)

        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes" if server.honour_range else "none")
        self.end_headers()
        self.wfile.write(body)


class _ItemServer:
    """Background HTTP server pretending to be ``archive.org/download``."""

    def __init__(self, content, *, honour_range=True, fail_paths=None):
        self.honour_range = honour_range
        self.fail_paths = dict(fail_paths or {})
        self.seen: list[tuple[str, str | None]] = []
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
        self._server.content = content  # type: ignore[attr-defined]
        self._server.honour_range = honour_range  # type: ignore[attr-defined]
        self._server.fail_paths = self.fail_paths  # type: ignore[attr-defined]
        self._server.seen = self.seen  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> _ItemServer:
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._server.shutdown()
        self._server.server_close()


class _DownloaderBase(unittest.TestCase):
    """Shared setup. No test methods here, so subclasses only run their own."""

    honour_range = True
    fail_paths: dict[str, int] = {}

    def setUp(self):
        content = {f"/download/{IDENT}/{name}": body for name, body in PAYLOADS.items()}
        self.server = _ItemServer(
            content, honour_range=self.honour_range, fail_paths=self.fail_paths
        )
        self.server.__enter__()
        self.addCleanup(self.server.__exit__)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.archive = ArchiveOrg(self.server.url, retries=1, timeout=10)
        self.quiet = Console(quiet=True)

    def part_path(self, name: str) -> Path:
        return self.root / IDENT / (name + PART_SUFFIX)

    def target(self, name: str) -> Path:
        return self.root / IDENT / name

    def md5(self, name: str) -> str:
        return hashlib.md5(PAYLOADS[name]).hexdigest()

    def make_plan(self, specs, *, identifier: str = IDENT, save_path=None) -> TorrentPlan:
        """``specs``: list of ``(item_name, action, md5 or None)``."""
        save_path = Path(save_path or self.root)
        files = []
        for name, action, digest in specs:
            payload = PAYLOADS.get(name, b"")
            files.append(
                PlannedFile(
                    rel=f"{identifier}/{name}",
                    dest=save_path / identifier / name,
                    size=len(payload),
                    item_name=name,
                    item_path=name.encode(),
                    md5=digest,
                    action=action,
                )
            )
        return TorrentPlan(
            torrent=Torrent(qbt_torrent(save_path=str(save_path))),
            identifier=identifier,
            root=identifier,
            files=files,
        )


class DownloaderTest(_DownloaderBase):
    # ------------------------------------------------------------------ happy

    def test_downloads_all_files_and_verifies_md5(self):
        plan = self.make_plan([(name, FETCH, self.md5(name)) for name in PAYLOADS])
        results = Downloader(self.archive, jobs=2, console=self.quiet).fetch_plan(plan)
        self.assertEqual(sorted(r.status for r in results), ["ok", "ok"])
        for name, payload in PAYLOADS.items():
            self.assertEqual(self.target(name).read_bytes(), payload)
            self.assertFalse(self.part_path(name).exists())
            self.assertEqual(md5_file(self.target(name)), self.md5(name))

    def test_empty_file_is_created(self):
        plan = self.make_plan([("empty.txt", TOUCH, None)])
        results = Downloader(self.archive, console=self.quiet).fetch_plan(plan)
        self.assertEqual(results[0].status, "created")
        self.assertEqual(self.target("empty.txt").read_bytes(), b"")

    def test_does_not_write_outside_the_save_path(self):
        plan = self.make_plan([("alpha.txt", FETCH, self.md5("alpha.txt"))])
        escaped = Path(tempfile.gettempdir()) / "ia-reviver-escaped.txt"
        self.addCleanup(lambda: escaped.unlink(missing_ok=True))
        plan.files[0].dest = escaped
        results = Downloader(self.archive, console=self.quiet).fetch_plan(plan)
        self.assertEqual(results[0].status, "failed")
        self.assertIn("save_path", results[0].error)
        self.assertFalse(escaped.exists())

    # ----------------------------------------------------------------- resume

    def test_resumes_a_partial_download_with_a_range_request(self):
        plan = self.make_plan([("alpha.txt", FETCH, self.md5("alpha.txt"))])
        self.part_path("alpha.txt").parent.mkdir(parents=True, exist_ok=True)
        self.part_path("alpha.txt").write_bytes(PAYLOADS["alpha.txt"][:1000])

        results = Downloader(self.archive, console=self.quiet).fetch_plan(plan)
        self.assertEqual(results[0].status, "ok")
        self.assertEqual(results[0].resumed_from, 1000)
        self.assertEqual(self.target("alpha.txt").read_bytes(), PAYLOADS["alpha.txt"])
        self.assertIn("bytes=1000-", [rng for _path, rng in self.server.seen])

    def test_partial_file_larger_than_expected_is_restarted(self):
        plan = self.make_plan([("alpha.txt", FETCH, self.md5("alpha.txt"))])
        self.part_path("alpha.txt").parent.mkdir(parents=True, exist_ok=True)
        self.part_path("alpha.txt").write_bytes(b"Z" * (len(PAYLOADS["alpha.txt"]) + 500))

        results = Downloader(self.archive, console=self.quiet).fetch_plan(plan)
        self.assertEqual(results[0].status, "ok")
        self.assertEqual(self.target("alpha.txt").read_bytes(), PAYLOADS["alpha.txt"])

    # ----------------------------------------------------------- verification

    def test_md5_mismatch_fails_and_removes_the_bad_file(self):
        plan = self.make_plan([("alpha.txt", FETCH, "0" * 32)])
        results = Downloader(self.archive, console=self.quiet).fetch_plan(plan)
        self.assertEqual(results[0].status, "failed")
        self.assertIn("md5 mismatch", results[0].error)
        self.assertFalse(self.target("alpha.txt").exists())
        # The poisoned part file must be gone so a re-run starts clean.
        self.assertFalse(self.part_path("alpha.txt").exists())

    def test_truncated_response_is_reported_as_failed(self):
        plan = self.make_plan([("alpha.txt", FETCH, self.md5("alpha.txt"))])
        plan.files[0].size = len(PAYLOADS["alpha.txt"]) + 1  # one byte more than exists
        results = Downloader(self.archive, console=self.quiet, retries=1).fetch_plan(plan)
        self.assertEqual(results[0].status, "failed")
        self.assertIn("bytes", results[0].error)

    def test_md5_verification_can_be_disabled(self):
        plan = self.make_plan([("alpha.txt", FETCH, "0" * 32)])
        results = Downloader(self.archive, verify=False, console=self.quiet).fetch_plan(plan)
        self.assertEqual(results[0].status, "ok")
        self.assertEqual(md5_file(self.target("alpha.txt")), self.md5("alpha.txt"))

    # ---------------------------------------------------------------- errors

    def test_http_404_is_reported_without_retrying(self):
        plan = self.make_plan([("ghost.txt", FETCH, None)])
        plan.files[0].size = 10
        results = Downloader(self.archive, console=self.quiet, retries=3).fetch_plan(plan)
        self.assertEqual(results[0].status, "failed")
        self.assertIn("404", results[0].error)

    def test_server_error_is_retried_then_fails(self):
        plan = self.make_plan([("alpha.txt", FETCH, self.md5("alpha.txt"))])
        self.server.fail_paths["/download/fixture_item/alpha.txt"] = 503
        results = Downloader(self.archive, console=self.quiet, retries=2).fetch_plan(plan)
        self.assertEqual(results[0].status, "failed")

    # ------------------------------------------------------------ rate limit

    def test_rate_limiter_disabled_is_a_no_op(self):
        limiter = RateLimiter(None)
        started = time.monotonic()
        for _ in range(2000):
            limiter.consume(1024 * 1024)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_rate_limiter_throttles(self):
        limiter = RateLimiter(50_000)  # 50 KB/s
        started = time.monotonic()
        limiter.consume(25_000)  # a quarter second's worth
        self.assertGreaterEqual(time.monotonic() - started, 0.2)


class TestMirrorThatIgnoresRange(_DownloaderBase):
    """archive.org honours Range, but the tool must stay correct if a mirror does not."""

    honour_range = False

    def test_full_body_restart_when_range_is_ignored(self):
        plan = self.make_plan([("alpha.txt", FETCH, self.md5("alpha.txt"))])
        self.part_path("alpha.txt").parent.mkdir(parents=True, exist_ok=True)
        self.part_path("alpha.txt").write_bytes(b"stale-partial-data")

        results = Downloader(self.archive, console=self.quiet).fetch_plan(plan)
        self.assertEqual(results[0].status, "ok")
        self.assertEqual(self.target("alpha.txt").read_bytes(), PAYLOADS["alpha.txt"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
