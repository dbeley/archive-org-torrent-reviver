"""qBittorrent WebUI API v2 client.

Written against a real qBittorrent 5.2.3, and deliberately tolerant of the
differences between versions, because the obvious implementation breaks:

* ``POST /api/v2/auth/login`` returns an empty body with HTTP **204** on 5.x,
  and ``Ok.`` with HTTP 200 on older releases. Only 4.x-era code checks for the
  literal ``"Ok."`` and would treat a modern successful login as a failure.
* The session cookie is named ``SID`` on older versions and ``QBT_SID_<port>``
  on 5.x. Never hardcode the name — let the cookie jar carry it.
* ``comment`` is present in ``GET /api/v2/torrents/info`` on 5.x, so the
  archive.org identifier can be read without one extra request per torrent.
"""

from __future__ import annotations

import http.cookiejar
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

__all__ = [
    "QbitError",
    "QbitHttpError",
    "QbitAuthError",
    "QbitBannedError",
    "QbitForbiddenError",
    "QbitNotFound",
    "Torrent",
    "QbitClient",
    "CHECKING_STATES",
]

CHECKING_STATES = frozenset(
    {"checkingDL", "checkingUP", "checkingResumeData", "forcedMetaDL", "metaDL"}
)
"""States that mean "qBittorrent is busy with metadata or hashing".

``forcedMetaDL``/``metaDL`` are included because a magnet that has not resolved
yet has no file list to act on.
"""

ACTIVE_STATES = frozenset(
    {
        "downloading",
        "uploading",
        "stalledDL",
        "stalledUP",
        "forcedDL",
        "forcedUP",
        "allocating",
        "moving",
        "queuedDL",
        "queuedUP",
        "checkingDL",
        "checkingUP",
    }
)


class QbitError(RuntimeError):
    """Any failure talking to the qBittorrent WebUI."""


class QbitHttpError(QbitError):
    """A non-2xx HTTP response, carrying the status code.

    The code matters: ``stop``/``start`` may only fall back to their pre-5.x
    names on a 404, never on an authentication or permission failure.
    """

    def __init__(self, message: str, code: int = 403) -> None:
        super().__init__(message)
        self.code = code


class QbitAuthError(QbitError):
    """Bad credentials."""


class QbitBannedError(QbitHttpError):
    """The WebUI banned this client IP after too many failed logins."""


class QbitForbiddenError(QbitHttpError):
    """403 on a request — the WebUI did not accept it as authenticated.

    qBittorrent's WebUI raises a bare ``403 Forbidden`` from exactly one place:
    a request for a *private* API endpoint that arrives with no valid session.
    Reads can still succeed, so this is the failure that looks like "scan works,
    revive does not".
    """


