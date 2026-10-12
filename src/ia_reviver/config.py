"""Configuration: defaults < TOML file < environment < command line.

Defaults target the common NixOS layout (``services.qbittorrent`` on localhost,
password in a secret file), and every value can be overridden in a config file,
an environment variable, or a flag — in that order of increasing precedence.

A password *file* is supported explicitly so the tool works with systemd
credentials and sops-nix, which is how the password should be supplied on a
server: never on the command line where it lands in the process table.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

__all__ = ["Config", "ConfigError", "load_config", "DEFAULT_CONFIG_PATHS", "ENV_PREFIXES"]

ENV_PREFIXES = ("IA_REVIVER_", "IA_")
CONFIG_ENV = "IA_REVIVER_CONFIG"
DEFAULT_CONFIG_PATHS = (
    Path("ia-reviver.toml"),
    Path.home() / ".config" / "ia-reviver" / "config.toml",
    Path("/etc/ia-reviver/config.toml"),
)


class ConfigError(RuntimeError):
    """Invalid or unusable configuration."""


@dataclass(slots=True)
class Config:
    """Resolved settings for one run."""

    # --- qBittorrent WebUI ---
    qb_url: str = "http://127.0.0.1:8080"
    qb_username: str | None = None
    qb_password: str | None = None
    qb_password_file: str | None = None
    verify_tls: bool = True
    qb_timeout: float = 30.0

    # --- archive.org ---
    archive_base_url: str = "https://archive.org"
    archive_timeout: float = 120.0
    archive_retries: int = 4

    # --- fetching ---
    jobs: int = 3
    limit_rate: int | None = None
    """Aggregate ceiling in bytes/s; ``None`` means unlimited."""
    verify: bool = True
    download_retries: int = 3
    resume_after: bool = True
    """Start torrents again once their data is in place."""

    # --- behaviour ---
    recheck_timeout: float = 900.0
    state_dir: str = "~/.local/state/ia-reviver"

    @property
    def state_path(self) -> Path:
        return Path(os.path.expanduser(self.state_dir))

    def password(self) -> str | None:
        """The WebUI password, read from a file when one was configured.

        Trailing newlines are stripped: a secret written by ``sops`` or ``echo``
        almost always ends with one, and qBittorrent compares the value verbatim.
        """
        if self.qb_password_file:
            path = Path(os.path.expanduser(self.qb_password_file))
            try:
                return path.read_text(encoding="utf-8").rstrip("\r\n")
            except OSError as exc:
                raise ConfigError(f"cannot read qb_password_file {path}: {exc}") from exc
        return self.qb_password


_FIELD_ALIASES: dict[str, str] = {
    "url": "qb_url",
    "username": "qb_username",
    "user": "qb_username",
    "password": "qb_password",
    "password_file": "qb_password_file",
    "insecure": "verify_tls",  # inverted; handled below
    "retries": "download_retries",
}

#: TOML section name -> prefix prepended to its keys. This is what lets a config
#: file be written either flat (``qb_url = ...``) or sectioned
#: (``[qbittorrent] url = ...``) and mean the same thing.
_SECTION_PREFIX: dict[str, str] = {
    "qb": "qb_",
    "qbittorrent": "qb_",
    "archive": "archive_",
    "fetching": "",
    "download": "",
    "output": "",
    "behaviour": "",
    "behavior": "",
}

_BOOL_FIELDS = {"verify", "verify_tls", "resume_after"}
_INT_FIELDS = {"jobs", "limit_rate", "archive_retries", "download_retries"}
_FLOAT_FIELDS = {"qb_timeout", "archive_timeout", "recheck_timeout"}


def _coerce(name: str, value: Any) -> Any:
    """Turn a raw config/env value into the field's type."""
    if value is None:
        return None
    if name in _BOOL_FIELDS:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    if name == "insecure":
        return not _coerce("verify", value)
    if name == "limit_rate":
        text = str(value).strip()
        if not text or text in {"0", "none", "unlimited"}:
            return None
        from .util import parse_size

        if text.isdigit():
            return int(text)
        return parse_size(text)
    if name in _INT_FIELDS:
        return int(value)
    if name in _FLOAT_FIELDS:
        return float(value)
    return str(value)


def _normalize(source: dict[str, Any]) -> dict[str, Any]:
    """Flatten ``[qbittorrent] url = ...`` style sections and apply aliases."""
    flat: dict[str, Any] = {}
    for key, value in source.items():
        if isinstance(value, dict):
            prefix = _SECTION_PREFIX.get(key, f"{key}_")
            for sub_key, sub_value in value.items():
                flat[f"{prefix}{sub_key}"] = sub_value
        else:
            flat[key] = value

    normalized: dict[str, Any] = {}
    valid = {f.name for f in fields(Config)}
    for key, value in flat.items():
        target = key if key in valid else _FIELD_ALIASES.get(key)
        if target == "verify_tls" and key == "insecure":
            normalized[target] = not _coerce("verify", value)
            continue
        if target is None:
            continue
        try:
            normalized[target] = _coerce(key if key != "insecure" else "insecure", value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"invalid value for '{key}': {value!r} ({exc})") from exc
    return normalized


def _from_file(path: Path) -> dict[str, Any]:
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    return _normalize(data)


def _from_env(environ: dict[str, str]) -> dict[str, Any]:
    """Collect ``IA_REVIVER_*`` / ``IA_*`` variables.

    The original key name is kept (rather than being resolved to a field here) so
    that alias and inversion rules are applied in exactly one place: ``_normalize``.
    """
    raw: dict[str, Any] = {}
    valid = {f.name for f in fields(Config)}
    for key, value in environ.items():
        for prefix in ENV_PREFIXES:
            if not key.startswith(prefix):
                continue
            name = key[len(prefix) :].lower()
            if name in valid or name in _FIELD_ALIASES:
                raw[name] = value
            break
    # Aliases used by other *arr-style tooling, so an existing environment works.
    for alias, target in (
        ("QB_URL", "qb_url"),
        ("QB_USERNAME", "qb_username"),
        ("QB_PASSWORD", "qb_password"),
    ):
        if alias in environ and target not in raw:
            raw[target] = environ[alias]
    return _normalize(raw)


def find_config_file(explicit: str | None = None) -> Path | None:
    if explicit:
        path = Path(os.path.expanduser(explicit))
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        return path
    env_path = os.environ.get(CONFIG_ENV)
    if env_path:
        path = Path(os.path.expanduser(env_path))
        if not path.is_file():
            raise ConfigError(f"{CONFIG_ENV} points at a missing file: {path}")
        return path
    for candidate in DEFAULT_CONFIG_PATHS:
        if candidate.is_file():
            return candidate
    return None


def load_config(
    *,
    config_file: str | None = None,
    overrides: dict[str, Any] | None = None,
    environ: dict[str, str] | None = None,
) -> Config:
    """Build the effective configuration.

    Precedence, lowest to highest: dataclass defaults, TOML file, environment,
    explicit ``overrides`` (the command line).
    """
    environ = environ if environ is not None else dict(os.environ)
    config = Config()

    path = find_config_file(config_file)
    if path is not None:
        config = replace(config, **_from_file(path))

    config = replace(config, **_from_env(environ))

    if overrides:
        cleaned = {key: value for key, value in _normalize(overrides).items() if value is not None}
        config = replace(config, **cleaned)

    if config.jobs < 1:
        raise ConfigError("jobs must be at least 1")
    if config.download_retries < 1:
        raise ConfigError("download_retries must be at least 1")
    return config
