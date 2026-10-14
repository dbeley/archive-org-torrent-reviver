"""Orchestration: find the dead archive.org torrents, fill them, make them seed.

The sequence for one torrent is:

1. pause it, so qBittorrent cannot write the same files while we do;
2. fetch from archive.org exactly the files it is missing;
3. force a recheck, so qBittorrent hashes what we wrote;
4. restart it and re-announce, so the tracker learns the swarm has a seed again.

Step 3 is the whole point: qBittorrent only becomes a seeder when its own piece
hashes confirm the data, so the tool never claims success on the strength of a
download alone.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from .archive import ArchiveOrg, ArchiveOrgError, ItemNotFound, ItemUnavailable
from .downloader import Downloader, FileResult
from .identify import ArchiveMatch, classify_torrent
from .planner import PlanError, TorrentPlan, build_plan, needs_help
from .progress import Console
from .qbittorrent import QbitClient, QbitError, Torrent
from .util import human_bytes

__all__ = [
    "Candidate",
    "collect_candidates",
    "build_plans",
    "ReviveOutcome",
    "revive_plan",
    "LockError",
    "torrent_lock",
]


@dataclass(slots=True)
class Candidate:
    """An archive.org torrent currently in qBittorrent."""

    torrent: Torrent
    match: ArchiveMatch

    @property
    def identifier(self) -> str:
        return self.match.identifier or self.torrent.name

    @property
    def needs_help(self) -> bool:
        return needs_help(self.torrent)

    @property
    def dead(self) -> bool:
        """No seeder registered on the tracker (``-1`` means "unknown")."""
        return self.torrent.num_complete == 0

    @property
    def flag(self) -> str:
        if not self.torrent.has_metadata:
            return "no-metadata"
        if self.torrent.is_complete:
            # qBittorrent's *UP states mean the torrent is complete; only the
            # actively-serving ones are actually uploading to peers.
            seeding = {"uploading", "forcedUP", "queuedUP", "stalledUP"}
            return "seeding" if self.torrent.state in seeding else "complete"
        if self.dead:
            return "dead"
        return "stalled"


def collect_candidates(
    client: QbitClient, *, needing_only: bool = False, include_complete: bool = True
) -> list[Candidate]:
    """Every archive.org torrent qBittorrent knows about.

    Detection uses the torrent's comment and its trackers (see
    :mod:`ia_reviver.identify`), both of which are already present in
    ``torrents/info`` — so this costs exactly one API call.
    """
    out: list[Candidate] = []
    for torrent in client.torrents():
        match = classify_torrent(torrent)
        if match is None:
            continue
        candidate = Candidate(torrent=torrent, match=match)
        if needing_only and not candidate.needs_help:
            continue
        if not include_complete and candidate.torrent.is_complete:
            continue
        out.append(candidate)
    out.sort(key=lambda c: (not c.needs_help, c.identifier.lower()))
    return out


def build_plans(
    client: QbitClient,
    archive: ArchiveOrg,
    candidates: list[Candidate],
    *,
    verify: bool = True,
    force: bool = False,
    console: Console | None = None,
) -> tuple[list[TorrentPlan], list[tuple[Candidate, str]]]:
    """Build plans for every candidate; returns ``(plans, failures)``.

    A failing candidate never aborts the run: the reason is recorded and the
    remaining torrents are still planned.
    """
    console = console or Console()
    plans: list[TorrentPlan] = []
    failures: list[tuple[Candidate, str]] = []

    def failed(candidate: Candidate, reason: str) -> None:
        failures.append((candidate, reason))
        console.progress_done()
        console.warn(f"{candidate.identifier}: {reason}")

    total = len(candidates)
    for index, candidate in enumerate(candidates, start=1):
        # Each candidate costs at least one archive.org round-trip, so show the
        # counter: over dozens of torrents this phase runs for minutes.
        console.progress(index, total, candidate.identifier)
        try:
            plan = build_plan(client, archive, candidate.torrent, verify=verify, force=force)
        except (ItemUnavailable, ItemNotFound, PlanError, ArchiveOrgError, QbitError) as exc:
            failed(candidate, str(exc))
        else:
            plans.append(plan)
    console.progress_done()
    return plans, failures


# --------------------------------------------------------------------- locking


class LockError(RuntimeError):
    """Another process is already working on this torrent."""


class torrent_lock:
    """Best-effort exclusive lock per torrent, so two runs cannot interleave.

    A stale lock (whose owning PID is gone) is taken over automatically —
    otherwise a killed run would permanently block that torrent.
    """

    def __init__(self, state_dir: Path, torrent_hash: str) -> None:
        self.path = state_dir / "locks" / f"{torrent_hash}.lock"
        self._owned = False

    def __enter__(self) -> torrent_lock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                with os.fdopen(fd, "w") as handle:
                    handle.write(str(os.getpid()))
                self._owned = True
                return self
            except FileExistsError:
                if self._stale():
                    with contextlib.suppress(OSError):
                        self.path.unlink()
                    continue
                raise LockError(
                    f"another ia-reviver run holds the lock {self.path}; "
                    "wait for it to finish or delete the file if it is stale"
                ) from None
        raise LockError(f"could not acquire lock {self.path}")

    def _stale(self) -> bool:
        try:
            pid = int(self.path.read_text().strip() or 0)
        except (OSError, ValueError):
            return True
        if pid <= 0:
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:  # pragma: no cover - alive, owned by another user
            return False
        return False

    def __exit__(self, *_exc) -> None:
        if self._owned:
            with contextlib.suppress(OSError):
                self.path.unlink()
            self._owned = False


# ---------------------------------------------------------------------- revive


@dataclass(slots=True)
class ReviveOutcome:
    """What actually happened to one torrent."""

    plan: TorrentPlan
    identifier: str
    results: list[FileResult] = field(default_factory=list)
    fetched_bytes: int = 0
    failed_files: int = 0
    progress_before: float = 0.0
    progress_after: float = 0.0
    state_after: str = ""
    seeders_after: int = -1
    completed: bool = False
    error: str = ""
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return self.completed and not self.error

    @property
    def seeding(self) -> bool:
        """Complete *and* the client is actually serving it.

        A torrent left stopped is complete but reaches nobody, so counts must not
        be built on ``ok`` alone — that would report a paused torrent as seeding.
        """
        if not self.ok:
            return False
        return self.state_after not in {"", "stoppedUP", "pausedUP"}

    @property
    def summary(self) -> str:
        if self.error:
            return f"failed: {self.error}"
        if self.completed:
            # Be precise about "seeding": a torrent left stopped is complete but
            # not yet serving anyone, and saying otherwise would overstate it.
            if self.state_after in {"stoppedUP", "pausedUP"}:
                return (
                    "revived — qBittorrent verified 100% of the data. The torrent is "
                    "complete but stopped (it was paused before this run); start it to seed"
                )
            return (
                f"revived — qBittorrent verified 100% of the data and is now seeding "
                f"(state: {self.state_after})"
            )
        if self.plan.changed_files:
            # Distinguish "archive.org no longer has these bytes" from a transfer
            # that simply failed: no retry, no other tool, and no later run will
            # fix this one, and calling it "partial" invites exactly that hope.
            changed = self.plan.changed_files
            shown = ", ".join(f.rel for f in changed[:3])
            extra = f" (+{len(changed) - 3} more)" if len(changed) > 3 else ""
            return (
                f"cannot be completed: {len(changed)} file(s) changed on archive.org "
                f"after this .torrent was made ({shown}{extra}) — their exact bytes no "
                "longer exist, so the recheck can never reach 100%"
            )
        return (
            f"partial: {self.progress_before * 100:.1f}% → "
            f"{self.progress_after * 100:.1f}% "
            f"({self.failed_files} file(s) could not be fetched)"
        )


def revive_plan(
    client: QbitClient,
    downloader: Downloader,
    plan: TorrentPlan,
    *,
    pause: bool = True,
    start: bool | None = None,
    recheck_timeout: float = 900.0,
    state_dir: Path | None = None,
    console: Console | None = None,
) -> ReviveOutcome:
    """Fill one torrent's missing files, recheck it, and leave it seeding."""
    console = console or Console()
    outcome = ReviveOutcome(plan=plan, identifier=plan.identifier)
    started = time.monotonic()
    outcome.progress_before = plan.torrent.progress
    was_paused = plan.torrent.is_paused
    lock = torrent_lock(state_dir, plan.torrent.hash) if state_dir is not None else _NullLock()

    with lock:
        try:
            if pause and not was_paused:
                console.detail(f"pausing {plan.identifier} before writing its files")
                client.stop([plan.torrent.hash])

            results = downloader.fetch_plan(plan)
            outcome.results = results
            outcome.fetched_bytes = sum(r.size for r in results if r.status == "ok")
            outcome.failed_files = sum(1 for r in results if r.status == "failed")

            for result in results:
                if result.status == "failed":
                    console.error(f"{plan.identifier}: {result.rel}: {result.error}")

            console.detail(f"rechecking {plan.identifier} ({plan.torrent.hash[:12]})")
            checked = client.then_check([plan.torrent.hash], timeout=recheck_timeout)
            after = checked.get(plan.torrent.hash)
            if after is not None:
                outcome.progress_after = after.progress
                outcome.state_after = after.state
                outcome.seeders_after = after.num_complete
                outcome.completed = after.progress >= 1.0

            should_start = (not was_paused) if start is None else start
            if should_start:
                client.start([plan.torrent.hash])
                # Re-read the state: a torrent we just started is no longer in the
                # post-recheck state, and reporting the stale one would misdescribe
                # the outcome (saying "stopped" for a torrent that is now seeding).
                try:
                    refreshed = client.torrent(plan.torrent.hash)
                except QbitError as exc:  # pragma: no cover - best effort
                    console.detail(f"could not re-read the torrent state: {exc}")
                else:
                    outcome.state_after = refreshed.state
                    outcome.progress_after = refreshed.progress
                    outcome.seeders_after = refreshed.num_complete
                    outcome.completed = refreshed.progress >= 1.0
                if outcome.completed:
                    # Tell the tracker immediately that this swarm has a seed.
                    try:
                        client.reannounce([plan.torrent.hash])
                    except QbitError as exc:  # pragma: no cover - cosmetic
                        console.detail(f"reannounce failed (harmless): {exc}")
            elif pause and not was_paused:
                console.detail("leaving the torrent stopped (it was paused before)")
        except (QbitError, ArchiveOrgError) as exc:
            outcome.error = str(exc)
        except OSError as exc:
            outcome.error = f"filesystem error: {exc}"

    outcome.elapsed = time.monotonic() - started
    return outcome


