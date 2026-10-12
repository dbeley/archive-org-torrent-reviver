# Changelog

Notable changes to `ia-reviver`. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-10-12

First release.

### Added

- `ia-reviver scan` — lists the archive.org torrents in qBittorrent that are
  incomplete or have no seeder, identifying each item from its comment and
  trackers. `--json` for scripts.
- `ia-reviver revive` — downloads the missing files from
  `archive.org/download/<id>/<path>` and forces a recheck, turning an unseeded
  torrent into a seeder. Supports `--dry-run`, `--yes`, `--all`, `--jobs`,
  `--max-bytes`, `--limit-rate`, `--force`.
- `ia-reviver check` — verifies qBittorrent and archive.org are reachable.
- Ranged, resumable downloads (`*.ia-reviver-part`) with MD5 verification
  against the item manifest where archive.org publishes one.
- Refusal of any torrent-supplied path that escapes the torrent's `save_path`.
- Detection of stale torrents, where archive.org has regenerated the item and a
  recheck could never succeed.
- qBittorrent WebUI API v2 client supporting both 4.x and 5.x — including the
  5.x `204`/empty-body login and `QBT_SID_<port>` cookie.
- Configuration by defaults, TOML file, environment, or flags; password read
  from a file or systemd credential rather than an argument.
- Nix flake: `packages.ia-reviver`, `packages.default`, `apps.default`, a
  `devShells.default`, and named `checks`.
- Test suite of 127 tests, fully offline, run against a fake qBittorrent WebUI
  and a fake archive.org.

[Unreleased]: https://github.com/dbeley/archive-org-torrent-reviver/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/dbeley/archive-org-torrent-reviver/releases/tag/v0.1.0
