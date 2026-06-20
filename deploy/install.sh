#!/usr/bin/env bash
# Install the NIFTY desk systemd units + timers.
# Usage:  sudo ./deploy/install.sh [APPDIR] [USER]
# Defaults: APPDIR=/opt/nifty-dashboard  USER=nifty
set -euo pipefail

APPDIR="${1:-/opt/nifty-dashboard}"
RUNUSER="${2:-nifty}"
UNITDIR="/etc/systemd/system"
SRC="$(cd "$(dirname "$0")/systemd" && pwd)"

echo "Installing units from $SRC"
echo "  APPDIR=$APPDIR  USER=$RUNUSER"

for f in "$SRC"/*.service "$SRC"/*.timer; do
  base="$(basename "$f")"
  sed -e "s|__APPDIR__|$APPDIR|g" -e "s|__USER__|$RUNUSER|g" "$f" > "$UNITDIR/$base"
done

systemctl daemon-reload

# Enable the timers (the dashboard/gift services are started BY the timers, not at boot).
systemctl enable --now \
  nifty-morning.timer \
  nifty-premarket.timer \
  nifty-dashboard-start.timer \
  nifty-dashboard-stop.timer \
  nifty-gift-start.timer \
  nifty-gift-stop.timer \
  nifty-session-report.timer \
  nifty-eod-fii.timer \
  nifty-eod-cm.timer \
  nifty-eod-fo.timer \
  nifty-eod-retry.timer \
  nifty-eod-filing.timer \
  nifty-email-report.timer

# Always-on replay/backtest service (independent of market hours).
systemctl enable --now nifty-replay.service

echo
echo "Done. Scheduled timers:"
systemctl list-timers 'nifty-*' --no-pager || true
echo
echo "Manual controls:"
echo "  sudo systemctl start nifty-dashboard      # start now (also auto at 09:10 IST)"
echo "  journalctl -u nifty-dashboard -f          # live logs"
