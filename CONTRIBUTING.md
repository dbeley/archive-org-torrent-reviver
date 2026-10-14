# Contributing

Thanks for looking. Bug reports and pull requests are welcome.

## Ground rules

- **No runtime dependencies.** The tool is stdlib-only on purpose, so it drops
  onto a torrent box with nothing to install. Please don't add a dependency
  without a very good reason — open an issue first and make the case.
- **Success is reported by qBittorrent, not by the download.** The whole point
  of this tool is step 6 (its own recheck). Code that claims a revive worked on
  the strength of a completed HTTP fetch will not be merged.
- **Tests stay offline.** The suite must pass with no network access and no
  qBittorrent installed. New behaviour gets a test against the fakes in
  `tests/fakes.py`; if your change needs a new qBittorrent quirk modelled, add
  it there rather than mocking the client itself.
- **Be careful with paths.** Anything derived from torrent contents is
  untrusted input (see [SECURITY.md](SECURITY.md)).

## Getting set up

```console
$ nix develop                       # python, pytest, ruff, qbittorrent-nox
$ python3 -m unittest discover -s tests -t . -v
$ python3 -W error::ResourceWarning -m unittest discover -s tests -t .
$ ruff check src tests
$ ruff format src tests
```

Without Nix, any Python ≥ 3.11 works; there is nothing to install. Put `src/`
on `PYTHONPATH` to run the CLI from a checkout:

```console
$ PYTHONPATH=src python3 -m ia_reviver scan
```

To exercise it against a real qBittorrent, use the throwaway instance the repo
can start for you — never a production one:

```console
$ scripts/dev-qbittorrent.sh start     # :18080, bound to 127.0.0.1, git-ignored
$ PYTHONPATH=src python3 -m ia_reviver --config .dev-qbittorrent/ia-reviver.toml scan
$ scripts/dev-qbittorrent.sh stop
```

## Before opening a PR

- `python3 -m unittest discover -s tests -t .` passes, including under
  `-W error::ResourceWarning` (unclosed sockets are treated as failures).
- `ruff check src tests` and `ruff format --check src tests` are clean — CI
  enforces both.
- `nix flake check` passes, if you touched the flake.
- Any user-visible change updates `README.md`, and notable behaviour goes in
  `CHANGELOG.md` under `Unreleased`.

## Scope

Deliberately *not* in scope, so you don't waste time on a PR:

- Adding torrents to qBittorrent from archive.org. That is your job, or your
  *arr stack's.
- Items archive.org marks `access-restricted-item` / `stream_only` — the files
  are not downloadable, so there is nothing to fill from.
- Getting qBittorrent (libtorrent) to *finish* a torrent from archive.org's web
  seed. BEP 19 is implemented and the client does download from that seed — it
  was measured stopping at 80–87% and never resuming — so the web-seed plumbing
  itself is not the thing to fix.
