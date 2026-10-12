"""Minimal bencode decoder and the small slice of torrent parsing we need.

Only what matters for this tool: the ``info`` dict's file list and the SHA-1 of
the exact ``info`` bytes (the BitTorrent v1 info-hash). Written from scratch on
purpose — no third-party dependency, so the whole tool stays stdlib-only.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

__all__ = [
    "BencodeError",
    "TorrentMeta",
    "TorrentFile",
    "bdecode",
    "info_span",
    "infohash",
    "parse_torrent",
]


class BencodeError(ValueError):
    """Raised when a byte string is not valid bencode."""


def bdecode(data: bytes, i: int = 0) -> tuple[int, object]:
    """Decode one bencoded value at ``data[i:]``.

    Returns ``(index_after_value, value)``. Integers become ``int``, byte strings
    stay ``bytes``, lists become ``list`` and dictionaries become ``dict`` keyed
    by ``bytes``.
    """
    if i >= len(data):
        raise BencodeError("truncated bencode")
    char = data[i : i + 1]

    if char == b"i":
        end = data.find(b"e", i)
        if end < 0:
            raise BencodeError("unterminated integer")
        raw = data[i + 1 : end]
        try:
            return end + 1, int(raw)
        except ValueError as exc:  # pragma: no cover - malformed input only
            raise BencodeError(f"bad integer {raw!r}") from exc

    if char == b"l":
        i += 1
        out: list[object] = []
        while True:
            if i >= len(data):
                raise BencodeError("unterminated list")
            if data[i : i + 1] == b"e":
                return i + 1, out
            i, value = bdecode(data, i)
            out.append(value)

    if char == b"d":
        i += 1
        pairs: dict[bytes, object] = {}
        while True:
            if i >= len(data):
                raise BencodeError("unterminated dictionary")
            if data[i : i + 1] == b"e":
                return i + 1, pairs
            i, key = bdecode(data, i)
            if not isinstance(key, bytes):
                raise BencodeError("dictionary key is not a byte string")
            i, value = bdecode(data, i)
            pairs[key] = value

    if char.isdigit():
        colon = data.find(b":", i)
        if colon < 0:
            raise BencodeError("unterminated string length")
        raw_len = data[i:colon]
        try:
            length = int(raw_len)
        except ValueError as exc:
            raise BencodeError(f"bad string length {raw_len!r}") from exc
        start = colon + 1
        end = start + length
        if end > len(data):
            raise BencodeError("string runs past end of buffer")
        return end, data[start:end]

    raise BencodeError(f"unexpected byte {char!r} at offset {i}")


def info_span(data: bytes) -> tuple[int, int] | None:
    """Return the byte offsets of the top-level ``info`` value, if present.

    The info-hash is the SHA-1 of *these exact bytes*, so the span — not a
    re-encoding — is what must be hashed.
    """
    i = 1  # skip the opening 'd'
    if data[:1] != b"d":
        raise BencodeError("torrent does not start with a dictionary")
    while i < len(data) and data[i : i + 1] != b"e":
        i, key = bdecode(data, i)
        value_start = i
        i, _ = bdecode(data, i)
        if key == b"info":
            return value_start, i
    return None


def infohash(data: bytes) -> str:
    """SHA-1 info-hash (hex) of a raw ``.torrent`` payload."""
    span = info_span(data)
    if span is None:
        raise BencodeError("torrent has no info dictionary")
    return hashlib.sha1(data[span[0] : span[1]]).hexdigest()


def _text(raw: bytes) -> str:
    """Decode a torrent string for display / paths.

    ``surrogateescape`` keeps non-UTF-8 byte sequences round-trippable, which
    matters because torrent paths come from an arbitrary uploader and may not be
    valid UTF-8. The raw bytes are always kept alongside for URL building.
    """
    return raw.decode("utf-8", "surrogateescape")


@dataclass(slots=True)
class TorrentFile:
    """One entry of the torrent's ``info.files`` list."""

    path: str
    """Path inside the torrent, relative to ``TorrentMeta.name``, as a ``str``."""
    path_bytes: bytes
    """The same path as raw bytes (used to build exact download URLs)."""
    size: int

    @property
    def is_empty(self) -> bool:
        return self.size == 0


