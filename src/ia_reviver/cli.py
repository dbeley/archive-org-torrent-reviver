"""Command line interface.

Two verbs carry the whole tool:

``scan``
    List the archive.org torrents already in qBittorrent and say which ones are
    dead (no seeder) or incomplete — read-only, no side effects.

``revive``
    Ask archive.org for the bytes those torrents are missing, recheck, and leave
    them seeding. It prints exactly what it will fetch and how much, and asks
    before downloading anything unless ``--yes`` is given.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .archive import ArchiveOrg, ArchiveOrgError
from .config import Config, ConfigError, load_config
from .downloader import Downloader
from .progress import Console
from .qbittorrent import QbitClient, QbitError
from .revive import (
    Candidate,
    ReviveOutcome,
    build_plans,
    collect_candidates,
    revive_plan,
    write_state,
)
from .util import format_table, human_bytes, is_tty, parse_size, short_hash

PROG = "ia-reviver"

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_ERROR = 2

_EPILOG = """\
examples:
  # what archive.org torrents are in qBittorrent, and which ones are dead?
  ia-reviver scan

  # fill every dead/incomplete archive.org torrent (asks for confirmation)
  ia-reviver revive --all

  # one torrent, by identifier or by hash prefix, without prompting
  ia-reviver revive AboutBan1935 --yes
  ia-reviver revive 01f4854dfcdc --yes

  # dry run: show the fetch plan, download nothing
  ia-reviver revive --all --dry-run

