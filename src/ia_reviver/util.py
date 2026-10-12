"""Formatting and small filesystem helpers (stdlib only)."""

from __future__ import annotations

import hashlib
import os
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

__all__ = [
    "human_bytes",
    "human_rate",
    "human_duration",
    "parse_size",
    "short_hash",
    "md5_file",
    "safe_relative_path",
    "is_tty",
    "format_table",
]

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def human_bytes(num: float, *, precision: int = 1) -> str:
    """``1536`` -> ``1.5 KiB``."""
    value = float(num)
    for unit in _UNITS:
        if abs(value) < 1024.0 or unit == _UNITS[-1]:
            if unit == "B":
                return f"{value:.0f} {unit}"
            return f"{value:.{precision}f} {unit}"
        value /= 1024.0
    return f"{value:.{precision}f} {_UNITS[-1]}"  # pragma: no cover


def human_rate(bytes_per_second: float) -> str:
    return f"{human_bytes(bytes_per_second, precision=1)}/s"


def human_duration(seconds: float) -> str:
    """``3725`` -> ``1h02m``; unknown/negative becomes ``--``."""
    if seconds is None or seconds < 0:
        return "--"
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m{secs:02d}s"


def parse_size(text: str) -> int:
    """Parse ``10M`` / ``1.5 GiB`` / ``512`` into bytes."""
    cleaned = text.strip().replace(" ", "").lower()
    if not cleaned:
        raise ValueError("empty size")
    multipliers = {
        "": 1,
        "b": 1,
        "k": 1024,
        "kb": 1024,
        "kib": 1024,
        "m": 1024**2,
        "mb": 1024**2,
        "mib": 1024**2,
        "g": 1024**3,
        "gb": 1024**3,
        "gib": 1024**3,
        "t": 1024**4,
        "tb": 1024**4,
        "tib": 1024**4,
    }
    for suffix in sorted(multipliers, key=len, reverse=True):
        if suffix and cleaned.endswith(suffix):
            number = cleaned[: -len(suffix)]
            return int(float(number) * multipliers[suffix])
    return int(float(cleaned))


def short_hash(value: str, length: int = 12) -> str:
    return value[:length]


def md5_file(path: Path, *, chunk: int = 1024 * 1024, should_stop=None) -> str:
    """Stream a file through MD5 without holding it in memory.

    ``should_stop`` is an optional zero-argument callable used to make this
    interruptible (the caller aborts verification if the user hits Ctrl-C).
    """
    digest = hashlib.md5(usedforsecurity=False)
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
            if should_stop is not None and should_stop():
                raise InterruptedError("verification interrupted")
    return digest.hexdigest()


def safe_relative_path(name: str) -> tuple[str | None, str]:
    """Validate a torrent-supplied path before touching the filesystem.

    Torrent file names come from whoever created the torrent, so a hostile one
    could contain ``..`` or an absolute path and make this tool write outside the
    torrent's own directory. Returns ``(relative_name, reason)`` where a ``None``
    name means the path is unsafe and ``reason`` explains why.
    """
    if not name or name in {".", ".."}:
        return None, "empty or dot path"
    if name.startswith("/") or name.startswith("\\"):
        return None, "absolute path"
    if os.path.isabs(name) or (len(name) > 1 and name[1] == ":"):
        return None, "absolute path"
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts:
        return None, "empty path"
    if any(part == ".." for part in parts):
        return None, "path traversal ('..')"
    return "/".join(parts), ""


def is_tty(stream=None) -> bool:
    stream = stream or sys.stdout
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):  # pragma: no cover
        return False


def format_table(headers: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    """Render a simple left-aligned text table."""
    materialized = [[str(cell) for cell in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in materialized:
        for index, cell in enumerate(row):
            if index < len(widths):
                widths[index] = max(widths[index], len(cell))
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    for row in materialized:
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return "\n".join(lines)
