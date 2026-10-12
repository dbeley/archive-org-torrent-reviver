"""Decide whether a qBittorrent torrent came from archive.org, and for which item.

Two independent signals are used because neither is universal:

1. The torrent's ``comment``. archive.org writes a machine-readable one:
   ``This content hosted at the Internet Archive at https://archive.org/details/<id>``.
   When present this is authoritative — the identifier is written, not guessed.
2. The announce/tracker URLs. Every archive.org torrent announces to
   ``bt1.archive.org:6969`` / ``bt2.archive.org:6969``. This catches torrents
   whose comment was stripped, retagged, or re-created by a third party.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

from .archive import is_archive_host
from .qbittorrent import Torrent

__all__ = [
    "ArchiveMatch",
    "DETAILS_RE",
    "archive_identifier_from_text",
    "classify_torrent",
    "tracker_hosts",
]

# Item identifiers are [A-Za-z0-9._-]; '/' must not be swallowed, and a bare
# "details/" (no id) must not match.
DETAILS_RE = re.compile(
    r"archive\.org\s*/\s*details\s*/\s*([A-Za-z0-9][A-Za-z0-9._-]*)",
    re.IGNORECASE,
)


@dataclass(slots=True)
class ArchiveMatch:
    """Why a torrent was considered an archive.org torrent."""

    identifier: str | None
    """Item identifier, when it could be read without guessing."""
    sources: list[str]
    hosts: list[str]

    @property
    def confident(self) -> bool:
        """True when the identifier came from an explicit details URL."""
        return "comment" in self.sources and self.identifier is not None


def archive_identifier_from_text(text: str) -> str | None:
    """Pull the identifier out of an archive.org ``details`` URL in ``text``."""
    if not text:
        return None
    match = DETAILS_RE.search(text)
    return match.group(1) if match else None


def tracker_hosts(torrent: Torrent) -> list[str]:
    """Tracker host names, from the live tracker field and from ``magnet_uri``."""
    hosts: list[str] = []
    urls = [torrent.tracker, *torrent.tracker_urls]
    for url in urls:
        if not url or url.startswith("**"):
            continue
        host = urllib.parse.urlparse(url).hostname
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def classify_torrent(torrent: Torrent) -> ArchiveMatch | None:
    """Return an :class:`ArchiveMatch` if the torrent looks archive.org-origin."""
    sources: list[str] = []
    identifier = archive_identifier_from_text(torrent.comment)
    if identifier:
        sources.append("comment")
    if not identifier:
        for label, candidate in (
            ("magnet_uri", torrent.magnet_uri),
            ("content_path", torrent.content_path),
            ("name", torrent.name),
        ):
            found = archive_identifier_from_text(candidate)
            if found:
                identifier = found
                sources.append(label)
                break

    hosts = tracker_hosts(torrent)
    archive_hosts = [h for h in hosts if is_archive_host(h)]
    if archive_hosts:
        sources.append("tracker")
    elif "archive.org" in torrent.magnet_uri:
        # Web seed URLs are not trackers but are equally diagnostic.
        archive_hosts = ["archive.org"]
        sources.append("webseed")
    elif "archive.org" in torrent.comment:
        sources.append("comment-text")

    if not sources:
        return None
    return ArchiveMatch(identifier=identifier, sources=sources, hosts=archive_hosts)
