#!/usr/bin/env bash
# Spin up a throwaway qBittorrent, so the API client can be exercised for real
# without touching a production instance.
#
#   scripts/dev-qbittorrent.sh start   # start on :18080 with a known password
#   scripts/dev-qbittorrent.sh stop
#   scripts/dev-qbittorrent.sh status
#   scripts/dev-qbittorrent.sh log
#
# Profile and downloads live under .dev-qbittorrent/ (git-ignored). The instance
# is bound to 127.0.0.1 only.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
PROFILE="$ROOT/.dev-qbittorrent"
PORT="${QB_PORT:-18080}"
TORRENT_PORT="${QB_TORRENT_PORT:-16881}"
USERNAME="${QB_USERNAME:-admin}"
PASSWORD="${QB_PASSWORD:-ia-reviver-dev}"
PIDFILE="$PROFILE/qbittorrent.pid"
LOGFILE="$PROFILE/qbittorrent.log"

find_nox() {
  if command -v qbittorrent-nox >/dev/null 2>&1; then
    command -v qbittorrent-nox
    return
  fi
  # Fall back to nix, so this works from a bare checkout.
  nix build --no-link --print-out-paths 'nixpkgs#qbittorrent-nox' 2>/dev/null |
    while read -r path; do
      echo "$path/bin/qbittorrent-nox"
    done
}

write_config() {
  # Pre-seed the WebUI settings, including a PBKDF2 password hash, so no
  # interactive first-run step is needed.
  mkdir -p "$PROFILE/qBittorrent/config"
  local hash
  hash="$(
    QB_DEV_PASSWORD="$PASSWORD" python3 <<'PY'
import base64, hashlib, os
password = os.environ["QB_DEV_PASSWORD"].encode()
salt = os.urandom(16)
key = hashlib.pbkdf2_hmac("sha512", password, salt, 100000, dklen=64)
print(f"@ByteArray({base64.b64encode(salt).decode()}:{base64.b64encode(key).decode()})")
PY
  )"
  cat >"$PROFILE/qBittorrent/config/qBittorrent.conf" <<CONF
[LegalNotice]
Accepted=true

[Preferences]
WebUI\\Address=127.0.0.1
WebUI\\Port=$PORT
WebUI\\Username=$USERNAME
WebUI\\Password_PBKDF2="$hash"
WebUI\\CSRFProtection=false
WebUI\\HostHeaderValidation=false
WebUI\\LocalHostAuth=true
WebUI\\AuthSubnetWhitelistEnabled=false

[BitTorrent]
Session\\DefaultSavePath=$PROFILE/qBittorrent/downloads
CONF
  # The password goes to a file, never a command line, matching how the tool
  # expects to be configured in production.
  umask 077
  printf '%s' "$PASSWORD" >"$PROFILE/password"
  mkdir -p "$PROFILE/qBittorrent/downloads"
}

start() {
  if status >/dev/null 2>&1; then
    echo "already running (pid $(cat "$PIDFILE")) at http://127.0.0.1:$PORT"
    return 0
  fi
  local nox
  nox="$(find_nox)"
  if [ -z "$nox" ] || [ ! -x "$nox" ]; then
    echo "qbittorrent-nox not found: enter 'nix develop' first" >&2
    exit 1
  fi
  write_config
  "$nox" --profile="$PROFILE" --webui-port="$PORT" \
    --torrenting-port="$TORRENT_PORT" --confirm-legal-notice \
    >>"$LOGFILE" 2>&1 &
  echo $! >"$PIDFILE"
  for _ in $(seq 1 30); do
    if curl -sf -o /dev/null "http://127.0.0.1:$PORT/api/v2/app/version"; then
      break
    fi
    sleep 0.5
  done
  echo "qBittorrent listening on http://127.0.0.1:$PORT (user: $USERNAME)"
  echo "  profile:   $PROFILE"
  echo "  downloads: $PROFILE/qBittorrent/downloads"
  echo "  config:    $PROFILE/ia-reviver.toml (written by this script)"
  cat >"$PROFILE/ia-reviver.toml" <<CONF
qb_url = "http://127.0.0.1:$PORT"
qb_username = "$USERNAME"
qb_password_file = "$PROFILE/password"
jobs = 3
state_dir = "$PROFILE/state"
CONF
  echo "  usage:     python3 -m ia_reviver --config $PROFILE/ia-reviver.toml scan"
}

stop() {
  if [ -f "$PIDFILE" ]; then
    kill "$(cat "$PIDFILE")" 2>/dev/null || true
    rm -f "$PIDFILE"
    echo "stopped"
  else
    echo "not running"
  fi
}

status() {
  if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "running (pid $(cat "$PIDFILE")) at http://127.0.0.1:$PORT"
    return 0
  fi
  echo "not running"
  return 1
}

case "${1:-start}" in
  start) start ;;
  stop) stop ;;
  status) status ;;
  log) tail -n 40 "$LOGFILE" ;;
  *) echo "usage: $0 {start|stop|status|log}" >&2; exit 2 ;;
esac
