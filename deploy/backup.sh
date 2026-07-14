#!/usr/bin/env bash
# Nightly off-box backup of the desk's irreplaceable data.
#
# Ships: recent tick archives (consistent sqlite .backup, gzipped),
#        journal/ (context artifacts, paper trades, reports),
#        small live_nifty_oi sidecars (iv_history.jsonl, orb_*.json).
# Skips: data/nse_eod raw CSVs (re-downloadable from NSE archives),
#        data/replay timelines (rebuildable, version-keyed caches).
#
# One-time setup on the box:
#   sudo apt-get install -y rclone sqlite3
#   rclone config                # create a remote, e.g. "b2" or "gdrive"
#   set BACKUP_REMOTE in the systemd unit, e.g. b2:nifty-desk-backup
#
# Restore drill (run quarterly, seriously):
#   rclone copy "$BACKUP_REMOTE/$(hostname)/<date>" /tmp/restore
#   gunzip /tmp/restore/nifty_oi_ticks_<day>.sqlite.gz
#   move into data/live_nifty_oi/ on a clean checkout, open /replay for that
#   day - if the dashboard renders, the backup is real.
set -euo pipefail

APP_DIR="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
REMOTE="${BACKUP_REMOTE:-}"
STAMP="$(date +%F)"
DEST="$REMOTE/$(hostname)/$STAMP"

if [ -z "$REMOTE" ]; then
  echo "[backup] BACKUP_REMOTE is not set - refusing to silently do nothing." >&2
  echo "[backup] Configure an rclone remote and set BACKUP_REMOTE in nifty-backup.service." >&2
  exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Tick DBs touched in the last 2 days: consistent snapshot via sqlite3 .backup
# (safe even if a writer is attached), then gzip (~6-10x on this schema).
find "$APP_DIR/data/live_nifty_oi" -maxdepth 1 -name '*.sqlite' -mtime -2 -print0 |
  while IFS= read -r -d '' db; do
    base="$(basename "$db")"
    if command -v sqlite3 >/dev/null 2>&1; then
      sqlite3 "$db" ".backup '$WORK/$base'"
    else
      cp "$db" "$WORK/$base"   # fallback; fine when the desk is stopped
    fi
    gzip -f "$WORK/$base"
  done

# Everything small and irreplaceable in one tar: journal artifacts + sidecars.
tar -czf "$WORK/journal_$STAMP.tar.gz" \
  -C "$APP_DIR" journal \
  $(cd "$APP_DIR" && ls data/live_nifty_oi/*.jsonl data/live_nifty_oi/*.json 2>/dev/null || true)

rclone copy "$WORK" "$DEST" --transfers 2 --checkers 4
echo "[backup] shipped $(ls "$WORK" | wc -l) files -> $DEST"
