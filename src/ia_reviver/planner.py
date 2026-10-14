"""Work out, per torrent, which bytes are missing and where they must land.

Nothing here touches the network for bulk data and nothing writes to disk: the
plan is a pure description that the CLI shows for confirmation, so a user can
see "this will pull 12.4 GiB" before committing to it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .archive import ArchiveOrg, ArchiveOrgError  # noqa: F401  (re-exported for callers)
from .bencode import TorrentMeta
from .identify import ArchiveMatch, classify_torrent
from .qbittorrent import QbitClient, Torrent
from .util import md5_file, safe_relative_path

__all__ = [
    "HAVE",
    "FETCH",
    "TOUCH",
    "UNAVAILABLE",
    "CHANGED",
    "PlanError",
    "NotArchiveTorrent",
    "PlannedFile",
    "TorrentPlan",
    "needs_help",
    "build_plan",
    "describe_selection",
]

HAVE = "have"
FETCH = "fetch"
TOUCH = "touch"
UNAVAILABLE = "unavailable"
CHANGED = "changed"


class PlanError(RuntimeError):
    """The plan for a torrent cannot be built safely."""


class NotArchiveTorrent(PlanError):
    """The torrent did not come from archive.org."""


@dataclass(slots=True)
class PlannedFile:
    """One file of the torrent, with the action that will be taken on it."""

    rel: str
    """Path relative to the torrent's ``save_path``, as qBittorrent reports it."""
    dest: Path
    size: int
    item_name: str
    """Path of the same file inside the archive.org item."""
    item_path: bytes
    """Raw bytes of ``item_name``, used to build an exact download URL."""
    md5: str | None = None
    action: str = FETCH
    note: str = ""
    qbt_progress: float = 0.0

    @property
    def needs_download(self) -> bool:
        return self.action == FETCH

    @property
    def is_obtainable(self) -> bool:
        """False when archive.org cannot supply the bytes this torrent needs."""
        return self.action not in (UNAVAILABLE, CHANGED)


@dataclass(slots=True)
class TorrentPlan:
    """Everything needed to revive one torrent."""

    torrent: Torrent
    identifier: str
    root: str
    files: list[PlannedFile] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stale: bool = False
    local_info_hash: str = ""
    item_info_hash: str | None = None
    match: ArchiveMatch | None = None

    @property
    def fetch_files(self) -> list[PlannedFile]:
        return [f for f in self.files if f.action == FETCH]

    @property
    def touch_files(self) -> list[PlannedFile]:
        return [f for f in self.files if f.action == TOUCH]

    @property
    def unavailable_files(self) -> list[PlannedFile]:
        return [f for f in self.files if f.action == UNAVAILABLE]

    @property
    def changed_files(self) -> list[PlannedFile]:
        """Files archive.org still publishes, but at a different size than recorded."""
        return [f for f in self.files if f.action == CHANGED]

    @property
    def unobtainable_files(self) -> list[PlannedFile]:
        """Files whose exact bytes archive.org cannot supply any more."""
        return self.unavailable_files + self.changed_files

    @property
    def to_fetch_bytes(self) -> int:
        return sum(f.size for f in self.fetch_files)

    @property
    def to_fetch(self) -> int:
        return len(self.fetch_files)

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def revivable(self) -> bool:
        """True when every file can be supplied from archive.org."""
        return not self.unobtainable_files and bool(self.files)

    @property
    def memory_summary(self) -> str:
        """One-line description used in tables and confirmation prompts."""
        return (
            f"{self.to_fetch}/{len(self.files)} files, "
            f"{self.to_fetch_bytes / 1024 / 1024:.1f} MiB to fetch"
        )


def needs_help(torrent: Torrent) -> bool:
    """True when archive.org could give this torrent data it does not have."""
    if not torrent.has_metadata:
        return True
    if torrent.state in {"missingFiles", "error"}:
        return True
    return torrent.progress < 1.0


