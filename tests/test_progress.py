"""The progress counter exists so a long silent phase cannot look like a hang.

Planning talks to archive.org once per torrent before it can print anything, so
a run over dozens of torrents spends minutes with no output. These tests pin the
two behaviours that keep that honest: a terminal shows a live counter, and a
piped log still shows movement.
"""

from __future__ import annotations

import io
import unittest

from ia_reviver.progress import Console


class FakeTty(io.StringIO):
    """A stream that claims to be a terminal."""

    def isatty(self) -> bool:
        return True


class TestProgressCounter(unittest.TestCase):
    def test_piped_output_emits_periodic_lines(self):
        stream = io.StringIO()
        console = Console(stream=stream)

        console.progress(1, 3, "first")
        console.progress(2, 3, "second")  # throttled: too soon after the last
        console.progress(3, 3, "last")  # the final item is always emitted

        out = stream.getvalue()
        self.assertIn("[  1/3] first", out)
        self.assertNotIn("second", out)
        self.assertIn("[  3/3] last", out)

    def test_terminal_output_is_rewritten_in_place(self):
        stream = FakeTty()
        console = Console(stream=stream)

        console.progress(1, 2, "first")
        console.progress(2, 2, "last")
        console.progress_done()

        out = stream.getvalue()
        # Carriage returns, never newlines: one line, rewritten.
        self.assertNotIn("\n", out)
        self.assertIn("[  1/2] first", out)
        self.assertIn("[  2/2] last", out)
        self.assertTrue(out.endswith("\r\033[K"), repr(out))

    def test_quiet_console_says_nothing(self):
        stream = io.StringIO()
        console = Console(quiet=True, stream=stream)

        console.progress(1, 1, "first")
        console.progress_done()

        self.assertEqual(stream.getvalue(), "")

    def test_progress_done_is_safe_when_nothing_was_drawn(self):
        stream = io.StringIO()
        Console(stream=stream).progress_done()

        self.assertEqual(stream.getvalue(), "")

    def test_progress_never_writes_a_newline_on_a_terminal(self):
        """A stray newline would scroll a 66-torrent plan off the screen."""
        stream = FakeTty()
        console = Console(stream=stream)

        for index in range(1, 67):
            console.progress(index, 66, f"item{index}")

        self.assertNotIn("\n", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