class QbitNotFound(QbitError):
    """The requested torrent does not exist."""


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass(slots=True)
class Torrent:
    """One entry of ``GET /api/v2/torrents/info``."""

    raw: dict[str, Any]

    @property
    def hash(self) -> str:
        return str(self.raw.get("hash") or "")

    @property
    def name(self) -> str:
        return str(self.raw.get("name") or "")

    @property
    def state(self) -> str:
        return str(self.raw.get("state") or "")

    @property
    def progress(self) -> float:
        return _float(self.raw.get("progress"))

    @property
    def size(self) -> int:
        return _int(self.raw.get("size"))

    @property
    def total_size(self) -> int:
        return _int(self.raw.get("total_size")) or _int(self.raw.get("size"))

    @property
    def amount_left(self) -> int:
        return _int(self.raw.get("amount_left"))

    @property
    def num_complete(self) -> int:
        """Seeders reported by the tracker; ``-1`` when unknown."""
        return _int(self.raw.get("num_complete"), -1)

    @property
    def num_incomplete(self) -> int:
        return _int(self.raw.get("num_incomplete"), -1)

    @property
    def num_seeds(self) -> int:
        """Seeds this client is actually connected to."""
        return _int(self.raw.get("num_seeds"))

    @property
    def save_path(self) -> str:
        return str(self.raw.get("save_path") or "")

    @property
    def content_path(self) -> str:
        return str(self.raw.get("content_path") or "")

    @property
    def root_path(self) -> str:
        return str(self.raw.get("root_path") or "")

    @property
    def comment(self) -> str:
        return str(self.raw.get("comment") or "")

    @property
    def tracker(self) -> str:
        return str(self.raw.get("tracker") or "")

    @property
    def magnet_uri(self) -> str:
        return str(self.raw.get("magnet_uri") or "")

    @property
    def category(self) -> str:
        return str(self.raw.get("category") or "")

    @property
    def has_metadata(self) -> bool:
        return bool(self.raw.get("has_metadata", True))

    @property
    def dlspeed(self) -> int:
        return _int(self.raw.get("dlspeed"))

    @property
    def eta(self) -> int:
        return _int(self.raw.get("eta"))

    @property
    def piece_size(self) -> int:
        return _int(self.raw.get("piece_size"))

    @property
    def pieces_have(self) -> int:
        return _int(self.raw.get("pieces_have"))

    @property
    def pieces_num(self) -> int:
        return _int(self.raw.get("pieces_num"))

    @property
    def is_checking(self) -> bool:
        return self.state in CHECKING_STATES

    @property
    def is_complete(self) -> bool:
        return self.progress >= 1.0

    @property
    def is_paused(self) -> bool:
        return self.state in {"pausedDL", "pausedUP", "stoppedDL", "stoppedUP"}

    @property
    def tracker_urls(self) -> list[str]:
        """Tracker URLs from ``magnet_uri`` (``tr=`` params), if any."""
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.magnet_uri).query).get("tr", [])


