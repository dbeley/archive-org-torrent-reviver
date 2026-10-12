"""Console output: human summaries plus a single-line live progress display.

Kept dependency-free, and it degrades cleanly: when stdout is not a terminal
(cron, systemd, `nix run … | tee`) the animated line is replaced by plain
periodic lines so logs stay readable.
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass

from .util import human_bytes, human_duration, human_rate, is_tty

__all__ = ["Console", "Reporter", "FileOutcome"]

BAR_WIDTH = 22


def _bar(fraction: float) -> str:
    fraction = max(0.0, min(1.0, fraction))
    filled = int(round(BAR_WIDTH * fraction))
    return "█" * filled + "░" * (BAR_WIDTH - filled)


class Console:
    """Very small logger with optional colour."""

    def __init__(self, *, quiet: bool = False, verbose: bool = False, stream=None) -> None:
        self.quiet = quiet
        self.verbose = verbose
        self.stream = stream or sys.stdout
        self.tty = is_tty(self.stream)
        self.color = self.tty
        self._progress_visible = False
        self._last_progress = 0.0

    def _wrap(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def _write(self, text: str) -> None:
        print(text, file=self.stream, flush=True)

    def info(self, message: str) -> None:
        if not self.quiet:
            self._write(message)

    def ok(self, message: str) -> None:
        self._write(f"{self._wrap('✓', '32')} {message}")

    def warn(self, message: str) -> None:
        self._write(f"{self._wrap('!', '33')} {message}")

    def error(self, message: str) -> None:
        self._write(f"{self._wrap('✗', '31')} {message}")

    def detail(self, message: str) -> None:
        if self.verbose:
            self._write(f"  {self._wrap('·', '90')} {message}")

    def blank(self) -> None:
        if not self.quiet:
            self._write("")

    def section(self, title: str) -> None:
        if not self.quiet:
            self._write(self._wrap(title, "1"))

    def progress(self, current: int, total: int, label: str) -> None:
        """Live counter for phases that spend minutes fetching before printing.

        Planning has to talk to archive.org once per torrent before it can show
        anything, so without this a run over dozens of torrents sits silent for
        minutes and is indistinguishable from a hang. On a terminal the line is
        rewritten in place; when piped (cron, systemd, `| tee`) it emits a line
        every few seconds instead, so a log still shows movement.
        """
        if self.quiet:
            return
        text = f"  [{current:>3}/{total}] {label}"
        if self.tty:
            print(f"\r\033[K{text}", end="", file=self.stream, flush=True)
            self._progress_visible = True
            return
        now = time.monotonic()
        if current != total and now - self._last_progress < 5.0:
            return
        self._last_progress = now
        self._write(text)

    def progress_done(self) -> None:
        """Erase the in-place counter, if one is on screen."""
        if self._progress_visible:
            print("\r\033[K", end="", file=self.stream, flush=True)
            self._progress_visible = False


@dataclass(slots=True)
class FileOutcome:
    """Result of one file transfer, as reported to the live display."""

    rel: str
    status: str  # "ok" | "skipped" | "failed" | "created"
    size: int = 0
    error: str = ""


class Reporter:
    """Thread-safe progress reporter used by the downloader."""

    def __init__(
        self,
        console: Console | None = None,
        *,
        total_files: int = 0,
        total_bytes: int = 0,
        animate: bool | None = None,
        interval: float = 0.25,
    ) -> None:
        self.console = console or Console()
        self.total_files = total_files
        self.total_bytes = total_bytes
        self._lock = threading.Lock()
        self._done_bytes = 0
        self._done_files = 0
        self._active: set[str] = set()
        self._started_at = time.monotonic()
        self._last_plain = 0.0
        self._line_visible = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.interval = interval
        if animate is None:
            animate = is_tty(self.console.stream)
        self.animate = animate

    # ------------------------------------------------------------- lifecycle

    def __enter__(self) -> Reporter:
        if self.animate:
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._clear_line()
        self.console.info(self.summary())

    # ------------------------------------------------------------- callbacks

    def add_bytes(self, count: int) -> None:
        with self._lock:
            self._done_bytes += count

    def file_started(self, rel: str) -> None:
        with self._lock:
            self._active.add(rel)

    def file_finished(self, outcome: FileOutcome) -> None:
        with self._lock:
            self._active.discard(outcome.rel)
            self._done_files += 1
        if outcome.status == "failed":
            self._clear_line()
            self.console.error(f"{outcome.rel}: {outcome.error}")
        elif self.console.verbose:
            self._clear_line()
            label = {"ok": "fetched", "skipped": "already complete", "created": "created"}.get(
                outcome.status, outcome.status
            )
            self.console.detail(f"{label}: {outcome.rel} ({human_bytes(outcome.size)})")

    # --------------------------------------------------------------- rendering

    @property
    def elapsed(self) -> float:
        return max(1e-6, time.monotonic() - self._started_at)

    @property
    def rate(self) -> float:
        return self._done_bytes / self.elapsed

    def summary(self) -> str:
        return (
            f"{self._done_files}/{self.total_files} files · "
            f"{human_bytes(self._done_bytes)} of {human_bytes(self.total_bytes)} · "
            f"avg {human_rate(self.rate)} · {human_duration(self.elapsed)}"
        )

    def _status_line(self) -> str:
        fraction = self._done_bytes / self.total_bytes if self.total_bytes else 1.0
        eta = (self.total_bytes - self._done_bytes) / self.rate if self.rate > 0 else -1
        active = len(self._active)
        return (
            f"\r{_bar(fraction)} {fraction * 100:5.1f}%  "
            f"{human_bytes(self._done_bytes)}/{human_bytes(self.total_bytes)}  "
            f"{human_rate(self.rate)}  eta {human_duration(eta)}  "
            f"({active} active)"
        )

    def _clear_line(self) -> None:
        with self._lock:
            if self._line_visible:
                print("\r\033[K", end="", file=self.console.stream, flush=True)
                self._line_visible = False

    def _spin(self) -> None:
        while not self._stop.wait(self.interval):
            with self._lock:
                if self._done_files >= self.total_files and not self._active:
                    continue
                print(self._status_line(), end="", file=self.console.stream, flush=True)
                self._line_visible = True

    def plain_tick(self) -> None:
        """Non-animated progress: emit a line at most a few times per minute."""
        now = time.monotonic()
        if now - self._last_plain < 10.0:
            return
        self._last_plain = now
        self.console.info(f"  … {self.summary()}")
