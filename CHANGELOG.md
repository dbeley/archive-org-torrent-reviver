# Changelog

Notable changes to `ia-reviver`. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **A file archive.org changed after the `.torrent` was made is no longer
  downloaded in vain.** archive.org regenerates its own metadata files
  (`_meta.xml` and friends), so a torrent created earlier can record a size the
  item no longer serves — those exact bytes are gone from the archive, and no
  transfer will ever reproduce them. The old order was: queue the file, transfer
  it, notice the size differs, retry three times with backoff, then leave the
  oversized file in the `save_path` for qBittorrent to hash. Now the planner sees
  the mismatch against the item's published sizes and marks the file `changed`:
  it is never queued, the plan table reports how many files are gone, and the
  outcome reads "cannot be completed … their exact bytes no longer exist" rather
  than "partial" — which invited a retry that could never work. A *truncated*
  transfer (the server states the right length but delivers fewer bytes) is still
  retried, and a genuine size change is now settled from the response headers
  before a single body byte is transferred.
- **A connection that dies mid-body is retried** instead of aborting the file.
  `http.client` errors (`IncompleteRead`, `BadStatusLine`…) were not caught, so a
  dropped connection during a multi-gigabyte transfer skipped the retry logic
  entirely.
- **A refused write no longer gets reported against the wrong endpoint.** A 403
  ("Forbidden") from qBittorrent means the WebUI did not accept the request as
  authenticated, and its check runs *before* the URL is routed — so every
  private endpoint answers 403, including ones that do not exist. `stop`/`start`
  used to retry their pre-5.x names on *any* error, so a rejected `/torrents/stop`
  was retried as `/torrents/pause` and the message named `pause` — an endpoint
  that may not even exist on 5.x. The fallback now happens only on **404**, and a
  403 is reported with the endpoint that really failed and what a 403 means.
- **`revive` refuses to start when writes are refused**, instead of planning a
  whole library and then failing on every torrent. The check runs before the
  first archive.org request.
- **`check` now verifies writes, not just reads**, so this class of failure is
  caught by the command whose job is to catch it.
- **Planning no longer looks like a hang.** Building the plan costs at least one
  archive.org round-trip per torrent, and the phase printed nothing until every
  torrent had been planned — so a library of dozens of dead torrents sat silent
  for minutes with no way to tell work from a stall. Planning now shows a live
  `[n/total] <identifier>` counter: rewritten in place on a terminal, and at most
  one line every 5 seconds when piped (cron, systemd, `| tee`).

### Changed

- **No longer sends `Origin`/`Referer`.** qBittorrent runs its CSRF check *only*
  when one of those headers is present, and compares it against the request's
  `Host`; a mismatch is refused outright. Its source documents that sending
  neither is allowed deliberately, "so [that] Web API users [don't] spoof
  headers" — so omitting them removes a failure mode instead of adding one.
- The development harness now starts qBittorrent with CSRF protection **on**,
  matching a default deployment. It had been disabled, which is why a
  write-refusing WebUI was never exercised during development.
- `build_plans` folds its five identical per-candidate exception handlers into
  one.

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