class QbitClient:
    """Small, dependency-free qBittorrent WebUI client."""

    def __init__(
        self,
        url: str = "http://127.0.0.1:8080",
        *,
        username: str | None = None,
        password: str | None = None,
        timeout: float = 30.0,
        verify_tls: bool = True,
    ) -> None:
        self.url = url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self._jar = http.cookiejar.CookieJar()
        handlers: list[urllib.request.BaseHandler] = [urllib.request.HTTPCookieProcessor(self._jar)]
        if self.url.startswith("https") and not verify_tls:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=context))
        self._opener = urllib.request.build_opener(*handlers)
        self._logged_in = False
        self._version: str | None = None

    # ------------------------------------------------------------------ plumbing

    def _do(
        self,
        path: str,
        *,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        form: dict[str, Any] | None = None,
        accept_json: bool = True,
    ) -> Any:
        url = f"{self.url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        # Deliberately no Referer/Origin. qBittorrent runs its CSRF check *only*
        # when one of them is present, and compares it against the request's Host
        # header — a mismatch is refused outright. Its own source says omitting
        # both is allowed on purpose "so [that] Web API users [don't] spoof
        # headers". Sending neither is therefore both simpler and strictly more
        # robust than trying to guess a value that matches.
        headers: dict[str, str] = {}
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            response = self._opener.open(request, timeout=self.timeout)
            with response:
                body = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            body = exc.read()
            exc.close()
            text = body.decode("utf-8", "replace").strip()
            if exc.code == 403 and "ban" in text.lower():
                raise QbitBannedError(
                    "qBittorrent banned this IP after repeated failed logins. "
                    f"Wait for the ban to expire or clear it in the WebUI. ({text})"
                ) from exc
            if exc.code == 403:
                raise QbitForbiddenError(
                    f"HTTP 403 on {method} {path}: qBittorrent refused the request. "
                    "Its WebUI answers a bare 403 when a request for a private API "
                    "endpoint carries no valid session, so reads (scan, planning) can "
                    "succeed while every write is refused. Check the WebUI log, and "
                    "run 'ia-reviver check -v' to see what it reports about the session."
                ) from exc
            raise QbitHttpError(
                f"HTTP {exc.code} on {method} {path}: {text or '<empty>'}", exc.code
            ) from exc
        except (urllib.error.URLError, TimeoutError, ssl.SSLError) as exc:
            raise QbitError(f"cannot reach qBittorrent at {self.url}: {exc}") from exc

        if not accept_json:
            return status
        text = body.decode("utf-8", "replace").strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def login(self) -> str:
        """Authenticate, returning the qBittorrent version string.

        Success is HTTP 200 (body ``Ok.``) on 4.x and HTTP **204 with an empty
        body** on 5.x — so the status plus a follow-up version probe decides,
        never the body text.
        """
        if self.username is None:
            # Assume localhost auth is disabled, but verify rather than assume:
            # a WebUI with authentication on will answer 403 here.
            self._logged_in = True
            try:
                return self._probe_version()
            except QbitError as exc:
                self._logged_in = False
                raise QbitAuthError(
                    "the qBittorrent WebUI requires authentication but no username is "
                    "configured. Set --qb-username/--qb-password (or IA_REVIVER_QB_*), "
                    f"or enable 'Bypass authentication for clients on localhost'. ({exc})"
                ) from exc
        try:
            status = self._do(
                "/api/v2/auth/login",
                method="POST",
                form={"username": self.username, "password": self.password or ""},
                accept_json=False,
            )
        except QbitBannedError:
            # A ban is not a credentials problem; say what actually happened.
            raise
        except QbitError as exc:
            raise QbitAuthError(f"login request failed: {exc}") from exc
        if status not in (200, 204):
            raise QbitAuthError(f"login returned HTTP {status}")
        self._logged_in = True
        try:
            return self._probe_version()
        except QbitError as exc:
            self._logged_in = False
            raise QbitAuthError(
                "login did not establish a session (wrong username/password, or "
                f"the WebUI rejected the request): {exc}"
            ) from exc

    def _probe_version(self) -> str:
        version = self._do("/api/v2/app/version")
        if not isinstance(version, str) or not version:
            raise QbitError("qBittorrent did not report a version")
        self._version = version
        return version

    def _ensure_session(self) -> None:
        if not self._logged_in:
            self.login()

    def app_version(self, refresh: bool = False) -> str:
        if refresh or self._version is None:
            self._ensure_session()
        return self._version or ""

    def webapi_version(self) -> str:
        self._ensure_session()
        value = self._do("/api/v2/app/webapiVersion")
        return str(value) if value is not None else ""

    def preferences(self) -> dict[str, Any]:
        self._ensure_session()
        value = self._do("/api/v2/app/preferences")
        return value if isinstance(value, dict) else {}

    # ------------------------------------------------------------------ torrents

    def torrents(self, hashes: Iterable[str] | None = None) -> list[Torrent]:
        """``GET /api/v2/torrents/info``, optionally filtered to some hashes."""
        self._ensure_session()
        params = None
        hash_list = list(hashes) if hashes is not None else None
        if hash_list is not None:
            if not hash_list:
                return []
            params = {"hashes": "|".join(hash_list)}
        payload = self._do("/api/v2/torrents/info", params=params)
        if not isinstance(payload, list):
            raise QbitError("qBittorrent returned an unexpected torrents/info payload")
        return [Torrent(t) for t in payload if isinstance(t, dict)]

    def torrent(self, torrent_hash: str) -> Torrent:
        found = self.torrents([torrent_hash])
        if not found:
            raise QbitNotFound(f"no torrent with hash {torrent_hash} in qBittorrent")
        return found[0]

    def files(self, torrent_hash: str) -> list[dict[str, Any]]:
        """``GET /api/v2/torrents/files``.

        Each ``name`` is relative to ``save_path`` and, for multi-file torrents,
        **includes the torrent's root folder** (verified on 5.2.3), which is why
        joining ``save_path`` with ``name`` yields the real path on disk.
        """
        self._ensure_session()
        payload = self._do("/api/v2/torrents/files", params={"hash": torrent_hash})
        if not isinstance(payload, list):
            raise QbitError("qBittorrent returned an unexpected torrents/files payload")
        return [f for f in payload if isinstance(f, dict)]

    def trackers(self, torrent_hash: str) -> list[dict[str, Any]]:
        self._ensure_session()
        payload = self._do("/api/v2/torrents/trackers", params={"hash": torrent_hash})
        return [t for t in payload if isinstance(t, dict)] if isinstance(payload, list) else []

    def properties(self, torrent_hash: str) -> dict[str, Any]:
        self._ensure_session()
        payload = self._do("/api/v2/torrents/properties", params={"hash": torrent_hash})
        return payload if isinstance(payload, dict) else {}

    # ------------------------------------------------------------------ mutations

    def _post_hashes(self, action: str, hashes: Iterable[str]) -> None:
        hash_list = list(hashes)
        if not hash_list:
            return
        self._ensure_session()
        self._do(action, method="POST", form={"hashes": "|".join(hash_list)})

    def recheck(self, hashes: Iterable[str]) -> None:
        """Force a recheck. This is what turns fetched files into "seeding"."""
        self._post_hashes("/api/v2/torrents/recheck", hashes)

    def _post_first_available(self, paths: tuple[str, ...], hashes: Iterable[str]) -> None:
        """POST to the first of ``paths`` this WebUI knows.

        Only a **404** justifies trying the next name. Falling back on any error
        would retry the other spelling after an authentication or permission
        failure that no rename can fix, and would report the *fallback*
        endpoint's error instead of the real one.
        """
        last: QbitHttpError | None = None
        for path in paths:
            try:
                self._post_hashes(path, hashes)
                return
            except QbitHttpError as exc:
                if exc.code != 404:
                    raise
                last = exc
        raise QbitError(
            f"none of {' or '.join(paths)} exist on this qBittorrent "
            f"(HTTP 404 on each); the WebUI API may have changed ({last})"
        )

    def stop(self, hashes: Iterable[str]) -> None:
        """Pause torrents (``stop`` on 5.x, ``pause`` before that)."""
        self._post_first_available(
            ("/api/v2/torrents/stop", "/api/v2/torrents/pause"), list(hashes)
        )

    def start(self, hashes: Iterable[str]) -> None:
        """Resume torrents (``start`` on 5.x, ``resume`` before that)."""
        self._post_first_available(
            ("/api/v2/torrents/start", "/api/v2/torrents/resume"), list(hashes)
        )

    def verify_post_access(self) -> None:
        """Prove that state-changing requests are authorised, changing nothing.

        Reading uses GETs, which a WebUI can serve happily while refusing every
        POST. Without this, a large library gets planned, the first pause fails,
        and the run discovers that nothing was ever going to work. Targets a hash
        that cannot exist, so it is a no-op even when it succeeds.
        """
        self._ensure_session()
        self.stop(["0" * 40])

    def reannounce(self, hashes: Iterable[str]) -> None:
        self._post_hashes("/api/v2/torrents/reannounce", hashes)

    def then_check(
        self,
        hashes: Iterable[str],
        *,
        timeout: float = 900.0,
        poll: float = 1.0,
        on_tick=None,
    ) -> dict[str, Torrent]:
        """Recheck then wait until every torrent is done hashing.

        Returns the final ``torrents/info`` entries keyed by hash. A client only
        becomes a seed once hashing confirms the data on disk, so waiting here is
        what lets the caller report a real outcome rather than a hopeful one.
        """
        hash_list = list(hashes)
        if not hash_list:
            return {}
        self.recheck(hash_list)
        started = time.monotonic()
        deadline = started + timeout
        saw_checking = False
        while time.monotonic() < deadline:
            current = {t.hash: t for t in self.torrents(hash_list)}
            busy = [h for h, t in current.items() if t.is_checking]
            if busy:
                saw_checking = True
            elif saw_checking or (time.monotonic() - started) > 3.0:
                return current
            if on_tick:
                on_tick(current)
            time.sleep(poll)
        raise QbitError(
            f"timed out after {timeout:.0f}s waiting for qBittorrent to finish rechecking"
        )
