"""Tests for the bencode decoder and torrent parsing."""

from __future__ import annotations

import hashlib
import unittest

from ia_reviver.bencode import (
    BencodeError,
    bdecode,
    info_span,
    infohash,
    parse_torrent,
)
from tests.fakes import FIXTURE_IDENTIFIER, fixture_torrent, fixture_torrent_bytes

# The real info-hash of the fixture torrent, cross-checked against the value
# qBittorrent itself reported for this torrent and against the archive.org
# tracker's scrape. If the parser drifts, this test catches it.
FIXTURE_INFO_HASH = "d9a84f76bd0be498d7ac3764d7bde16a632855a7"


class TestPrimitives(unittest.TestCase):
    def test_integer(self):
        self.assertEqual(bdecode(b"i42e"), (4, 42))
        self.assertEqual(bdecode(b"i-7e"), (4, -7))
        self.assertEqual(bdecode(b"i0e"), (3, 0))

    def test_string(self):
        self.assertEqual(bdecode(b"4:spam"), (6, b"spam"))
        self.assertEqual(bdecode(b"0:"), (2, b""))

    def test_list(self):
        self.assertEqual(bdecode(b"l4:spami42ee"), (12, [b"spam", 42]))

    def test_dict(self):
        # len(b"d3:cow3:moo4:spam4:eggse") == 24
        self.assertEqual(
            bdecode(b"d3:cow3:moo4:spam4:eggse"), (24, {b"cow": b"moo", b"spam": b"eggs"})
        )

    def test_nested(self):
        _, value = bdecode(b"d1:ali1ei2eee")
        self.assertEqual(value, {b"a": [1, 2]})

    def test_offset_respected(self):
        data = b"xx4:spam"  # 8 bytes; the value ends at index 8
        self.assertEqual(bdecode(data, 2), (8, b"spam"))

    def test_truncated_raises(self):
        with self.assertRaises(BencodeError):
            bdecode(b"4:spa")

    def test_unterminated_raises(self):
        with self.assertRaises(BencodeError):
            bdecode(b"d1:a")

    def test_garbage_raises(self):
        with self.assertRaises(BencodeError):
            bdecode(b"v")

    def test_non_bytes_dict_key_raises(self):
        with self.assertRaises(BencodeError):
            bdecode(b"di1ei2ee")


class TestTorrentParsing(unittest.TestCase):
    def test_info_span_hashes_exact_bytes(self):
        raw = fixture_torrent_bytes()
        start, end = info_span(raw)
        self.assertEqual(hashlib.sha1(raw[start:end]).hexdigest(), FIXTURE_INFO_HASH)
        # The span must be the raw slice, not a re-encoding of the decoded dict.
        self.assertEqual(raw[start : start + 1], b"d")

    def test_infohash_helper_matches(self):
        self.assertEqual(infohash(fixture_torrent_bytes()), FIXTURE_INFO_HASH)

    def test_parse_real_archive_torrent(self):
        meta = fixture_torrent()
        self.assertEqual(meta.info_hash, FIXTURE_INFO_HASH)
        self.assertEqual(meta.name, FIXTURE_IDENTIFIER)
        self.assertEqual(len(meta.files), 16)
        self.assertEqual(meta.total_size, 13858803)
        self.assertEqual(meta.piece_length, 524288)
        self.assertIn("bt1.archive.org", meta.tracker_hosts)
        self.assertIn("bt2.archive.org", meta.tracker_hosts)
        self.assertTrue(meta.url_list)

    def test_comment_carries_the_details_url(self):
        meta = fixture_torrent()
        self.assertIn(f"https://archive.org/details/{FIXTURE_IDENTIFIER}", meta.comment)

    def test_file_paths_are_relative_without_the_root(self):
        meta = fixture_torrent()
        for entry in meta.files:
            # Paths are relative to the torrent root, so they must never be
            # prefixed with it (that prefix is the folder qBittorrent creates).
            self.assertFalse(entry.path.startswith(meta.name + "/"))
            self.assertFalse(entry.path.startswith("/"))
            self.assertEqual(entry.path.encode("utf-8", "surrogateescape"), entry.path_bytes)

    def test_multi_file_paths_are_split_on_separators(self):
        raw = (
            b"d4:infod5:filesl"
            b"d6:lengthi3e4:pathl6:subdir5:a.txtee"
            b"e4:name1:x12:piece lengthi16384e6:pieces20:aaaaaaaaaaaaaaaaaaaaee"
        )
        meta = parse_torrent(raw)
        self.assertEqual(meta.files[0].path, "subdir/a.txt")
        self.assertEqual(meta.files[0].path_bytes, b"subdir/a.txt")

    def test_single_file_torrent(self):
        # Minimal single-file torrent: info.length instead of info.files.
        raw = (
            b"d"
            b"4:infod"
            b"6:lengthi1024e"
            b"4:name7:one.bin"
            b"12:piece lengthi16384e"
            b"6:pieces20:aaaaaaaaaaaaaaaaaaaa"
            b"ee"
        )
        meta = parse_torrent(raw)
        self.assertTrue(meta.single_file)
        self.assertEqual(meta.name, "one.bin")
        self.assertEqual(len(meta.files), 1)
        self.assertEqual(meta.files[0].path, "one.bin")
        self.assertEqual(meta.total_size, 1024)

    def test_zero_length_files_parsed(self):
        # archive.org torrents routinely contain empty marker files (a `.srt`
        # sidecar that was never produced, a 0-byte log). They must be parsed as
        # 0-length entries rather than dropped.
        from tests.fakes import bencode

        raw = bencode(
            {
                "info": {
                    "name": "x",
                    "piece length": 16384,
                    "pieces": b"a" * 20,
                    "files": [
                        {"length": 0, "path": ["marker.log"]},
                        {"length": 12, "path": ["a.txt"]},
                    ],
                }
            }
        )
        meta = parse_torrent(raw)
        sizes = {f.path: f.size for f in meta.files}
        self.assertEqual(sizes, {"marker.log": 0, "a.txt": 12})
        self.assertTrue(any(f.is_empty for f in meta.files))

    def test_missing_info_raises(self):
        with self.assertRaises(BencodeError):
            parse_torrent(b"d8:announce13:http://x/y/ze")

    def test_non_utf8_path_is_preserved_as_bytes(self):
        raw = (
            b"d4:infod5:filesl"
            b"d6:lengthi5e4:pathl3:\xff\xfe\xfdee"
            b"e4:name1:x12:piece lengthi16384e6:pieces20:aaaaaaaaaaaaaaaaaaaaee"
        )
        meta = parse_torrent(raw)
        self.assertEqual(meta.files[0].path_bytes, b"\xff\xfe\xfd")
        # Round-tripping through the surrogate-escaped str must give back the bytes.
        self.assertEqual(meta.files[0].path.encode("utf-8", "surrogateescape"), b"\xff\xfe\xfd")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
