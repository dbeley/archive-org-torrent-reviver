"""Revive dead archive.org torrents by populating them from archive.org itself.

A dead archive.org torrent (zero seeders) can be brought back to life without any
peer: archive.org still hosts every byte the torrent describes over plain HTTP.
Download those bytes into the location qBittorrent expects, force a recheck, and
the client becomes a seed for a swarm that had none.
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