def _candidates(rel: str, root: str) -> list[str]:
    """Possible item-relative names for a qBittorrent file path.

    qBittorrent reports multi-file torrents as ``<root>/<path>`` but can be
    configured not to create the root folder, so both shapes are tried.
    """
    out: list[str] = []
    if root and rel == root:
        out.append("")
    if root and rel.startswith(root + "/"):
        out.append(rel[len(root) + 1 :])
    out.append(rel)
    return [c for c in out if c]


def _resolve_item_name(rel: str, root: str, known: set[str]) -> str | None:
    """Map a qBittorrent path to an item path, preferring a name the item knows."""
    candidates = _candidates(rel, root)
    for candidate in candidates:
        if candidate in known:
            return candidate
    return candidates[0] if candidates else None


def _strip_root(rel: str, root: str) -> str:
    """Best-effort removal of the torrent's root folder from a reported path."""
    candidates = _candidates(rel, root)
    return candidates[0] if candidates else rel


def _file_set(meta: TorrentMeta) -> dict[str, int]:
    return {f.path: f.size for f in meta.files}


def _describe_file_diff(local: dict[str, int], item: dict[str, int], limit: int = 4) -> str:
    parts: list[str] = []
    missing_on_item = [name for name in local if name not in item]
    missing_locally = [name for name in item if name not in local]
    differing = [name for name in local if name in item and local[name] != item[name]]
    for label, names in (
        ("only in your torrent", missing_on_item),
        ("only in the item's torrent", missing_locally),
        ("size differs", differing),
    ):
        if names:
            shown = ", ".join(names[:limit])
            extra = f" (+{len(names) - limit} more)" if len(names) > limit else ""
            parts.append(f"{label}: {shown}{extra}")
    return "; ".join(parts)


