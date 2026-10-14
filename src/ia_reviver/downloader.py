"""Resumable, rate-limited, parallel HTTP fetching of item files.

Three properties matter here and are covered by explicit tests:

* **Resumable** — a partial file is kept as ``<name>.ia-reviver-part`` and the
  next run continues it with a ``Range`` request instead of starting over.
  archive.org serves byte ranges, so a multi-gigabyte item survives a dropped
  connection.
* **Verified** — a file is renamed into place only after its size *and* (when the
  item publishes one) its MD5 match the item's own manifest. A corrupt transfer
  can therefore never be presented to qBittorrent as complete.
* **Contained** — every destination is re-checked to be inside the torrent's
  ``save_path`` before anything is written.
"""

from __future__ import annotations

import http.client
import os
import threading
import time
import urllib.error
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .archive import ArchiveHttpError, ArchiveOrg, ArchiveOrgError
from .planner import FETCH, TOUCH, PlannedFile, TorrentPlan
from .progress import Console, FileOutcome, Reporter
from .util import md5_file

__all__ = ["FileResult", "RateLimiter", "Downloader", "DownloadError", "PART_SUFFIX"]

PART_SUFFIX = ".ia-reviver-part"
CHUNK_SIZE = 512 * 1024


class DownloadError(RuntimeError):
    """A file could not be fetched or failed verification."""


class _Transient(DownloadError):
    """A failure worth retrying (network blip, 5xx, truncated response)."""


@dataclass(slots=True)
class FileResult:
    """Outcome of one file."""

    rel: str
    status: str  # "ok" | "skipped" | "created" | "failed"
    size: int = 0
    error: str = ""
    elapsed: float = 0.0
    resumed_from: int = 0

    @property
    def ok(self) -> bool:
        return self.status != "failed"


class RateLimiter:
    """Simple token-less rate limiter: keeps total throughput under a ceiling.

    Shared across download threads so ``--limit-rate`` bounds the *aggregate*
    speed, not the speed of each connection.
    """

    def __init__(self, bytes_per_second: int | None) -> None:
        self.rate = float(bytes_per_second) if bytes_per_second else 0.0
        self._lock = threading.Lock()
        self._total = 0
        self._start = time.monotonic()

    def consume(self, count: int) -> None:
        if self.rate <= 0:
            return
        with self._lock:
            self._total += count
            target = self._total / self.rate
            ahead = target - (time.monotonic() - self._start)
        if ahead > 0:
            time.sleep(min(ahead, 5.0))


def _announced_size(response, offset: int) -> int | None:
    """The file's total size as the server states it, or ``None`` if it doesn't.

    ``None`` means "not stated", never "zero", so a response that omits a length
    falls through to the byte count instead of looking like a mismatch.
    """
    if response.headers.get("Content-Encoding"):
        # The body is encoded, so Content-Length is not the file's size.
        return None
    content_range = response.headers.get("Content-Range") or ""
    if "/" in content_range:
        total = content_range.rsplit("/", 1)[-1].strip()
        if total.isdigit():
            return int(total)
    length = (response.headers.get("Content-Length") or "").strip()
    if length.isdigit():
        return int(length) + offset
    return None


def _changed_at_source(planned: PlannedFile, expected: int, actual: int) -> str:
    """Explain a size this torrent can never match."""
    return (
        f"'{planned.item_name}' is {actual} B on archive.org but this torrent records "
        f"{expected} B: the item changed after the .torrent was created, so those exact "
        "bytes no longer exist and the recheck cannot reach 100%"
    )


def _within(root: Path, candidate: Path) -> bool:
    """True when ``candidate`` resolves to a path inside ``root``."""
    try:
        resolved = candidate.resolve()
        base = root.resolve()
    except OSError:  # pragma: no cover - unreadable path
        return False
    return resolved == base or base in resolved.parents