Configuration is read from ia-reviver.toml, ~/.config/ia-reviver/config.toml or
/etc/ia-reviver/config.toml, from IA_REVIVER_* environment variables, then from
flags. See README.md for the full list.
"""


def _global_options() -> argparse.ArgumentParser:
    """Options accepted both before and after the sub-command.

    ``default=SUPPRESS`` everywhere is deliberate: without it a sub-parser would
    reset options that were already parsed at the top level to their defaults.
    """
    parser = argparse.ArgumentParser(add_help=False)
    group = parser.add_argument_group("connection")
    group.add_argument("--qb-url", default=argparse.SUPPRESS, help="qBittorrent WebUI URL")
    group.add_argument("--qb-username", default=argparse.SUPPRESS, help="WebUI username")
    group.add_argument("--qb-password", default=argparse.SUPPRESS, help="WebUI password")
    group.add_argument(
        "--qb-password-file",
        default=argparse.SUPPRESS,
        help="read the WebUI password from this file (systemd credential, sops secret)",
    )
    group.add_argument(
        "--no-verify-tls",
        dest="verify_tls",
        action="store_false",
        default=argparse.SUPPRESS,
        help="skip TLS verification of the qBittorrent WebUI",
    )
    group.add_argument("--config", default=argparse.SUPPRESS, help="path to a TOML config file")

    group = parser.add_argument_group("fetching")
    group.add_argument(
        "--jobs", type=int, default=argparse.SUPPRESS, help="parallel downloads (default: 3)"
    )
    group.add_argument(
        "--limit-rate",
        default=argparse.SUPPRESS,
        metavar="SIZE",
        help="aggregate download ceiling, e.g. 8M (default: unlimited)",
    )
    group.add_argument(
        "--no-verify",
        dest="verify",
        action="store_false",
        default=argparse.SUPPRESS,
        help="do not MD5-check files that are already on disk (faster, less safe)",
    )
    group.add_argument(
        "--state-dir",
        default=argparse.SUPPRESS,
        help="where locks and the run history live (default: ~/.local/state/ia-reviver)",
    )
    group.add_argument(
        "--recheck-timeout",
        type=float,
        default=argparse.SUPPRESS,
        help="seconds to wait for qBittorrent to finish rechecking (default: 900)",
    )

    group = parser.add_argument_group("output")
    group.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS)
    group.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS)
    return parser


def build_parser() -> argparse.ArgumentParser:
    common = _global_options()
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Revive dead archive.org torrents by populating them from archive.org itself.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    scan = subparsers.add_parser(
        "scan",
        parents=[common],
        help="list archive.org torrents in qBittorrent and flag the dead ones",
        description="Read-only: show which archive.org torrents qBittorrent holds.",
    )
    scan.add_argument("--json", action="store_true", help="machine-readable output")
    scan.add_argument(
        "--needing",
        action="store_true",
        help="only torrents that are incomplete or have missing files",
    )
    scan.add_argument(
        "--complete",
        action="store_true",
        help="also list archive.org torrents that are already fully downloaded",
    )

    revive = subparsers.add_parser(
        "revive",
        parents=[common],
        help="download the missing data from archive.org and recheck",
        description="Populate incomplete archive.org torrents from archive.org itself.",
    )
    revive.add_argument(
        "targets",
        nargs="*",
        metavar="TARGET",
        help="torrent hash (or prefix), info-hash prefix, or archive.org identifier",
    )
    revive.add_argument("--all", action="store_true", help="every torrent that needs data")
    revive.add_argument(
        "--dry-run", action="store_true", help="show the plan without downloading anything"
    )
    revive.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    revive.add_argument(
        "--force",
        action="store_true",
        help="proceed even if the local torrent is stale compared to archive.org",
    )
    revive.add_argument(
        "--no-pause",
        dest="pause",
        action="store_false",
        default=True,
        help="do not pause the torrent while its files are written",
    )
    revive.add_argument(
        "--start",
        dest="start",
        action="store_true",
        default=None,
        help="start the torrent afterwards even if it was paused before",
    )
    revive.add_argument(
        "--keep-stopped",
        dest="start",
        action="store_false",
        default=None,
        help="leave the torrent stopped after the recheck",
    )
    revive.add_argument(
        "--max-bytes",
        metavar="SIZE",
        help="refuse to fetch more than this in one run, e.g. 50G",
    )

    subparsers.add_parser(
        "check",
        parents=[common],
        help="verify the qBittorrent and archive.org connections",
        description="Diagnose configuration and connectivity.",
    )
    return parser


# --------------------------------------------------------------------- helpers


def _console(namespace: argparse.Namespace) -> Console:
    """Console honouring ``-q``/``-v``, which may be absent (argparse SUPPRESS)."""
    return Console(
        quiet=getattr(namespace, "quiet", False),
        verbose=getattr(namespace, "verbose", False),
    )


def connect(
    namespace: argparse.Namespace, console: Console
) -> tuple[QbitClient, ArchiveOrg, Config]:
    overrides = {
        key: getattr(namespace, key, None)
        for key in (
            "qb_url",
            "qb_username",
            "qb_password",
            "qb_password_file",
            "verify_tls",
            "config",
            "jobs",
            "limit_rate",
            "verify",
            "state_dir",
            "recheck_timeout",
        )
    }
    config = load_config(config_file=getattr(namespace, "config", None), overrides=overrides)
    client = QbitClient(
        config.qb_url,
        username=config.qb_username,
        password=config.password(),
        timeout=config.qb_timeout,
        verify_tls=config.verify_tls,
    )
    archive = ArchiveOrg(
        config.archive_base_url,
        timeout=config.archive_timeout,
        retries=config.archive_retries,
    )
    console.detail(f"qBittorrent at {config.qb_url}")
    if config.qb_username is None:
        console.detail("no username configured: assuming localhost access without authentication")
    return client, archive, config


def _candidate_rows(candidates: list[Candidate]) -> list[list[str]]:
    rows: list[list[str]] = []
    for candidate in candidates:
        torrent = candidate.torrent
        seeders = "?" if torrent.num_complete < 0 else str(torrent.num_complete)
        rows.append(
            [
                torrent.state,
                seeders,
                f"{torrent.progress * 100:.1f}%",
                human_bytes(torrent.amount_left) if torrent.amount_left else "-",
                candidate.flag,
                candidate.identifier,
                short_hash(torrent.hash),
            ]
        )
    return rows


def _candidate_payload(candidates: list[Candidate]) -> list[dict[str, object]]:
    payload: list[dict[str, object]] = []
    for candidate in candidates:
        torrent = candidate.torrent
        payload.append(
            {
                "identifier": candidate.identifier,
                "name": torrent.name,
                "hash": torrent.hash,
                "state": torrent.state,
                "progress": torrent.progress,
                "amount_left": torrent.amount_left,
                "size": torrent.size,
                "num_complete": torrent.num_complete,
                "num_incomplete": torrent.num_incomplete,
                "num_seeds": torrent.num_seeds,
                "tracker": torrent.tracker,
                "save_path": torrent.save_path,
                "content_path": torrent.content_path,
                "flag": candidate.flag,
                "needs_help": candidate.needs_help,
                "detected_from": candidate.match.sources,
                "archive_url": f"https://archive.org/details/{candidate.identifier}",
            }
        )
    return payload


def _resolve_targets(
    specs: list[str], candidates: list[Candidate], console: Console
) -> tuple[list[Candidate], list[str]]:
    """Match user-supplied targets against known torrents."""
    if not specs:
        return [], []
    selected: list[Candidate] = []
    missing: list[str] = []
    for spec in specs:
        needle = spec.strip().lower()
        found = None
        for candidate in candidates:
            torrent = candidate.torrent
            if (
                torrent.hash.lower().startswith(needle)
                or candidate.identifier.lower() == needle
                or torrent.name.lower() == needle
            ):
                found = candidate
                break
        if found is None:
            missing.append(spec)
        elif found not in selected:
            selected.append(found)
    return selected, missing


def _confirm(prompt: str, *, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not (sys.stdin.isatty() and is_tty()):
        print(
            "refusing to download without confirmation: stdin is not a terminal. "
            "Re-run with --yes (or --dry-run to only see the plan).",
            file=sys.stderr,
        )
        return False
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in {"y", "yes"}


def _print_plan(console: Console, plans, *, verbose: bool) -> None:
    rows = []
    for plan in plans:
        rows.append(
            [
                plan.identifier,
                short_hash(plan.local_info_hash),
                str(len(plan.files)),
                str(plan.to_fetch),
                human_bytes(plan.to_fetch_bytes),
                "stale" if plan.stale else "ok",
            ]
        )
    console.info(
        format_table(["IDENTIFIER", "INFO-HASH", "FILES", "TO FETCH", "SIZE", "STATE"], rows)
    )
    if verbose:
        for plan in plans:
            for planned in plan.files:
                if planned.action == "fetch":
                    console.detail(f"fetch {planned.item_name} ({human_bytes(planned.size)})")
    for plan in plans:
        for warning in plan.warnings:
            console.warn(f"{plan.identifier}: {warning}")


# -------------------------------------------------------------------- commands


def cmd_scan(namespace: argparse.Namespace) -> int:
    console = _console(namespace)
    client, _archive, _config = connect(namespace, console)
    try:
        client.login()
    except QbitError as exc:
        console.error(str(exc))
        return EXIT_ERROR

    candidates = collect_candidates(
        client,
        needing_only=namespace.needing,
        include_complete=namespace.complete,
    )
    if namespace.json:
        print(json.dumps(_candidate_payload(candidates), indent=2))
        return EXIT_OK

    todo = [c for c in candidates if c.needs_help]
    console.section(f"qBittorrent {client.app_version()} at {client.url}")
    if not candidates:
        console.info("no archive.org torrents found in qBittorrent.")
        console.info("add some from https://archive.org/details/<identifier> and re-run.")
        return EXIT_OK
    console.info(
        f"{len(candidates)} archive.org torrent(s); "
        f"{len(todo)} need data, "
        f"{sum(1 for c in candidates if c.dead and c.needs_help)} with no seeder at all"
    )
    console.blank()
    console.info(
        format_table(
            ["STATE", "SEEDS", "PROGRESS", "LEFT", "FLAG", "IDENTIFIER", "HASH"],
            _candidate_rows(candidates),
        )
    )
    if todo:
        console.blank()
        console.info(
            f"run '{PROG} revive --all' to download the missing data from archive.org "
            "(or --dry-run first)."
        )
    return EXIT_OK


def cmd_check(namespace: argparse.Namespace) -> int:
    console = _console(namespace)
    try:
        client, archive, config = connect(namespace, console)
    except ConfigError as exc:
        console.error(str(exc))
        return EXIT_ERROR

    failures = 0
    try:
        version = client.login()
        console.ok(f"qBittorrent {version} at {client.url} (WebUI API {client.webapi_version()})")
    except QbitError as exc:
        console.error(f"qBittorrent: {exc}")
        return EXIT_ERROR

    preferences = client.preferences()
    if preferences.get("save_path"):
        console.info(f"  default save path: {preferences['save_path']}")
    torrents = client.torrents()
    candidates = collect_candidates(client)
    console.info(f"  {len(torrents)} torrent(s), {len(candidates)} from archive.org")

    try:
        payload = archive.metadata("nasa")
        if payload:
            console.ok(f"archive.org reachable at {archive.base_url}")
        else:  # pragma: no cover - unexpected
            console.warn(f"archive.org answered but returned no metadata at {archive.base_url}")
            failures += 1
    except ArchiveOrgError as exc:
        console.error(f"archive.org: {exc}")
        failures += 1

    console.info(
        f"  config: jobs={config.jobs}, verify={config.verify}, "
        f"limit_rate={config.limit_rate or 'unlimited'}"
    )
    return EXIT_PARTIAL if failures else EXIT_OK


def cmd_revive(namespace: argparse.Namespace) -> int:
    console = _console(namespace)
    try:
        client, archive, config = connect(namespace, console)
    except ConfigError as exc:
        console.error(str(exc))
        return EXIT_ERROR

    try:
        client.login()
    except QbitError as exc:
        console.error(str(exc))
        return EXIT_ERROR

    all_candidates = collect_candidates(client, include_complete=True)
    todo = [c for c in all_candidates if c.needs_help]
    if not all_candidates:
        console.info("no archive.org torrents found in qBittorrent.")
        return EXIT_OK

    if namespace.targets:
        selected, missing = _resolve_targets(namespace.targets, all_candidates, console)
        for spec in missing:
            console.warn(
                f"'{spec}' does not match any archive.org torrent in qBittorrent. "
                "Add it in the WebUI first (this tool only fills torrents you already have)."
            )
        if not selected:
            return EXIT_ERROR
    elif namespace.all:
        selected = todo
        if not selected:
            console.ok("every archive.org torrent in qBittorrent is already complete.")
            return EXIT_OK
    else:
        selected = todo
        if not selected:
            console.ok("every archive.org torrent in qBittorrent is already complete.")
            return EXIT_OK
        console.info(f"no target given: considering all {len(todo)} torrent(s) that need data")

    console.section(f"planning {len(selected)} torrent(s)")
    plans, failures = build_plans(
        client, archive, selected, verify=config.verify, force=namespace.force, console=console
    )
    plans = [p for p in plans if p.to_fetch or p.touch_files]
    if not plans:
        console.ok("nothing to fetch: the selected torrents already have all their data locally.")
        return EXIT_PARTIAL if failures else EXIT_OK

    total_bytes = sum(p.to_fetch_bytes for p in plans)
    total_files = sum(p.to_fetch for p in plans)
    console.blank()
    _print_plan(console, plans, verbose=getattr(namespace, "verbose", False))
    console.blank()
    console.info(
        f"total: {total_files} file(s), {human_bytes(total_bytes)} to download "
        f"from archive.org, {len(plans)} torrent(s)"
    )

    if namespace.max_bytes:
        limit = parse_size(namespace.max_bytes)
        if total_bytes > limit:
            console.error(
                f"the plan exceeds --max-bytes ({human_bytes(total_bytes)} > "
                f"{human_bytes(limit)}); narrow the selection"
            )
            return EXIT_ERROR

    if namespace.dry_run:
        console.info("dry run: nothing was downloaded.")
        return EXIT_OK

    if not _confirm("download the missing data and recheck now?", assume_yes=namespace.yes):
        console.info("aborted; nothing was downloaded.")
        return EXIT_OK

    downloader = Downloader(
        archive,
        jobs=config.jobs,
        limit_rate=config.limit_rate,
        verify=config.verify,
        retries=config.download_retries,
        console=console,
    )

    outcomes: list[ReviveOutcome] = []
    for index, plan in enumerate(plans, start=1):
        console.blank()
        console.section(f"[{index}/{len(plans)}] {plan.identifier} — {plan.memory_summary}")
        outcome = revive_plan(
            client,
            downloader,
            plan,
            pause=namespace.pause,
            start=namespace.start,
            recheck_timeout=config.recheck_timeout,
            state_dir=config.state_path,
            console=console,
        )
        outcomes.append(outcome)
        if outcome.ok:
            console.ok(f"{outcome.identifier}: {outcome.summary}")
        elif outcome.error:
            console.error(f"{outcome.identifier}: {outcome.summary}")
        else:
            console.warn(f"{outcome.identifier}: {outcome.summary}")

    write_state(config.state_path, outcomes)

    console.blank()
    console.section("summary")
    revived = [o for o in outcomes if o.ok]
    seeding = [o for o in revived if o.seeding]
    held_back = [o for o in revived if not o.seeding]
    partial = [o for o in outcomes if not o.ok and not o.error]
    errored = [o for o in outcomes if o.error]
    counts = [f"{len(seeding)} revived and seeding"]
    if held_back:
        counts.append(f"{len(held_back)} revived but stopped (start to seed)")
    counts.append(f"{len(partial)} partial")
    counts.append(f"{len(errored)} failed")
    console.info("  " + ", ".join(counts))
    console.info(
        f"  {human_bytes(sum(o.fetched_bytes for o in outcomes))} downloaded to archive.org's swarm"
    )
    if errored:
        for outcome in errored:
            console.error(f"  {outcome.identifier}: {outcome.error}")

    if errored:
        return EXIT_ERROR
    return EXIT_PARTIAL if partial else EXIT_OK


_COMMANDS = {"scan": cmd_scan, "revive": cmd_revive, "check": cmd_check}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    namespace = parser.parse_args(argv)
    if not namespace.command:
        parser.print_help()
        return EXIT_OK
    handler = _COMMANDS[namespace.command]
    try:
        return handler(namespace)
    except KeyboardInterrupt:
        print(
            "\ninterrupted — partial downloads are kept and resume next run.",
            file=sys.stderr,
        )
        return 130
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except QbitError as exc:
        print(f"qBittorrent error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except ArchiveOrgError as exc:
        print(f"archive.org error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