def build_plan(
    client: QbitClient,
    archive: ArchiveOrg,
    torrent: Torrent,
    *,
    verify: bool = True,
    force: bool = False,
) -> TorrentPlan:
    """Build the fetch plan for one torrent.

    Raises :class:`NotArchiveTorrent` when the torrent is not from archive.org,
    and other :class:`PlanError` subclasses when the item cannot serve the data.
    """
    match = classify_torrent(torrent)
    if match is None:
        raise NotArchiveTorrent(f"'{torrent.name}' does not look like an archive.org torrent")

    identifier = match.identifier or torrent.name
    plan = TorrentPlan(
        torrent=torrent,
        identifier=identifier,
        root=torrent.name,
        local_info_hash=torrent.hash,
        match=match,
    )

    item_torrent = archive.torrent(identifier)
    plan.item_info_hash = item_torrent.info_hash
    if not match.identifier:
        plan.warnings.append(
            f"identifier '{identifier}' was taken from the torrent name, not from an "
            "archive.org comment URL"
        )

    qbt_files = client.files(torrent.hash)
    if not qbt_files:
        raise PlanError(f"qBittorrent reports no files for '{torrent.name}' yet")

    item_files = archive.item_files(identifier)

    # --- Is the local torrent still the one archive.org serves? -------------
    root = item_torrent.name or torrent.name
    plan.root = root
    local_set = {
        stripped: size
        for f in qbt_files
        if (size := int(f.get("size") or 0)) >= 0
        and (stripped := _strip_root(str(f.get("name", "")), root))
    }
    if item_torrent.info_hash != torrent.hash:
        if local_set == _file_set(item_torrent):
            plan.warnings.append(
                "the info-hash differs from the item's current torrent, but the file "
                "list and sizes match; filling from archive.org should still work"
            )
        else:
            plan.stale = True
            plan.warnings.append(
                "your local torrent is STALE — archive.org now serves a different "
                f"torrent for '{identifier}'. Differences: "
                f"{_describe_file_diff(local_set, _file_set(item_torrent))}"
            )
            if not force:
                plan.warnings.append(
                    "the recheck would fail on the stale pieces; re-add the current "
                    "torrent from https://archive.org/download/"
                    f"{identifier}/{identifier}_archive.torrent, or pass --force"
                )

    # --- Per-file decisions -------------------------------------------------
    known = set(_file_set(item_torrent)) | set(item_files)
    save_path = Path(torrent.save_path or ".")
    for entry in qbt_files:
        rel_raw = str(entry.get("name", ""))
        size = int(entry.get("size") or 0)
        qbt_progress = float(entry.get("progress") or 0.0)

        rel, unsafe_reason = safe_relative_path(rel_raw)
        if rel is None:
            plan.files.append(
                PlannedFile(
                    rel=rel_raw,
                    dest=save_path,
                    size=size,
                    item_name="",
                    item_path=b"",
                    action=UNAVAILABLE,
                    note=f"unsafe path rejected: {unsafe_reason}",
                    qbt_progress=qbt_progress,
                )
            )
            continue

        item_name = _resolve_item_name(rel, root, known) or rel
        item_path = item_name.encode("utf-8", "surrogateescape")
        manifest = item_files.get(item_name)
        dest = save_path / rel
        planned = PlannedFile(
            rel=rel,
            dest=dest,
            size=size,
            item_name=item_name,
            item_path=item_path,
            md5=manifest.md5 if manifest else None,
            qbt_progress=qbt_progress,
        )

        on_disk = dest.is_file()
        disk_size = dest.stat().st_size if on_disk else -1

        if manifest is not None and manifest.size not in (0, size) and size != 0:
            planned.note = (
                f"size on archive.org is {manifest.size} but the torrent expects {size}; "
                "the item has changed since this torrent was made"
            )

        if size == 0:
            planned.action = HAVE if on_disk else TOUCH
            if not on_disk:
                planned.note = "empty file, created locally"
        elif on_disk and disk_size == size:
            if not verify or not planned.md5:
                planned.action = HAVE
                planned.note = "present (size match, not verified)" if verify else "present"
            elif md5_file(dest) == planned.md5:
                planned.action = HAVE
                planned.note = "present (md5 verified)"
            else:
                planned.action = FETCH
                planned.note = "present but md5 mismatch — will re-download"
        elif manifest is None:
            planned.action = UNAVAILABLE
            planned.note = planned.note or "not published on archive.org"
            plan.warnings.append(f"'{item_name}' is not in the item's file list")
        elif manifest.size != size:
            # archive.org still publishes the file, but not at the size this
            # torrent records: the item changed after the .torrent was created,
            # so the bytes this torrent needs no longer exist anywhere. A
            # download cannot fix that — it would transfer the wrong data,
            # leave a wrong-sized file behind, and the recheck could never
            # reach 100%. Say so instead of queueing a transfer doomed to fail.
            planned.action = CHANGED
            planned.note = (
                f"archive.org serves {manifest.size} B where this torrent records "
                f"{size} B — changed after the .torrent was made"
            )
            plan.warnings.append(
                f"'{item_name}' changed on archive.org ({size} B → {manifest.size} B); "
                "this torrent cannot be completed by filling it from archive.org"
            )
        else:
            planned.action = FETCH

        plan.files.append(planned)

    return plan


def describe_selection(plans: list[TorrentPlan]) -> str:
    """Multi-line human summary of a set of plans."""
    lines: list[str] = []
    for plan in plans:
        if plan.stale:
            state = "stale"
        elif plan.changed_files or plan.unavailable_files:
            state = f"{len(plan.unobtainable_files)} file(s) gone from the source"
        else:
            state = "ok"
        lines.append(
            f"  {plan.identifier}  [{state}]  {len(plan.files)} files, "
            f"{plan.to_fetch} to fetch "
            f"({plan.to_fetch_bytes / 1024 / 1024:.1f} MiB)"
        )
    return "\n".join(lines)