class Downloader:
    """Fetches the files of one or more plans from archive.org."""

    def __init__(
        self,
        archive: ArchiveOrg,
        *,
        jobs: int = 3,
        limit_rate: int | None = None,
        verify: bool = True,
        retries: int = 3,
        timeout: float = 120.0,
        console: Console | None = None,
    ) -> None:
        self.archive = archive
        self.jobs = max(1, jobs)
        self.verify = verify
        self.retries = max(1, retries)
        self.timeout = timeout
        self.limiter = RateLimiter(limit_rate)
        self.console = console or Console()

    # ------------------------------------------------------------------ public

    def fetch_plan(self, plan: TorrentPlan) -> list[FileResult]:
        """Fetch everything the plan marked for download, plus create empty files."""
        targets = [f for f in plan.files if f.action in (FETCH, TOUCH)]
        if not targets:
            return []
        total_bytes = sum(f.size for f in targets if f.action == FETCH)
        root = Path(plan.torrent.save_path or ".")

        results: list[FileResult] = []
        with Reporter(
            self.console,
            total_files=len(targets),
            total_bytes=total_bytes,
        ) as reporter:
            empty = [f for f in targets if f.action == TOUCH]
            for planned in empty:
                results.append(self._touch(root, planned))
                reporter.file_finished(
                    FileOutcome(planned.rel, results[-1].status, planned.size, results[-1].error)
                )

            pending = [f for f in targets if f.action == FETCH]
            if pending:
                with ThreadPoolExecutor(max_workers=min(self.jobs, len(pending))) as pool:
                    futures = {
                        pool.submit(self._fetch_one, plan, f, root, reporter): f for f in pending
                    }
                    for future, planned in futures.items():
                        try:
                            result = future.result()
                        except Exception as exc:  # noqa: BLE001 - reported, never raised
                            result = FileResult(planned.rel, "failed", planned.size, str(exc))
                        results.append(result)
                        reporter.file_finished(
                            FileOutcome(result.rel, result.status, result.size, result.error)
                        )
        return results

    def fetch_all(self, plans: Iterable[TorrentPlan]) -> dict[str, list[FileResult]]:
        out: dict[str, list[FileResult]] = {}
        for plan in plans:
            self.console.section(f"→ {plan.identifier} ({plan.to_fetch} files to fetch)")
            out[plan.torrent.hash] = self.fetch_plan(plan)
        return out

    # ------------------------------------------------------------------ workers

    def _touch(self, root: Path, planned: PlannedFile) -> FileResult:
        """Create a zero-byte file that the torrent expects."""
        dest = planned.dest
        if not _within(root, dest):
            return FileResult(planned.rel, "failed", 0, "destination escapes save_path")
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                dest.touch()
            return FileResult(planned.rel, "created", 0)
        except OSError as exc:
            return FileResult(planned.rel, "failed", 0, f"cannot create empty file: {exc}")

    def _fetch_one(
        self, plan: TorrentPlan, planned: PlannedFile, root: Path, reporter: Reporter
    ) -> FileResult:
        started = time.monotonic()
        if not _within(root, planned.dest):
            return FileResult(
                planned.rel, "failed", planned.size, "destination escapes the torrent save_path"
            )
        reporter.file_started(planned.rel)
        part = planned.dest.with_name(planned.dest.name + PART_SUFFIX)
        resumed_from = 0
        last_error = ""

        for attempt in range(1, self.retries + 1):
            try:
                resumed_from = self._transfer(plan, planned, part, reporter)
                self._finalize(planned, part)
                return FileResult(
                    planned.rel,
                    "ok",
                    planned.size,
                    "",
                    time.monotonic() - started,
                    resumed_from,
                )
            except _Transient as exc:
                last_error = str(exc)
                self.console.detail(
                    f"retry {attempt}/{self.retries} for {planned.rel}: {last_error}"
                )
                if attempt < self.retries:
                    time.sleep(min(30.0, 2.0 * attempt))
            except PermissionError as exc:
                return FileResult(planned.rel, "failed", planned.size, f"permission denied: {exc}")
            except OSError as exc:
                return FileResult(planned.rel, "failed", planned.size, f"filesystem error: {exc}")
            except DownloadError as exc:
                # Verification failure: drop the suspect data so the next run
                # starts clean instead of resuming onto a poisoned part file.
                _unlink(part)
                return FileResult(planned.rel, "failed", planned.size, str(exc))

        return FileResult(
            planned.rel,
            "failed",
            planned.size,
            f"gave up after {self.retries} attempts: {last_error}",
        )

    def _transfer(
        self, plan: TorrentPlan, planned: PlannedFile, part: Path, reporter: Reporter
    ) -> int:
        """Stream one file into ``part``; returns the number of bytes resumed from."""
        expected = planned.size
        part.parent.mkdir(parents=True, exist_ok=True)

        offset = 0
        if part.exists():
            offset = part.stat().st_size
            if offset > expected:
                # Leftover from a different (stale) torrent revision.
                part.unlink()
                offset = 0
            elif offset == expected:
                return offset

        try:
            response = self.archive.open_file(plan.identifier, planned.item_path, offset=offset)
        except ArchiveHttpError as exc:
            if exc.code == 416:  # our range is past the end; start over
                _unlink(part)
                raise _Transient("range not satisfiable, restarting file") from exc
            if exc.code in (401, 403):
                raise DownloadError(
                    f"HTTP {exc.code} for '{planned.item_name}': not readable on archive.org"
                ) from exc
            if exc.code == 404:
                raise DownloadError(
                    f"HTTP 404 for '{planned.item_name}': the item no longer publishes this file"
                ) from exc
            raise _Transient(f"HTTP {exc.code} for '{planned.item_name}'") from exc
        except (urllib.error.URLError, TimeoutError, OSError, ArchiveOrgError) as exc:
            raise _Transient(f"connection failed: {exc}") from exc

        written = offset
        with response:
            status = getattr(response, "status", 200) or 200
            if offset and status != 206:
                # Server ignored the Range header: restart from zero.
                offset = 0
                written = 0
                mode = "wb"
            else:
                mode = "ab" if offset else "wb"

            # Fail before transferring a single byte when the source no longer
            # holds what this torrent records. archive.org regenerates its
            # metadata files, so a torrent made earlier can name a size the item
            # no longer serves — and no amount of downloading reproduces it.
            announced = _announced_size(response, offset)
            if announced is not None and announced != expected:
                _unlink(part)
                raise DownloadError(_changed_at_source(planned, expected, announced))

            with open(part, mode) as handle:
                while True:
                    try:
                        chunk = response.read(CHUNK_SIZE)
                    except (TimeoutError, OSError, http.client.HTTPException) as exc:
                        # A connection that dies mid-body raises http.client errors
                        # (IncompleteRead, BadStatusLine...). For a multi-gigabyte
                        # file that is a blip to retry, not a reason to give up.
                        raise _Transient(f"read failed after {written} bytes: {exc}") from exc
                    if not chunk:
                        break
                    handle.write(chunk)
                    written += len(chunk)
                    reporter.add_bytes(len(chunk))
                    self.limiter.consume(len(chunk))

        if written > expected:
            # The source is longer than the torrent says. Unlike a truncated
            # transfer, no retry can help: the bytes wanted are gone, and leaving
            # the oversized file behind would only litter the save_path.
            _unlink(part)
            raise DownloadError(_changed_at_source(planned, expected, written))
        if written != expected:
            raise _Transient(f"got {written} of {expected} bytes")
        return offset

    def _finalize(self, planned: PlannedFile, part: Path) -> None:
        """Verify the completed part file, then move it into place."""
        if self.verify and planned.md5:
            actual = md5_file(part)
            if actual != planned.md5:
                _unlink(part)
                raise DownloadError(
                    "md5 mismatch after download "
                    f"(expected {planned.md5}, got {actual}). The item's file or this "
                    "torrent has changed — re-add the current .torrent from archive.org"
                )
        planned.dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(part, planned.dest)


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:  # pragma: no cover - best effort
        pass
