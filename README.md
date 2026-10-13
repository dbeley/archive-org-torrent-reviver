# ia-reviver

[![CI](https://github.com/dbeley/archive-org-torrent-reviver/actions/workflows/ci.yml/badge.svg)](https://github.com/dbeley/archive-org-torrent-reviver/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

Populate **unseeded archive.org torrents from archive.org itself**, then let
qBittorrent verify the data and become the seed that swarm no longer had.

## The problem

Most archive.org torrents have no seeders. archive.org says so in the torrent's
own comment:

> This content hosted at the Internet Archive … Files may have changed, which
> prevents torrents from downloading correctly or completely; please check for
> an updated torrent at … **Note: retrieval usually requires a client that
> supports webseeding (GetRight style).**

qBittorrent (libtorrent) does not do GetRight-style web seeding, so those
torrents sit at `stalledDL` / 0% forever — while every byte they describe is
still sitting on archive.org, one plain HTTP request away.

`ia-reviver` closes that gap: it finds the dead archive.org torrents in your
client, downloads the missing files **from archive.org over HTTP**, drops them
where qBittorrent expects them, forces a recheck, and restarts the torrent. The
result is a real seeder for a torrent that had none — no peer required.

## How it works

```
scan      qBittorrent ──► "these archive.org torrents have no seeder / are incomplete"
revive    for each torrent:
            1. identify the item (from the torrent comment / trackers)
            2. fetch the item's current .torrent and file manifest from archive.org
            3. compare with the files qBittorrent is missing
            4. PAUSE  the torrent            (so it cannot write while we do)
            5. FETCH  the missing files      (ranged, resumable, MD5-verified)
            6. RECHECK                        qBittorrent hashes what we wrote
            7. START + re-announce            the tracker learns the swarm has a seed
```

Step 6 is why this works rather than merely "looks like it works": qBittorrent
becomes a seeder only when **its own piece hashes** confirm the data. ia-reviver
never claims success on the strength of a download alone — it reports what the
client says after the recheck.

## Requirements

- **qBittorrent 4.x or 5.x**, with the WebUI enabled. Only HTTP(S) access to it
  is needed; the tool speaks the WebUI API v2.
- Network access to `archive.org`.
- Linux/macOS/anything with Python ≥ 3.11. **No Python dependencies** — stdlib
  only, on purpose, so it is trivial to package and run on a torrent box.

## Install / run

### NixOS and any Nix system

```console
$ nix run github:dbeley/archive-org-torrent-reviver -- --help
$ nix run github:dbeley/archive-org-torrent-reviver -- scan
```

From a checkout you also get:

```console
$ git clone https://github.com/dbeley/archive-org-torrent-reviver
$ cd archive-org-torrent-reviver
$ nix build . && ./result/bin/ia-reviver scan
$ nix develop            # dev shell: python, pytest, ruff, qbittorrent-nox
```

The flake provides `packages.ia-reviver`, `packages.default`, `apps.default`,
and a `devShells.default`. Version pinning is up to you: pin the flake input, or
add it as an input to your own configuration.

### Anywhere else

```console
$ pipx install git+https://github.com/dbeley/archive-org-torrent-reviver
$ # or
$ pip install --user git+https://github.com/dbeley/archive-org-torrent-reviver
```

## Usage

```console
$ ia-reviver check                      # is qBittorrent / archive.org reachable?
$ ia-reviver scan                       # read-only: what is dead, what is incomplete
$ ia-reviver scan --json                # machine-readable, for scripts
$ ia-reviver revive --all --dry-run     # show the plan: what, and how many GiB
$ ia-reviver revive --all               # do it (asks for confirmation)
$ ia-reviver revive AboutBan1935 --yes  # one torrent, by archive.org identifier
$ ia-reviver revive 01f4854dfcdc --yes  # …or by hash prefix
```

`scan` output:

```
qBittorrent v5.2.3 at http://127.0.0.1:8080
1 archive.org torrent(s); 1 need data, 1 with no seeder at all

STATE      SEEDS  PROGRESS  LEFT      FLAG  IDENTIFIER        HASH
---------  -----  --------  --------  ----  ----------------  ------------
stalledDL  0      0.0%      13.2 MiB  dead  ramrakshastotram  d9a84f76bd0b

run 'ia-reviver revive --all' to download the missing data from archive.org (or --dry-run first).
```

A revive that worked:

```
[1/1] ramrakshastotram — 16/16 files, 13.2 MiB to fetch
16/16 files · 13.2 MiB of 13.2 MiB · avg 613.5 KiB/s · 22s
✓ ramrakshastotram: revived — qBittorrent verified 100% of the data and is now seeding

summary
  1 revived and seeding, 0 partial, 0 failed
  13.2 MiB downloaded to archive.org's swarm
```

Refresh the torrent list in the qBittorrent WebUI and the torrent shows as
seeding.

### Verified against a real client and a real item

`ramrakshastotram` (16 files, 13,858,803 bytes) was added to a real qBittorrent
5.2.3 sitting at `stalledDL` with `num_complete = 0`. After
`ia-reviver revive --all --yes`:

- qBittorrent's own recheck reported 100% with `amount_left = 0`;
- the archive.org tracker reported **1 seeder** where it had reported 0;
- all 16 files on disk hashed byte-identical to the item's published MD5
  manifest — checked independently of this tool's own verifier.

## Configuration

Precedence, lowest to highest: **defaults → TOML file → environment → flags**.

Config file locations (first found wins):

1. `--config <path>`
2. `$IA_REVIVER_CONFIG`
3. `./ia-reviver.toml`
4. `~/.config/ia-reviver/config.toml`
5. `/etc/ia-reviver/config.toml`

Both flat and sectioned files are accepted:

```toml
# --- flat ---
qb_url = "http://127.0.0.1:8080"
qb_username = "admin"
qb_password_file = "/run/secrets/qbittorrent_password"

# --- or sectioned, same meaning ---
[qbittorrent]
url = "http://127.0.0.1:8080"
username = "admin"
password_file = "/run/secrets/qbittorrent_password"

[archive]
base_url = "https://archive.org"

[fetching]
jobs = 3
limit_rate = "8M"
verify = true
```

| Option | Env var | Default | Meaning |
|---|---|---|---|
| `qb_url` | `IA_REVIVER_QB_URL` | `http://127.0.0.1:8080` | WebUI URL (aliases: `QB_URL`) |
| `qb_username` | `IA_REVIVER_QB_USERNAME` | *(none)* | WebUI user (aliases: `QB_USERNAME`) |
| `qb_password` | `IA_REVIVER_QB_PASSWORD` | *(none)* | WebUI password (aliases: `QB_PASSWORD`) |
| `qb_password_file` | `IA_REVIVER_QB_PASSWORD_FILE` | *(none)* | read the password from a file |
| `verify_tls` | `IA_REVIVER_VERIFY_TLS` | `true` | `IA_REVIVER_INSECURE=true` inverts it |
| `archive_base_url` | `IA_REVIVER_ARCHIVE_BASE_URL` | `https://archive.org` | archive.org base URL |
| `jobs` | `IA_REVIVER_JOBS` | `3` | parallel file downloads |
| `limit_rate` | `IA_REVIVER_LIMIT_RATE` | *(unlimited)* | aggregate ceiling, e.g. `8M` |
| `verify` | `IA_REVIVER_VERIFY` | `true` | MD5-check files already on disk |
| `download_retries` | `IA_REVIVER_RETRIES` | `3` | attempts per file |
| `recheck_timeout` | `IA_REVIVER_RECHECK_TIMEOUT` | `900` | seconds to wait for hashing |
| `state_dir` | `IA_REVIVER_STATE_DIR` | `~/.local/state/ia-reviver` | locks + run history |

`qb_password_file` exists so the password can come from a systemd credential or
a sops-nix secret instead of the command line, where it would be visible in the
process table.

## Using it on a NixOS torrent host

The flake is deliberately just enough to *run* the tool — no NixOS module is
imposed. On a host running `services.qbittorrent`:

```console
# one-off, from the repo
$ nix run github:dbeley/archive-org-torrent-reviver -- \
    --qb-url http://127.0.0.1:8080 \
    --qb-username admin --qb-password-file /run/secrets/qbittorrent_password \
    scan
```

To have it available as a command and run periodically, add the flake as an
input to your configuration and wire it up yourself:

```nix
{
  inputs.ia-reviver.url = "github:dbeley/archive-org-torrent-reviver";

  # …
  environment.systemPackages = [ inputs.ia-reviver.packages.${pkgs.system}.default ];

  systemd.services.ia-reviver = {
    description = "Populate unseeded archive.org torrents from archive.org";
    startAt = "daily";
    serviceConfig = {
      Type = "oneshot";
      User = "qbittorrent";
      # A secret file, not a command-line argument:
      EnvironmentFile = "/run/secrets/ia-reviver.env"; # IA_REVIVER_QB_PASSWORD=…
      ExecStart = "${inputs.ia-reviver.packages.${pkgs.system}.default}/bin/ia-reviver revive --all --yes --limit-rate 20M";
    };
  };
}
```

See [`docs/nixos.md`](docs/nixos.md) for a longer walkthrough, including a
`--limit-rate`-throttled nightly job and systemd hardening.

## Safety

Things it does on purpose:

- **Never deletes anything.** It only writes into the torrent's own
  `save_path`; the single exception is the `.ia-reviver-part` file it created
  itself when a download fails or fails verification.
- **Refuses path traversal.** File names inside a torrent are attacker-supplied
  data; any `..`, absolute path or escaping destination is rejected before a
  single byte is written, and every destination is re-checked to be inside
  `save_path`.
- **Verifies before placing.** A file is renamed into position only after its
  size and (when archive.org publishes one) its MD5 match the item manifest, so
  a corrupt transfer can never be presented to qBittorrent as complete.
- **Detects stale torrents.** If the item's current `.torrent` no longer
  describes the same files as your local one, the recheck could never succeed;
  the tool says so and stops unless you pass `--force`.
- **Pauses the torrent while writing**, so qBittorrent cannot write the same
  files concurrently, and restores its pause/start state afterwards.
- **Asks before downloading.** The plan with real totals is always printed, and
  without `--yes` it prompts. It refuses to proceed non-interactively.
  `--max-bytes 50G` adds a hard ceiling.
- **Is resumable and idempotent.** Interrupt it and re-run: partial files resume
  with an HTTP `Range`, complete files are skipped.

## Troubleshooting

**`scan` works but `revive` fails with `HTTP 403 ... Forbidden`.**

qBittorrent answers a bare 403 when a request for a *private* API endpoint
arrives without a session it accepts, and that check runs before the URL is
routed — so reads can succeed while every write is refused, and even a
non-existent endpoint answers 403 rather than 404. Run `ia-reviver check -v`: it
now exercises a write up front and will tell you whether writes are authorised.
Then look at what the WebUI itself logged:

```console
$ journalctl -u qbittorrent --since "-15 min" \
    | grep -iE "csrf|origin|referer|host header|unauthor|forbidden"
```

Usual causes, in order of likelihood:

- the tool reaches the WebUI on a different host/port than the one you gave it
  (`qb_url`), e.g. through a reverse proxy or an SSH tunnel, so the request the
  WebUI sees does not match what it expects;
- `HostHeaderValidation` is enabled and the `Host` you connect with is not one
  the WebUI recognises;
- the session cookie is not surviving the trip (an authenticating proxy that
  strips it).

Note that `revive` now checks writes *before* planning, so this fails in seconds
rather than after planning an entire library.

## Limits and caveats

- **Only fills torrents you already have in qBittorrent.** Adding torrents from
  archive.org is your job (or your *arr stack's).
- **`--force` on a stale torrent wastes bandwidth and probably fails.** If
  archive.org has re-generated an item's torrent, re-add the current `.torrent`
  instead.
- **Access-restricted items cannot be helped.** If archive.org marks an item
  `access-restricted-item` / `stream_only`, the files are not downloadable and
  the tool says so instead of hammering it.
- **It cannot invent missing peers for material archive.org no longer hosts.**
  It fills from the item's *current* files.
- **Bandwidth is real.** A single item can be hundreds of GiB; use `--dry-run`,
  `--max-bytes` and `--limit-rate` before pointing it at a whole library.
- **Be a good citizen.** archive.org is a donation-funded non-profit. `--jobs 3`
  is the default rather than 20 for a reason; please keep it polite.

## Development

```console
$ nix develop
$ python3 -m unittest discover -s tests -t .   # 127 tests, fully offline
$ ruff check src tests
```

The test suite uses a **fake qBittorrent WebUI** and a **fake archive.org**,
both real HTTP servers on localhost, so the whole revive loop (login, plan,
range-resuming fetch, recheck, state reporting) is exercised without network
access. The fake client reproduces the behaviours that break naive clients:
`POST /api/v2/auth/login` answering **204 with an empty body** (5.x) rather than
`200 "Ok."` (4.x), a session cookie named `QBT_SID_<port>` rather than `SID`,
and `403` on every endpoint without a session.

Two real fixtures are checked in — `tests/data/ramrakshastotram.{torrent,metadata.json}`,
a real archive.org item — and the parser is asserted to reproduce that torrent's
real info-hash, so a parser regression cannot pass silently.

To poke a real qBittorrent locally (run these inside `nix develop`, which puts
`src/` on `PYTHONPATH`):

```console
$ scripts/dev-qbittorrent.sh start     # throwaway instance on :18080, bound to 127.0.0.1
$ python3 -m ia_reviver --config .dev-qbittorrent/ia-reviver.toml check
$ python3 -m ia_reviver --config .dev-qbittorrent/ia-reviver.toml scan
$ scripts/dev-qbittorrent.sh stop
```

The script writes a config pointing at a password *file*, so the throwaway
instance is exercised the same way a production one is.

### Layout

| Module | Responsibility |
|---|---|
| `bencode.py` | bencode decoding, info-hash, torrent parsing |
| `archive.py` | archive.org metadata / `.torrent` / ranged file access |
| `qbittorrent.py` | WebUI API v2 client (versions 4.x and 5.x) |
| `identify.py` | "is this torrent from archive.org, and which item?" |
| `planner.py` | which files are missing, where they go, what is unsafe |
| `downloader.py` | parallel, resumable, verified, rate-limited fetching |
| `revive.py` | orchestration: pause → fetch → recheck → seed |
| `config.py` | defaults / file / env / flag resolution |
| `progress.py`, `util.py` | output and helpers |
| `cli.py` | `scan`, `revive`, `check` |

## Contributing and security

- [CONTRIBUTING.md](CONTRIBUTING.md) — setup, and the ground rules (no
  dependencies, tests stay offline, success is reported by qBittorrent).
- [SECURITY.md](SECURITY.md) — how to report a vulnerability, and the trust
  boundary: torrent contents are untrusted input.
- [CHANGELOG.md](CHANGELOG.md)

## License

MIT — see [LICENSE](LICENSE).
