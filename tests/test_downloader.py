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
from ia_reviver.downloader import PART_SUFFIX, Downloader, RateLimiter, _announced_size
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

        if self.path in server.truncate_paths:
            # Announce the full length, deliver half of it, then hang up. This is
            # a transfer cut short mid-stream, which is transient and must be
            # retried — unlike a source that genuinely serves a different size.
            self.wfile.write(body[: max(1, len(body) // 2)])
            self.close_connection = True
            return
        self.wfile.write(body)


class _ItemServer:
    """Background HTTP server pretending to be ``archive.org/download``."""

    def __init__(self, content, *, honour_range=True, fail_paths=None, truncate_paths=None):
        self.honour_range = honour_range
        self.fail_paths = dict(fail_paths or {})
        self.truncate_paths: set[str] = set(truncate_paths or ())
        self.seen: list[tuple[str, str | None]] = []
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
        self._server.content = content  # type: ignore[attr-defined]
        self._server.honour_range = honour_range  # type: ignore[attr-defined]
        self._server.fail_paths = self.fail_paths  # type: ignore[attr-defined]
        self._server.truncate_paths = self.truncate_paths  # type: ignore[attr-defined]
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


class AnnouncedSizeTest(unittest.TestCase):
    """``_announced_size`` is what lets a doomed transfer fail before it starts."""

    class _Response:
        def __init__(self, headers):
            self.headers = headers

    def size(self, headers, offset=0):
        return _announced_size(self._Response(headers), offset)

    def test_a_ranged_response_reports_the_total_not_the_chunk(self):
        headers = {"Content-Range": "bytes 100-3983/3984", "Content-Length": "3884"}
        self.assertEqual(self.size(headers, offset=100), 3984)

    def test_a_plain_response_reports_its_length(self):
        self.assertEqual(self.size({"Content-Length": "3956"}), 3956)

    def test_an_encoded_body_cannot_be_trusted_to_match_the_file_size(self):
        headers = {"Content-Length": "900", "Content-Encoding": "gzip"}
        self.assertIsNone(self.size(headers))

    def test_a_response_that_states_no_length_says_so_rather_than_zero(self):
        """None must mean "unknown"; treating it as 0 would flag every file."""
        self.assertIsNone(self.size({}))
        self.assertIsNone(self.size({"Content-Length": ""}))
        self.assertIsNone(self.size({"Content-Range": "bytes 0-9/*"}))


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

    def test_truncated_response_is_retried_and_reported_as_failed(self):
        """A transfer cut short is transient: the fix is to try again."""
        path = f"/download/{IDENT}/alpha.txt"
        self.server.truncate_paths.add(path)
        plan = self.make_plan([("alpha.txt", FETCH, self.md5("alpha.txt"))])

        results = Downloader(self.archive, console=self.quiet, retries=3).fetch_plan(plan)

        self.assertEqual(results[0].status, "failed")
        self.assertIn("gave up after 3 attempts", results[0].error)
        # It really did retry rather than deciding after one attempt.
        self.assertEqual(len([p for p, _ in self.server.seen if p == path]), 3)

    def test_size_the_source_no_longer_serves_is_permanent_not_retried(self):
        """archive.org regenerates its metadata files, so a torrent made earlier
        can name a size the item no longer serves. No retry can conjure bytes
        that no longer exist, and the run must say so instead of grinding."""
        path = f"/download/{IDENT}/alpha.txt"
        plan = self.make_plan([("alpha.txt", FETCH, self.md5("alpha.txt"))])
        plan.files[0].size = len(PAYLOADS["alpha.txt"]) + 28  # the item grew

        results = Downloader(self.archive, console=self.quiet, retries=4).fetch_plan(plan)

        self.assertEqual(results[0].status, "failed")
        self.assertIn("changed after the .torrent was created", results[0].error)
        self.assertIn(f"{len(PAYLOADS['alpha.txt'])} B", results[0].error)
        self.assertIn(f"{len(PAYLOADS['alpha.txt']) + 28} B", results[0].error)
        # One attempt only, and nothing left behind to poison a later recheck.
        self.assertEqual(len([p for p, _ in self.server.seen if p == path]), 1)
        self.assertFalse(self.part_path("alpha.txt").exists())
        self.assertFalse(self.target("alpha.txt").exists())

    def test_the_changed_size_is_detected_from_the_headers_alone(self):
        """Content-Length settles it, so the body is never transferred."""
        plan = self.make_plan([("alpha.txt", FETCH, self.md5("alpha.txt"))])
        plan.files[0].size = len(PAYLOADS["alpha.txt"]) + 28
        results = Downloader(self.archive, console=self.quiet).fetch_plan(plan)
        self.assertEqual(results[0].status, "failed")
        # Nothing was written: were the body read, the message would cite the
        # bytes actually received rather than the announced transfer size.
        self.assertFalse(self.part_path("alpha.txt").exists())
        self.assertIn(f"{len(PAYLOADS['alpha.txt'])} B on archive.org", results[0].error)

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
