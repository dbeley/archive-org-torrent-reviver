# Security

## Reporting a vulnerability

Please use GitHub's [private vulnerability
reporting](https://github.com/dbeley/archive-org-torrent-reviver/security/advisories/new)
rather than a public issue. Include what you did, what happened, and what you
expected. If you cannot use that, open an issue that says only that you have a
security report and how to reach you — please don't put the details in it.

Expect an acknowledgement within a few days. This is a hobby project with no
funding and no bounty programme.

## What this tool touches

Worth knowing when assessing a report:

- **Qubittorrent WebUI credentials.** Read from a config file's
  `qb_password_file`, a `systemd` credential, or `IA_REVIVER_QB_PASSWORD`. They
  are sent to the WebUI to obtain a session cookie and are never written
  anywhere by this tool, never logged, and deliberately not accepted as a
  command-line argument (a flag would expose them in the process table).
- **Files in your torrent's `save_path`.** File names inside a `.torrent` are
  **attacker-supplied data** if the torrent came from anywhere untrusted. Any
  name that is absolute, contains `..`, or resolves outside `save_path` is
  rejected before a byte is written, and each destination is re-checked to be
  inside `save_path`. A bypass of that check is a security bug, not a bug.
- **Network access** to `archive.org` (HTTPS) and to your qBittorrent WebUI.
  Items are requested by identifier, and the host is fixed by
  `archive_base_url`, so a malicious torrent cannot make the tool fetch from a
  host of its choosing.
- **Nothing else.** It does not delete files other than the partial
  `*.ia-reviver-part` files it created itself, does not execute anything from a
  torrent, and does not listen on any port.

## Trust boundary

The tool trusts: your qBittorrent instance, archive.org, and your own config.
It does **not** trust torrent contents. If you find a case where torrent data
influences a path, a URL host, or a shell command, that is worth reporting.
