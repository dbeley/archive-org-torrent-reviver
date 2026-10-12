"""Shared test doubles: a fake qBittorrent WebUI and a fixture-backed archive.org.

The fake qBittorrent server deliberately reproduces the behaviours that a naive
client gets wrong, so the tests fail if the client regresses:

* ``POST /api/v2/auth/login`` answers **204 with an empty body** (qBittorrent 5.x)
  rather than ``200 "Ok."``.
* The session cookie is ``QBT_SID_<port>``, not ``SID``.
* Anything other than the login endpoint answers **403** without a valid session.
"""

from __future__ import annotations

import http.server
import json
import threading
import urllib.parse
from pathlib import Path
from typing import Any

from ia_reviver.archive import ItemFile
from ia_reviver.bencode import TorrentMeta, parse_torrent

DATA = Path(__file__).resolve().parent / "data"
FIXTURE_IDENTIFIER = "ramrakshastotram"
FIXTURE_TORRENT = DATA / f"{FIXTURE_IDENTIFIER}.torrent"
FIXTURE_METADATA = DATA / f"{FIXTURE_IDENTIFIER}_metadata.json"


def fixture_torrent_bytes() -> bytes:
    return FIXTURE_TORRENT.read_bytes()


def fixture_torrent() -> TorrentMeta:
    return parse_torrent(fixture_torrent_bytes())


def fixture_item_files() -> dict[str, ItemFile]:
    payload = json.loads(FIXTURE_METADATA.read_text())
    out: dict[str, ItemFile] = {}
    for entry in payload["files"]:
        out[entry["name"]] = ItemFile(
            name=entry["name"],
            size=int(entry.get("size") or 0),
            md5=entry.get("md5"),
            sha1=entry.get("sha1"),
            format=entry.get("format"),
        )
    return out


class FakeArchive:
    """Stand-in for :class:`ia_reviver.archive.ArchiveOrg` backed by the fixture."""

    def __init__(
        self,
        *,
        identifier: str = FIXTURE_IDENTIFIER,
        torrent: TorrentMeta | None = None,
        item_files: dict[str, ItemFile] | None = None,
        base_url: str = "https://archive.org",
    ) -> None:
        self.identifier = identifier
        self._torrent = torrent if torrent is not None else fixture_torrent()
        self._files = item_files if item_files is not None else fixture_item_files()
        self.base_url = base_url
        self.downloads: list[tuple[str, str, int]] = []

    def torrent(self, identifier: str, *, refresh: bool = False) -> TorrentMeta:
        return self._torrent

    def item_files(self, identifier: str, *, refresh: bool = False) -> dict[str, ItemFile]:
        return self._files

    def download_url(self, identifier: str, path: bytes | str) -> str:
        raw = path.encode() if isinstance(path, str) else path
        quoted = urllib.parse.quote_from_bytes(raw, safe="/")
        return f"{self.base_url}/download/{identifier}/{quoted}"


def qbt_file(name: str, size: int, progress: float = 0.0) -> dict[str, Any]:
    """One entry shaped like ``GET /api/v2/torrents/files``."""
    return {"name": name, "size": size, "progress": progress, "priority": 1}


# --------------------------------------------------------------------------- #
# An independent bencode *encoder*. Tests use it to build synthetic torrents to
# compare against the library's decoder — a second implementation is what makes
# that comparison meaningful.
# --------------------------------------------------------------------------- #


def bencode(value: Any) -> bytes:
    if isinstance(value, bool):  # must precede int: bool is an int subclass
        raise TypeError("bencode has no boolean type")
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, str):
        value = value.encode()
    if isinstance(value, bytes):
        return b"%d:%s" % (len(value), value)
    if isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        pairs = [
            (key if isinstance(key, bytes) else str(key).encode(), item)
            for key, item in value.items()
        ]
        pairs.sort(key=lambda pair: pair[0])
        return b"d" + b"".join(bencode(k) + bencode(v) for k, v in pairs) + b"e"
    raise TypeError(f"cannot bencode {type(value)!r}")


