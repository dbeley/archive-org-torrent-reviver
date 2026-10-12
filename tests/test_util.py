"""Tests for formatting, MD5 streaming and — most importantly — path safety."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ia_reviver.util import (
    format_table,
    human_bytes,
    human_duration,
    md5_file,
    parse_size,
    safe_relative_path,
    short_hash,
)


class TestFormatting(unittest.TestCase):
    def test_human_bytes(self):
        self.assertEqual(human_bytes(0), "0 B")
        self.assertEqual(human_bytes(512), "512 B")
        self.assertEqual(human_bytes(1024), "1.0 KiB")
        self.assertEqual(human_bytes(1536), "1.5 KiB")
        self.assertEqual(human_bytes(1024**2), "1.0 MiB")
        self.assertEqual(human_bytes(3 * 1024**3), "3.0 GiB")

    def test_human_duration(self):
        self.assertEqual(human_duration(5), "5s")
        self.assertEqual(human_duration(65), "1m05s")
        self.assertEqual(human_duration(3725), "1h02m")
        self.assertEqual(human_duration(-1), "--")

    def test_parse_size(self):
        self.assertEqual(parse_size("512"), 512)
        self.assertEqual(parse_size("1K"), 1024)
        self.assertEqual(parse_size("10M"), 10 * 1024**2)
        self.assertEqual(parse_size("1.5G"), int(1.5 * 1024**3))
        self.assertEqual(parse_size("2 GiB"), 2 * 1024**3)

    def test_short_hash(self):
        self.assertEqual(short_hash("abcdef0123456789", 6), "abcdef")

    def test_format_table_aligns(self):
        table = format_table(["A", "BB"], [["x", "y"], ["long", "z"]])
        lines = table.splitlines()
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[0], "A     BB")
        self.assertEqual(lines[1], "----  --")
        self.assertEqual(lines[2], "x     y")
        self.assertEqual(lines[3], "long  z")


class TestSafeRelativePath(unittest.TestCase):
    def test_accepts_normal_paths(self):
        for name in ("a.txt", "dir/a.txt", "dir/sub/a.txt", "…unicode…/x.bin"):
            path, reason = safe_relative_path(name)
            self.assertEqual(path, name, reason)

    def test_normalizes_separators(self):
        self.assertEqual(safe_relative_path("dir//a.txt")[0], "dir/a.txt")
        self.assertEqual(safe_relative_path("./a.txt")[0], "a.txt")
        self.assertEqual(safe_relative_path("dir\\a.txt")[0], "dir/a.txt")

    def test_rejects_traversal(self):
        # A hostile torrent must never be able to write outside its own folder.
        for name in ("../evil", "a/../../evil", "..", "a/..", "../../etc/passwd"):
            path, reason = safe_relative_path(name)
            self.assertIsNone(path, f"{name} should be rejected")
            self.assertTrue(reason)

    def test_rejects_absolute(self):
        for name in ("/etc/passwd", "\\windows\\system32"):
            path, _ = safe_relative_path(name)
            self.assertIsNone(path, f"{name} should be rejected")

    def test_rejects_empty(self):
        self.assertIsNone(safe_relative_path("")[0])
        self.assertIsNone(safe_relative_path(".")[0])


class TestMd5File(unittest.TestCase):
    def test_matches_hashlib(self):
        import hashlib

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "blob.bin"
            payload = bytes(range(256)) * 5000  # 1.28 MB, crosses the read chunk
            path.write_bytes(payload)
            self.assertEqual(md5_file(path), hashlib.md5(payload).hexdigest())

    def test_empty_file(self):
        import hashlib

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty"
            path.write_bytes(b"")
            self.assertEqual(md5_file(path), hashlib.md5(b"").hexdigest())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
