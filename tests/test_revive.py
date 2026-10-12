"""End-to-end orchestration test: scan → plan → fetch → recheck, with no network.

A fake qBittorrent WebUI and a fake archive.org are wired together, so the whole
revive loop runs for real (HTTP, files on disk, recheck) without touching the
internet or the user's torrent client.
"""

from __future__ import annotations

import http.server
import json
import tempfile
import threading
import unittest
from pathlib import Path

from ia_reviver.archive import ArchiveOrg
from ia_reviver.bencode import parse_torrent
from ia_reviver.downloader import Downloader
from ia_reviver.planner import build_plan
from ia_reviver.progress import Console
from ia_reviver.qbittorrent import QbitClient
from ia_reviver.revive import (
    LockError,
    collect_candidates,
    revive_plan,
    torrent_lock,
    write_state,
)
from ia_reviver.util import md5_file
from tests.fakes import FakeQbitServer, make_item, qbt_file, qbt_torrent

IDENT = "fixture_item"
PAYLOADS = {
    "alpha.txt": b"A" * 5000,
    "beta.txt": bytes(range(200)) * 12,
    "empty.txt": b"",
}
TOTAL = sum(len(p) for p in PAYLOADS.values())


class _ArchiveHandler(http.server.BaseHTTPRequestHandler):
    """Serves /metadata/<id> and /download/<id>/<file>, with Range support."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        server = self.server
        path = self.path.split("?")[0]
        seen = server.seen
        seen.append(path)

        if path == f"/metadata/{IDENT}":
            body = json.dumps(server.metadata).encode()
        elif path == f"/download/{IDENT}/{IDENT}_archive.torrent":
            body = server.torrent_bytes
        elif path.startswith(f"/download/{IDENT}/"):
            name = path[len(f"/download/{IDENT}/") :]
            payload = server.content.get(name)
            if payload is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            start = 0
            range_header = self.headers.get("Range")
            if range_header and range_header.startswith("bytes="):
                start = int(range_header[len("bytes=") :].split("-")[0] or 0)
            if start:
                body = payload[start:]
                self.send_response(206)
                self.send_header(
                    "Content-Range", f"bytes {start}-{len(payload) - 1}/{len(payload)}"
                )
            else:
                body = payload
                self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _FakeArchiveServer:
    def __init__(self, torrent_bytes: bytes, content: dict[str, bytes], metadata: dict):
        self.torrent_bytes = torrent_bytes
        self.content = content
        self.metadata = metadata
        self.seen: list[str] = []
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ArchiveHandler)
        for attr, value in (
            ("torrent_bytes", torrent_bytes),
            ("content", content),
            ("metadata", metadata),
            ("seen", self.seen),
        ):
            setattr(self._server, attr, value)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        self._server.shutdown()
        self._server.server_close()


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        raw, manifest = make_item(PAYLOADS, identifier=IDENT)
        self.torrent = parse_torrent(raw)
        self.content = dict(PAYLOADS)
        metadata = {
            "metadata": {"identifier": IDENT, "title": "Fixture"},
            "files": [
                {"name": f"{IDENT}_archive.torrent", "size": len(raw)},
                *[
                    {"name": name, "size": len(body), "md5": manifest[name].md5}
                    for name, body in PAYLOADS.items()
                ],
            ],
        }
        self.archive_server = _FakeArchiveServer(raw, self.content, metadata)
        self.archive_server.__enter__()
        self.addCleanup(self.archive_server.__exit__)

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state_dir = self.root / "state"

        files = {
            self.torrent.info_hash: [
                qbt_file(f"{IDENT}/{name}", len(body)) for name, body in PAYLOADS.items()
            ]
        }
        self.qbt = FakeQbitServer(
            torrents=[
                qbt_torrent(
                    torrent_hash=self.torrent.info_hash,
                    name=IDENT,
                    state="stalledDL",
                    progress=0.0,
                    size=TOTAL,
                    amount_left=TOTAL,
                    save_path=str(self.root / "downloads"),
                    comment=f"hosted at https://archive.org/details/{IDENT}",
                )
            ],
            files=files,
        )
        self.qbt.__enter__()
        self.addCleanup(self.qbt.__exit__)

        self.client = QbitClient(self.qbt.url, username="admin", password="secret")
        self.archive = ArchiveOrg(self.archive_server.url, retries=1, timeout=10)
        self.console = Console(quiet=True)

    def plan(self, **kwargs):
        torrent = self.client.torrent(self.torrent.info_hash)
        return build_plan(self.client, self.archive, torrent, **kwargs)

    def revive(self, **kwargs):
        downloader = Downloader(self.archive, jobs=2, console=self.console)
        return revive_plan(
            self.client,
            downloader,
            self.plan(),
            state_dir=self.state_dir,
            console=self.console,
            **kwargs,
        )

    # ------------------------------------------------------------------ tests

    def test_scan_finds_the_archive_torrent(self):
        candidates = collect_candidates(self.client)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].identifier, IDENT)
        self.assertTrue(candidates[0].needs_help)
        self.assertTrue(candidates[0].dead)  # num_complete == 0
        self.assertEqual(candidates[0].flag, "dead")

    def test_scan_ignores_unrelated_torrents(self):
        self.qbt.torrents["unrelated"] = qbt_torrent(
            torrent_hash="unrelated",
            name="ubuntu.iso",
            comment="ubuntu",
            tracker="udp://tracker.example:80/announce",
            magnet_uri="",
        )
        candidates = collect_candidates(self.client)
        self.assertEqual([c.identifier for c in candidates], [IDENT])

    def test_plan_matches_the_item(self):
        plan = self.plan()
        self.assertEqual(plan.identifier, IDENT)
        self.assertEqual(len(plan.files), 3)
        self.assertEqual(plan.to_fetch, 2)  # the empty file only needs creating
        self.assertTrue(plan.revivable)
        self.assertFalse(plan.stale)

    def test_full_revive_reaches_one_hundred_percent(self):
        outcome = self.revive()
        self.assertTrue(outcome.ok, outcome.summary)
        self.assertTrue(outcome.completed)
        self.assertEqual(outcome.progress_after, 1.0)

        for name, payload in PAYLOADS.items():
            target = self.root / "downloads" / IDENT / name
            self.assertEqual(target.read_bytes(), payload, name)
            if payload:
                self.assertTrue(md5_file(target))

        # The torrent must have been paused while writing, then rechecked, then
        # started (it was downloading before this run).
        self.assertIn(self.torrent.info_hash, self.qbt.stopped)
        self.assertIn(self.torrent.info_hash, self.qbt.rechecked)
        self.assertIn(self.torrent.info_hash, self.qbt.started)
        self.assertIn("seeding", outcome.summary)

    def test_revive_leaves_a_previously_paused_torrent_stopped(self):
        self.qbt.set_torrent(state="stoppedDL")
        outcome = self.revive()
        self.assertTrue(outcome.ok)
        self.assertEqual(self.qbt.started, [])
        self.assertIn("stopped", outcome.summary)
        # The data is still fetched and verified even though we do not start it.
        self.assertEqual(outcome.progress_after, 1.0)

    def test_revive_can_force_a_start(self):
        self.qbt.set_torrent(state="stoppedDL")
        outcome = self.revive(start=True)
        self.assertTrue(outcome.ok)
        self.assertIn(self.torrent.info_hash, self.qbt.started)

    def test_dry_run_style_plan_does_not_touch_the_disk(self):
        plan = self.plan()
        self.assertEqual(plan.to_fetch, 2)
        self.assertFalse((self.root / "downloads").exists())
        self.assertEqual(self.qbt.rechecked, [])

    def test_second_revive_is_a_no_op(self):
        self.revive()
        plan = self.plan()
        self.assertEqual(plan.to_fetch, 0, "everything should already be on disk")
        self.assertEqual(plan.to_fetch_bytes, 0)

    def test_resume_after_interruption_completes_the_file(self):
        # Simulate a previous run that died mid-file.
        part = self.root / "downloads" / IDENT / "beta.txt.ia-reviver-part"
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(PAYLOADS["beta.txt"][:100])

        outcome = self.revive()
        self.assertTrue(outcome.ok)
        self.assertEqual(
            (self.root / "downloads" / IDENT / "beta.txt").read_bytes(), PAYLOADS["beta.txt"]
        )

    def test_state_history_is_written(self):
        outcomes = [self.revive()]
        path = write_state(self.state_dir, outcomes)
        self.assertIsNotNone(path)
        records = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(records[0]["identifier"], IDENT)
        self.assertTrue(records[0]["completed"])
        self.assertEqual(records[0]["after"], 1.0)

    def test_lock_is_exclusive_then_released(self):
        def acquire_again():
            with torrent_lock(self.state_dir, "abc"):
                pass

        # Holding the lock makes a second acquisition fail...
        with torrent_lock(self.state_dir, "abc"):
            self.assertRaises(LockError, acquire_again)
        # ...and letting it go makes acquisition succeed again.
        acquire_again()

    def test_stale_lock_from_a_dead_process_is_taken_over(self):
        lock_dir = self.state_dir / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        stale = lock_dir / "abc.lock"
        stale.write_text("999999")  # a PID that cannot exist
        with torrent_lock(self.state_dir, "abc"):
            pass

    def test_destination_escaping_the_save_path_is_refused(self):
        # A hostile entry in the file list must never be written elsewhere.
        self.qbt.files[self.torrent.info_hash].append(qbt_file(f"{IDENT}/../../../etc/evil", 10))
        plan = self.plan()
        evil = [f for f in plan.files if f.action == "unavailable"]
        self.assertEqual(len(evil), 1)
        self.assertIn("unsafe path", evil[0].note)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