def make_item(
    files: dict[str, bytes],
    *,
    identifier: str = "fixture_item",
    announce: str = "http://bt1.archive.org:6969/announce",
    with_comment: bool = True,
    with_url_list: bool = True,
    piece_length: int = 16384,
) -> tuple[bytes, dict[str, ItemFile]]:
    """Build a synthetic archive.org item: raw ``.torrent`` plus its manifest.

    Returns the torrent payload and an ``ItemFile`` map carrying the real MD5 of
    each payload, which is what lets the planner and downloader tests verify
    against an actual checksum rather than a stub.
    """
    import hashlib

    info_files = [
        {"length": len(payload), "path": name.split("/")} for name, payload in sorted(files.items())
    ]
    piece_count = max(1, (sum(len(p) for p in files.values()) + piece_length - 1) // piece_length)
    info = {
        "name": identifier,
        "piece length": piece_length,
        "pieces": b"\x00" * (20 * piece_count),
        "files": info_files,
    }
    top: dict[str, Any] = {
        "announce": announce,
        "announce-list": [[announce], ["http://bt2.archive.org:6969/announce"]],
        "info": info,
    }
    if with_comment:
        top["comment"] = (
            "This content hosted at the Internet Archive at "
            f"https://archive.org/details/{identifier}"
        )
    if with_url_list:
        top["url-list"] = [f"https://archive.org/download/{identifier}/"]
    manifest = {
        name: ItemFile(
            name=name,
            size=len(payload),
            md5=hashlib.md5(payload).hexdigest(),
            sha1=hashlib.sha1(payload).hexdigest(),
        )
        for name, payload in files.items()
    }
    return bencode(top), manifest


def qbt_torrent(
    *,
    torrent_hash: str = "d9a84f76bd0be498d7ac3764d7bde16a632855a7",
    name: str = FIXTURE_IDENTIFIER,
    state: str = "stalledDL",
    progress: float = 0.0,
    size: int = 13858803,
    amount_left: int = 13858803,
    num_complete: int = 0,
    num_incomplete: int = 2,
    save_path: str = "/data/downloads",
    comment: str | None = None,
    tracker: str = "http://bt1.archive.org:6969/announce",
    magnet_uri: str = "",
    has_metadata: bool = True,
) -> dict[str, Any]:
    """One entry shaped like ``GET /api/v2/torrents/info``."""
    if comment is None:
        comment = (
            "This content hosted at the Internet Archive at "
            f"https://archive.org/details/{name}\n"
            "Files may have changed, which prevents torrents from downloading correctly"
        )
    return {
        "hash": torrent_hash,
        "name": name,
        "state": state,
        "progress": progress,
        "size": size,
        "total_size": size,
        "amount_left": amount_left,
        "num_complete": num_complete,
        "num_incomplete": num_incomplete,
        "num_seeds": 0,
        "num_leechs": 0,
        "save_path": save_path,
        "content_path": f"{save_path}/{name}",
        "root_path": f"{save_path}/{name}",
        "comment": comment,
        "tracker": tracker,
        "magnet_uri": magnet_uri,
        "category": "",
        "tags": "",
        "has_metadata": has_metadata,
        "dlspeed": 0,
        "eta": 8640000,
        "piece_size": 16384,
        "pieces_have": 0,
        "pieces_num": 1,
        "infohash_v1": torrent_hash,
        "creator": "ia_make_torrent",
    }


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "FakeQbit/5.2.3"
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:  # silence the test output
        pass

    # ------------------------------------------------------------- utilities

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _empty(self, status: int, cookie: str | None = None) -> None:
        self.send_response(status)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _text(self, status: int, text: str) -> None:
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, list[str]]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode() if length else ""
        return urllib.parse.parse_qs(raw)

    def _authed(self) -> bool:
        cookie = self.headers.get("Cookie") or ""
        return self.server.session_cookie_name in cookie

    # ------------------------------------------------------------------ verbs

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if not self._authed():
            self._empty(403)
            return
        handler = self.server.routes.get(parsed.path)
        if handler is None:
            self._empty(404)
            return
        self._json(handler(query))

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        parsed = urllib.parse.urlparse(self.path)
        form = self._body()
        if parsed.path == "/api/v2/auth/login":
            server = self.server
            username = (form.get("username") or [""])[0]
            password = (form.get("password") or [""])[0]
            if server.banned:
                self._text(403, "Your IP has been banned")
                return
            if username != server.username or password != server.password:
                self._text(403, "Fails.")
                return
            # qBittorrent 5.x: 204 No Content, cookie named QBT_SID_<port>.
            self._empty(204, cookie=f"{server.session_cookie_name}=abc123; path=/")
            return
        if not self._authed():
            self._empty(403)
            return
        handler = self.server.post_routes.get(parsed.path)
        if handler is None:
            self._empty(404)
            return
        handler(form)
        self._empty(200)


