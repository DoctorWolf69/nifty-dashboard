#!/usr/bin/env bash
# Rolling-window retention: delete tick archives and replay timeline caches
# older than RETENTION_DAYS (default 30). Journals, EOD filings and the
# golden fixtures are kept forever - they are small and irreplaceable.
#
# ponytail: does not verify the file was backed up before deleting - the
# nightly backup timer plus the quarterly restore drill are the guard. Add
# an rclone existence check here if that ever proves insufficient.
set -euo pipefail

APP_DIR="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
DAYS="${RETENTION_DAYS:-30}"

echo "[retention] window ${DAYS}d under $APP_DIR"
find "$APP_DIR/data/live_nifty_oi" -maxdepth 1 -name 'nifty_oi_ticks_*.sqlite*' -mtime +"$DAYS" -print -delete
find "$APP_DIR/data/live_nifty_oi" -maxdepth 1 -name 'nifty_slim_*.sqlite*'    -mtime +"$DAYS" -print -delete
find "$APP_DIR/data/replay"        -maxdepth 1 -name 'timeline_*.json.gz'      -mtime +"$DAYS" -print -delete
find "$APP_DIR/data/replay"        -maxdepth 1 -name 'report_*.html'           -mtime +"$DAYS" -print -delete
echo "[retention] done"