@dataclass(slots=True)
class TorrentMeta:
    """The parts of a ``.torrent`` that this tool acts on."""

    info_hash: str
    name: str
    name_bytes: bytes
    files: list[TorrentFile] = field(default_factory=list)
    piece_length: int = 0
    piece_count: int = 0
    announce: str = ""
    announce_list: list[list[str]] = field(default_factory=list)
    comment: str = ""
    url_list: list[str] = field(default_factory=list)
    created_by: str = ""
    is_private: bool = False
    single_file: bool = False

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def tracker_hosts(self) -> set[str]:
        """Every announce host, lowercased and without a port."""
        hosts: set[str] = set()
        urls = [self.announce] + [u for tier in self.announce_list for u in tier]
        for url in urls:
            if not url:
                continue
            host = url.split("://", 1)[-1].split("/", 1)[0]
            host = host.rsplit("@", 1)[-1]
            if host.startswith("["):  # IPv6 literal
                host = host.split("]", 1)[0].lstrip("[")
            else:
                host = host.split(":", 1)[0]
            if host:
                hosts.add(host.lower())
        return hosts


def _as_text_list(value: object) -> list[list[str]]:
    if not isinstance(value, list):
        return []
    tiers: list[list[str]] = []
    for tier in value:
        if isinstance(tier, list):
            tiers.append([_text(item) for item in tier if isinstance(item, bytes)])
    return tiers


def parse_torrent(data: bytes) -> TorrentMeta:
    """Parse a raw ``.torrent`` payload into a :class:`TorrentMeta`."""
    span = info_span(data)
    _, top = bdecode(data)
    if not isinstance(top, dict):  # pragma: no cover - defensive
        raise BencodeError("torrent root is not a dictionary")
    info = top.get(b"info")
    if not isinstance(info, dict):
        raise BencodeError("torrent has no info dictionary")
    if span is None:  # pragma: no cover - defensive, info_span just found it
        raise BencodeError("torrent has no info dictionary")

    name_bytes = info.get(b"name")
    if not isinstance(name_bytes, bytes):
        raise BencodeError("torrent info has no name")

    files: list[TorrentFile] = []
    single_file = False
    raw_files = info.get(b"files")
    if isinstance(raw_files, list):
        for entry in raw_files:
            if not isinstance(entry, dict):
                continue
            size = entry.get(b"length")
            if not isinstance(size, int):
                continue
            parts = entry.get(b"path")
            if not isinstance(parts, list) or not all(isinstance(p, bytes) for p in parts):
                continue
            path_bytes = b"/".join(parts)
            files.append(TorrentFile(_text(path_bytes), path_bytes, size))
    elif isinstance(info.get(b"length"), int):
        single_file = True
        files.append(TorrentFile(_text(name_bytes), name_bytes, int(info[b"length"])))

    pieces = info.get(b"pieces")
    piece_count = len(pieces) // 20 if isinstance(pieces, bytes) else 0
    piece_length = info.get(b"piece length")
    comment = top.get(b"comment")
    created_by = top.get(b"created by")
    announce = top.get(b"announce")
    url_list = top.get(b"url-list")
    if isinstance(url_list, bytes):  # BEP-19 allows a bare string
        urls = [_text(url_list)]
    elif isinstance(url_list, list):
        urls = [_text(u) for u in url_list if isinstance(u, bytes)]
    else:
        urls = []

    return TorrentMeta(
        info_hash=hashlib.sha1(data[span[0] : span[1]]).hexdigest(),
        name=_text(name_bytes),
        name_bytes=name_bytes,
        files=files,
        piece_length=piece_length if isinstance(piece_length, int) else 0,
        piece_count=piece_count,
        announce=_text(announce) if isinstance(announce, bytes) else "",
        announce_list=_as_text_list(top.get(b"announce-list")),
        comment=_text(comment) if isinstance(comment, bytes) else "",
        url_list=urls,
        created_by=_text(created_by) if isinstance(created_by, bytes) else "",
        is_private=info.get(b"private") == 1,
        single_file=single_file,
    )