class _NullLock:
    """No-op context manager used when locking is disabled."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc) -> None:
        return None


def write_state(state_dir: Path, outcomes: list[ReviveOutcome]) -> Path | None:
    """Append a JSON record of the run, for auditing what the tool changed."""
    if not outcomes:
        return None
    directory = state_dir / "history"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "revived.jsonl"
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with open(path, "a", encoding="utf-8") as handle:
        for outcome in outcomes:
            handle.write(
                json.dumps(
                    {
                        "at": stamp,
                        "identifier": outcome.identifier,
                        "hash": outcome.plan.torrent.hash,
                        "info_hash": outcome.plan.local_info_hash,
                        "before": outcome.progress_before,
                        "after": outcome.progress_after,
                        "completed": outcome.completed,
                        "fetched_bytes": outcome.fetched_bytes,
                        "failed_files": outcome.failed_files,
                        "error": outcome.error,
                    }
                )
                + "\n"
            )
    return path


def format_outcome(console: Console, outcome: ReviveOutcome) -> str:
    """One-line, human, non-lying description of an outcome."""
    prefix = "✓" if outcome.ok else ("…" if not outcome.error else "✗")
    detail = f"{human_bytes(outcome.fetched_bytes)} fetched"
    return f"{prefix} {outcome.identifier}: {outcome.summary} [{detail}]"
