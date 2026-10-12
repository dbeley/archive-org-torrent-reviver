"""Tests for the qBittorrent client, against a fake server that misbehaves like the real one."""

from __future__ import annotations

import unittest

from ia_reviver.qbittorrent import (
    QbitAuthError,
    QbitBannedError,
    QbitClient,
    QbitError,
    QbitNotFound,
    Torrent,
)
from tests.fakes import FakeQbitServer, qbt_file, qbt_torrent

HASH = "d9a84f76bd0be498d7ac3764d7bde16a632855a7"


class QbitClientTest(unittest.TestCase):
    server: FakeQbitServer

    def setUp(self):
        self.server = FakeQbitServer(
            torrents=[qbt_torrent()],
            files={
                HASH: [
                    qbt_file("ramrakshastotram/a.txt", 2048),
                    qbt_file("ramrakshastotram/b.txt", 0, 1.0),
                ]
            },
        ).start()
        self.addCleanup(self.server.stop)
        self.client = QbitClient(self.server.url, username="admin", password="secret")

    # --------------------------------------------------------------- auth

    def test_login_accepts_204_with_empty_body(self):
        """qBittorrent 5.x answers 204, not 200/"Ok." — this must succeed."""
        self.assertEqual(self.client.login(), "v5.2.3")

    def test_login_with_wrong_password_raises_auth_error(self):
        client = QbitClient(self.server.url, username="admin", password="nope")
        with self.assertRaises(QbitAuthError):
            client.login()

    def test_requests_fail_without_a_session_cookie(self):
        """The fake server 403s without a session, like the real WebUI does."""
        client = QbitClient(self.server.url)  # no credentials
        with self.assertRaises(QbitError):
            client.torrents()

    def test_no_username_but_auth_required_explains_the_problem(self):
        client = QbitClient(self.server.url)
        with self.assertRaises(QbitAuthError) as ctx:
            client.login()
        self.assertIn("requires authentication", str(ctx.exception))

    def test_login_is_implicit_on_first_call(self):
        self.assertEqual(len(self.client.torrents()), 1)

    def test_banned_ip_is_reported_clearly(self):
        self.server.set_banned(True)
        client = QbitClient(self.server.url, username="admin", password="secret")
        with self.assertRaises(QbitBannedError) as ctx:
            client.login()
        self.assertIn("ban", str(ctx.exception).lower())

    def test_cookie_is_carried_between_calls(self):
        self.client.login()
        self.assertEqual(len(self.client.torrents()), 1)
        self.assertEqual(self.client.webapi_version(), "2.15.1")

    # -------------------------------------------------------------- reading

    def test_torrent_properties(self):
        self.client.login()
        torrent = self.client.torrents()[0]
        self.assertEqual(torrent.hash, HASH)
        self.assertEqual(torrent.name, "ramrakshastotram")
        self.assertEqual(torrent.state, "stalledDL")
        self.assertEqual(torrent.num_complete, 0)
        self.assertFalse(torrent.is_complete)
        self.assertFalse(torrent.is_checking)
        self.assertFalse(torrent.is_paused)
        self.assertIn("archive.org/details/", torrent.comment)
        self.assertEqual(torrent.tracker_urls, [])

    def test_torrent_flags(self):
        seeding = Torrent(qbt_torrent(state="uploading", progress=1.0))
        self.assertTrue(seeding.is_complete)
        checking = Torrent(qbt_torrent(state="checkingResumeData"))
        self.assertTrue(checking.is_checking)
        stopped = Torrent(qbt_torrent(state="stoppedDL"))
        self.assertTrue(stopped.is_paused)

    def test_unknown_seeder_count_is_minus_one(self):
        torrent = Torrent(qbt_torrent(num_complete=-1))
        self.assertEqual(torrent.num_complete, -1)

    def test_magnet_tracker_urls_are_extracted(self):
        magnet = (
            "magnet:?xt=urn:btih:" + HASH + "&dn=x"
            "&tr=http%3A%2F%2Fbt1.archive.org%3A6969%2Fannounce"
            "&tr=udp%3A%2F%2Ftracker.example%3A80"
        )
        torrent = Torrent(qbt_torrent(magnet_uri=magnet))
        self.assertEqual(
            torrent.tracker_urls,
            ["http://bt1.archive.org:6969/announce", "udp://tracker.example:80"],
        )

    def test_files_keep_root_folder_prefix(self):
        self.client.login()
        files = self.client.files(HASH)
        self.assertEqual(files[0]["name"], "ramrakshastotram/a.txt")

    def test_missing_torrent_raises_not_found(self):
        self.client.login()
        with self.assertRaises(QbitNotFound):
            self.client.torrent("deadbeef" * 5)

    # ------------------------------------------------------------ mutations

    def test_recheck_sends_hashes(self):
        self.client.login()
        self.client.recheck([HASH])
        self.assertEqual(self.server.rechecked, [HASH])

    def test_stop_then_start(self):
        self.client.login()
        self.client.stop([HASH])
        self.client.start([HASH])
        self.assertEqual(self.server.stopped, [HASH])
        self.assertEqual(self.server.started, [HASH])

    def test_stop_falls_back_to_legacy_pause_endpoint(self):
        legacy = FakeQbitServer(legacy_pause_names=True).start()
        self.addCleanup(legacy.stop)
        client = QbitClient(legacy.url, username="admin", password="secret")
        client.login()
        client.stop([HASH])
        client.start([HASH])
        self.assertEqual(legacy.stopped, [HASH])
        self.assertEqual(legacy.started, [HASH])

    def test_then_check_waits_until_hashing_finishes(self):
        self.client.login()
        torrents = self.client.then_check([HASH], timeout=15, poll=0.05)
        self.assertEqual(torrents[HASH].state, "stoppedUP")
        self.assertTrue(torrents[HASH].is_complete)
        # It must have observed the checking state rather than returning instantly.
        calls = [c for c in self.server.calls if c[1] == "/api/v2/torrents/info"]
        self.assertGreaterEqual(len(calls), 2)

    def test_then_check_reports_partial_progress(self):
        self.server.final_state = "stalledDL"
        self.server.final_progress = 0.42
        self.client.login()
        torrents = self.client.then_check([HASH], timeout=15, poll=0.05)
        self.assertAlmostEqual(torrents[HASH].progress, 0.42)


class QbitErrorTest(unittest.TestCase):
    def test_unreachable_server_is_a_clear_error(self):
        # Port 1 is reserved and nothing listens there.
        client = QbitClient("http://127.0.0.1:1", username="admin", password="x", timeout=2)
        with self.assertRaises(QbitError) as ctx:
            client.login()
        self.assertIn("cannot reach", str(ctx.exception).lower())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
