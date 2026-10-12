"""Thin client for the parts of archive.org this tool needs.

Every byte a torrent describes is still served by archive.org over plain HTTP at
``https://archive.org/download/<identifier>/<path>``. That is the whole trick: a
torrent with zero seeders is not unreachable, it is just unreachable *over
BitTorrent*.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .bencode import BencodeError, TorrentMeta, parse_torrent

__all__ = [
    "ArchiveOrgError",
    "ArchiveHttpError",
    "ItemNotFound",
    "ItemUnavailable",
    "ItemFile",
    "ArchiveOrg",
    "DEFAULT_BASE_URL",
]

DEFAULT_BASE_URL = "https://archive.org"
DEFAULT_USER_AGENT = "ia-reviver/0.1 (+https://github.com/dbeley/archive-org-torrent-reviver)"
DETAILS_HOSTS = ("archive.org", "www.archive.org")


class ArchiveOrgError(RuntimeError):
    """Any failure talking to archive.org."""


class ArchiveHttpError(ArchiveOrgError):
    """An HTTP error status from archive.org, with the status preserved.

    Callers need to tell a definitive 404 ("this file is gone") from a transient
    5xx ("try again"), so the status code must survive the retry layer.
    """

    def __init__(self, url: str, code: int, *, attempts: int = 1) -> None:
        suffix = f" (after {attempts} attempts)" if attempts > 1 else ""
        super().__init__(f"GET {url} returned HTTP {code}{suffix}")
        self.url = url
        self.code = code
        self.attempts = attempts


class ItemNotFound(ArchiveOrgError):
    """The identifier does not resolve to an item."""


class ItemUnavailable(ArchiveOrgError):
    """The item exists but cannot be served (restricted / stream-only)."""


@dataclass(slots=True)
class ItemFile:
    """One file of an archive.org item, as described by its metadata."""

    name: str
    size: int
    md5: str | None = None
    sha1: str | None = None
    format: str | None = None


def is_archive_host(host: str) -> bool:
    """True for ``archive.org`` and its sub-hosts (``bt1.``, ``ia601503.us.``…)."""
    host = host.lower().rstrip(".")
    return host == "archive.org" or host.endswith(".archive.org")


class ArchiveOrg:
    """Read-only archive.org client with retries and backoff."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = 60.0,
        retries: int = 4,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = max(1, retries)
        self.user_agent = user_agent
        self._metadata_cache: dict[str, dict[str, Any]] = {}
        self._torrent_cache: dict[str, TorrentMeta] = {}

    # ------------------------------------------------------------------ plumbing

    def _request(self, url: str, *, headers: dict[str, str] | None = None):
        """GET ``url`` with retries on transient failures.

        archive.org returns sporadic 5xx (and the occasional 429) under load, so
        a single failure must never be reported as "file missing".
        """
        last: Exception | None = None
        for attempt in range(self.retries):
            req_headers = {"User-Agent": self.user_agent, "Accept": "*/*"}
            if headers:
                req_headers.update(headers)
            try:
                return urllib.request.urlopen(
                    urllib.request.Request(url, headers=req_headers), timeout=self.timeout
                )
            except urllib.error.HTTPError as exc:
                last = exc
                # 4xx other than 429 are definitive: do not retry, do not mask.
                if exc.code == 429 or 500 <= exc.code < 600:
                    # Release the response before backing off, and on the final
                    # attempt too: otherwise the exception object keeps an open
                    # socket alive until it is garbage collected.
                    exc.close()
                    if attempt == self.retries - 1:
                        break
                    delay = min(30.0, 2.0 * (3**attempt))
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    if retry_after and retry_after.isdigit():
                        delay = min(60.0, float(retry_after))
                    time.sleep(delay)
                    continue
                exc.close()
                raise ArchiveHttpError(url, exc.code) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = exc
                if attempt == self.retries - 1:
                    break
                time.sleep(min(30.0, 1.0 * (2**attempt)))
        assert last is not None
        if isinstance(last, urllib.error.HTTPError):
            raise ArchiveHttpError(url, last.code, attempts=self.retries) from last
        raise ArchiveOrgError(f"GET {url} failed after {self.retries} attempts: {last}") from last

    def _get_json(self, url: str) -> dict[str, Any]:
        response = self._request(url)
        with response:
            payload = response.read()
        try:
            data = json.loads(payload.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            raise ArchiveOrgError(f"{url} returned invalid JSON") from exc
        return data if isinstance(data, dict) else {}

    def _get_bytes(self, url: str) -> bytes:
        response = self._request(url)
        with response:
            return response.read()

    # ------------------------------------------------------------------ metadata

    def metadata(self, identifier: str, *, refresh: bool = False) -> dict[str, Any]:
        """Raw ``/metadata/<identifier>`` payload.

        Note: a mistyped identifier returns an *empty* document rather than an
        error, so an empty result means "no such item", not "no metadata".
        """
        if not refresh and identifier in self._metadata_cache:
            return self._metadata_cache[identifier]
        url = f"{self.base_url}/metadata/{urllib.parse.quote(identifier, safe='')}"
        data = self._get_json(url)
        self._metadata_cache[identifier] = data
        return data

    def item_info(self, identifier: str, *, refresh: bool = False) -> dict[str, Any]:
        """Validated metadata, raising for missing or unservable items."""
        data = self.metadata(identifier, refresh=refresh)
        meta = data.get("metadata")
        if not data or not meta:
            raise ItemNotFound(
                f"archive.org has no item '{identifier}' "
                "(an empty /metadata response means the identifier does not exist)"
            )
        if (
            str(meta.get("access-restricted-item", "")).lower() == "true"
            or str(meta.get("stream_only", "")).lower() == "true"
        ):
            raise ItemUnavailable(
                f"item '{identifier}' is access-restricted or stream-only; "
                "its files cannot be downloaded or seeded"
            )
        if str(meta.get("is_dark", "")).lower() == "true":
            raise ItemUnavailable(f"item '{identifier}' is dark (removed or hidden)")
        return data

    def item_files(self, identifier: str, *, refresh: bool = False) -> dict[str, ItemFile]:
        """Item manifest keyed by file name, as served by archive.org."""
        data = self.item_info(identifier, refresh=refresh)
        out: dict[str, ItemFile] = {}
        for entry in data.get("files", []):
            name = entry.get("name")
            if not isinstance(name, str):
                continue
            try:
                size = int(entry.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            out[name] = ItemFile(
                name=name,
                size=size,
                md5=entry.get("md5") or None,
                sha1=entry.get("sha1") or None,
                format=entry.get("format") or None,
            )
        return out

    def torrent_file_name(self, identifier: str, *, refresh: bool = False) -> str:
        """Name of the item's ``.torrent`` file."""
        data = self.item_info(identifier, refresh=refresh)
        for entry in data.get("files", []):
            name = entry.get("name")
            if isinstance(name, str) and name.endswith(".torrent"):
                return name
        # Fall back to the documented convention; some items omit it from metadata.
        return f"{identifier}_archive.torrent"

    # ------------------------------------------------------------------ torrent

    def torrent_bytes(self, identifier: str, *, refresh: bool = False) -> bytes:
        """Download the item's ``.torrent`` payload."""
        name = self.torrent_file_name(identifier, refresh=refresh)
        quoted_id = urllib.parse.quote(identifier, safe="")
        quoted_name = urllib.parse.quote(name)
        url = f"{self.base_url}/download/{quoted_id}/{quoted_name}"
        try:
            return self._get_bytes(url)
        except ArchiveHttpError as exc:
            if exc.code in (401, 403):
                raise ItemUnavailable(
                    f"downloading the torrent for '{identifier}' returned HTTP {exc.code}: "
                    "the item is restricted"
                ) from exc
            if exc.code == 404:
                raise ItemNotFound(f"item '{identifier}' does not publish '{name}'") from exc
            raise

    def torrent(self, identifier: str, *, refresh: bool = False) -> TorrentMeta:
        """Parsed ``.torrent`` for an item, cached."""
        if not refresh and identifier in self._torrent_cache:
            return self._torrent_cache[identifier]
        raw = self.torrent_bytes(identifier, refresh=refresh)
        try:
            meta = parse_torrent(raw)
        except BencodeError as exc:
            raise ArchiveOrgError(f"item '{identifier}' serves an invalid .torrent: {exc}") from exc
        self._torrent_cache[identifier] = meta
        return meta

    # ------------------------------------------------------------------ downloads

    def download_url(self, identifier: str, path: bytes | str) -> str:
        """Exact HTTP URL for one item file.

        Works from the raw byte path so that non-UTF-8 file names still produce a
        correct request, and so ``/`` separators stay literal while everything
        else (spaces, ``#``, ``?``, ``+``) is percent-encoded.
        """
        raw = path.encode("utf-8", "surrogateescape") if isinstance(path, str) else path
        quoted = urllib.parse.quote_from_bytes(raw, safe="/")
        return f"{self.base_url}/download/{urllib.parse.quote(identifier, safe='')}/{quoted}"

    def open_file(self, identifier: str, path: bytes | str, *, offset: int = 0):
        """Open a ranged GET on one item file.

        The caller owns the returned response. ``offset > 0`` asks for the tail
        and is used to resume an interrupted download.
        """
        headers = {"Range": f"bytes={offset}-"} if offset > 0 else None
        return self._request(self.download_url(identifier, path), headers=headers)

    def item_size(self, identifier: str) -> int:
        try:
            return int(self.item_info(identifier).get("item_size") or 0)
        except (ArchiveOrgError, TypeError, ValueError):
            return 0
