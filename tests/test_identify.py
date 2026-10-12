"""Tests for archive.org torrent detection."""

from __future__ import annotations

import unittest

from ia_reviver.identify import (
    archive_identifier_from_text,
    classify_torrent,
    tracker_hosts,
)
from ia_reviver.qbittorrent import Torrent
from tests.fakes import qbt_torrent

REAL_COMMENT = (
    "This content hosted at the Internet Archive at https://archive.org/details/AboutBan1935\n"
    "Files may have changed, which prevents torrents from downloading correctly or completely; "
    "please check for an updated torrent at "
    "https://archive.org/download/AboutBan1935/AboutBan1935_archive.torrent\n"
)


class TestIdentifierExtraction(unittest.TestCase):
    def test_extracts_from_real_comment(self):
        self.assertEqual(archive_identifier_from_text(REAL_COMMENT), "AboutBan1935")

    def test_identifier_kept_verbatim(self):
        # Identifiers must be used literally: these two differ by one letter and
        # are genuinely different items.
        text = "https://archive.org/details/1942-01-07_Gen_Adolph_Takes_Over"
        self.assertEqual(archive_identifier_from_text(text), "1942-01-07_Gen_Adolph_Takes_Over")

    def test_ignores_bare_details_path(self):
        self.assertIsNone(archive_identifier_from_text("https://archive.org/details/"))

    def test_ignores_other_hosts(self):
        self.assertIsNone(archive_identifier_from_text("https://example.com/details/xyz"))

    def test_tolerates_spaces_and_case(self):
        self.assertEqual(
            archive_identifier_from_text("see HTTPS://Archive.Org / details / my-item_1.0"),
            "my-item_1.0",
        )

    def test_empty_input(self):
        self.assertIsNone(archive_identifier_from_text(""))


class TestClassifier(unittest.TestCase):
    def test_comment_url_wins(self):
        match = classify_torrent(Torrent(qbt_torrent(comment=REAL_COMMENT)))
        self.assertIsNotNone(match)
        self.assertEqual(match.identifier, "AboutBan1935")
        self.assertIn("comment", match.sources)
        self.assertTrue(match.confident)

    def test_detected_from_tracker_alone(self):
        torrent = Torrent(
            qbt_torrent(
                comment="no useful comment here", tracker="http://bt1.archive.org:6969/announce"
            )
        )
        match = classify_torrent(torrent)
        self.assertIsNotNone(match)
        self.assertIn("tracker", match.sources)
        self.assertIn("bt1.archive.org", match.hosts)
        # No details URL, so the identifier must be resolved later from the name.
        self.assertIsNone(match.identifier)
        self.assertFalse(match.confident)

    def test_detected_from_magnet_tracker(self):
        magnet = "magnet:?xt=urn:btih:abc&tr=http%3A%2F%2Fbt2.archive.org%3A6969%2Fannounce"
        match = classify_torrent(Torrent(qbt_torrent(comment="", tracker="", magnet_uri=magnet)))
        self.assertIsNotNone(match)
        self.assertIn("bt2.archive.org", match.hosts)

    def test_detected_from_webseed_only(self):
        magnet = "magnet:?xt=urn:btih:abc&ws=https%3A%2F%2Farchive.org%2Fdownload%2F"
        match = classify_torrent(Torrent(qbt_torrent(comment="", tracker="", magnet_uri=magnet)))
        self.assertIsNotNone(match)
        self.assertIn("webseed", match.sources)

    def test_non_archive_torrent_is_ignored(self):
        torrent = Torrent(
            qbt_torrent(
                name="ubuntu-24.04.iso",
                comment="Ubuntu release",
                tracker="udp://tracker.opentrackr.org:1337/announce",
                magnet_uri="",
            )
        )
        self.assertIsNone(classify_torrent(torrent))

    def test_tracker_hosts_skips_placeholder_entries(self):
        torrent = Torrent(qbt_torrent(tracker="** [DHT] **"))
        self.assertEqual(tracker_hosts(torrent), [])

    def test_tracker_hosts_handles_udp_and_ipv6(self):
        torrent = Torrent(qbt_torrent(tracker="udp://[2001:db8::1]:6969/announce"))
        self.assertEqual(tracker_hosts(torrent), ["2001:db8::1"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