class FakeQbitServer:
    """A tiny qBittorrent WebUI that runs in a background thread."""

    def __init__(
        self,
        *,
        torrents: list[dict[str, Any]] | None = None,
        files: dict[str, list[dict[str, Any]]] | None = None,
        username: str = "admin",
        password: str = "secret",
        checking_polls: int = 2,
        final_state: str = "stoppedUP",
        final_progress: float = 1.0,
        support_stop: bool = True,
        legacy_pause_names: bool = False,
    ) -> None:
        self.torrents = {t["hash"]: t for t in (torrents or [qbt_torrent()])}
        self.files = files if files is not None else {next(iter(self.torrents)): []}
        self.username = username
        self.password = password
        self.banned = False
        self.checking_polls = checking_polls
        self.final_state = final_state
        self.final_progress = final_progress
        self.support_stop = support_stop
        self.calls: list[tuple[str, str]] = []
        self.rechecked: list[str] = []
        self.started: list[str] = []
        self.stopped: list[str] = []
        self._checking: dict[str, int] = {}
        self._checking_started: set[str] = set()

        routes = {
            "/api/v2/app/version": lambda q: "v5.2.3",
            "/api/v2/app/webapiVersion": lambda q: "2.15.1",
            "/api/v2/app/preferences": lambda q: {"save_path": "/data/downloads"},
            "/api/v2/torrents/info": self._info,
            "/api/v2/torrents/files": self._files,
            "/api/v2/torrents/properties": lambda q: {"save_path": "/data/downloads"},
            "/api/v2/torrents/trackers": lambda q: [],
        }
        post_routes = {
            "/api/v2/torrents/recheck": self._recheck,
            "/api/v2/torrents/reannounce": lambda form: None,
        }
        if self.support_stop:
            # qBittorrent 5.x renamed pause/resume to stop/start; 4.x only knows
            # the old names. Both are exercised, because a real deployment may be
            # either.
            if legacy_pause_names:
                post_routes["/api/v2/torrents/pause"] = self._stop
                post_routes["/api/v2/torrents/resume"] = self._start
            else:
                post_routes["/api/v2/torrents/stop"] = self._stop
                post_routes["/api/v2/torrents/start"] = self._start

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.routes = routes  # type: ignore[attr-defined]
        self._server.post_routes = post_routes  # type: ignore[attr-defined]
        self._server.username = username  # type: ignore[attr-defined]
        self._server.password = password  # type: ignore[attr-defined]
        self._server.banned = False  # type: ignore[attr-defined]
        self._server.session_cookie_name = "QBT_SID_test"  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    # ------------------------------------------------------------------ state

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> FakeQbitServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> FakeQbitServer:
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.stop()

    def set_banned(self, value: bool) -> None:
        self.banned = value
        self._server.banned = value  # type: ignore[attr-defined]

    def set_torrent(self, **changes) -> None:
        key = next(iter(self.torrents))
        self.torrents[key].update(changes)

    # --------------------------------------------------------------- handlers

    def _info(self, query: dict[str, list[str]]) -> list[dict[str, Any]]:
        self.calls.append(("GET", "/api/v2/torrents/info"))
        wanted = (query.get("hashes") or [""])[0]
        hashes = [h for h in wanted.split("|") if h] or list(self.torrents)
        out = []
        for torrent_hash in hashes:
            entry = self.torrents.get(torrent_hash)
            if entry is None:
                continue
            pending = self._checking.get(torrent_hash, 0)
            if pending > 0:
                self._checking[torrent_hash] = pending - 1
                entry["state"] = "checkingResumeData"
            elif torrent_hash in self._checking:
                # The recheck finished: persist the result, the way qBittorrent
                # does. Returning it only once would misrepresent a client that
                # reads the state again afterwards.
                del self._checking[torrent_hash]
                entry["state"] = self.final_state
                entry["progress"] = self.final_progress
                entry["amount_left"] = (
                    0 if self.final_progress >= 1.0 else entry.get("amount_left", 0)
                )
            out.append(dict(entry))
        return out

    def _files(self, query: dict[str, list[str]]) -> list[dict[str, Any]]:
        torrent_hash = (query.get("hash") or [""])[0]
        return self.files.get(torrent_hash, [])

    def _recheck(self, form: dict[str, list[str]]) -> None:
        for torrent_hash in (form.get("hashes") or [""])[0].split("|"):
            if torrent_hash:
                self.rechecked.append(torrent_hash)
                self._checking[torrent_hash] = self.checking_polls

    def _stop(self, form: dict[str, list[str]]) -> None:
        hashes = [h for h in (form.get("hashes") or [""])[0].split("|") if h]
        self.stopped.extend(hashes)
        for torrent_hash in hashes:
            if torrent_hash in self.torrents:
                self.torrents[torrent_hash]["state"] = "stoppedDL"

    def _start(self, form: dict[str, list[str]]) -> None:
        hashes = [h for h in (form.get("hashes") or [""])[0].split("|") if h]
        self.started.extend(hashes)
        for torrent_hash in hashes:
            if torrent_hash in self.torrents:
                self.torrents[torrent_hash]["state"] = "uploading"
