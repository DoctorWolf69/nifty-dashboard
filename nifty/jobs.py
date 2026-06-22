"""Single CLI dispatcher for the scheduled NIFTY desk jobs.

Replaces the old apscheduler supervisor — each job is fired by its own systemd
timer (see deploy/systemd/). Usage:

    python -m nifty.jobs morning            # 08:15  Phases 1-4 morning pipeline
    python -m nifty.jobs premarket          # 09:01  pre-open scan + desk brief
    python -m nifty.jobs eod --targets fii_dii,india_vix
    python -m nifty.jobs eod --retry-missing
    python -m nifty.jobs eod-filing         # 19:35  FII/DII filing for tomorrow
    python -m nifty.jobs session-report     # 15:40  intraday EOD report
    python -m nifty.jobs email-report       # 20:15  regenerate + email report zip

By default a job no-ops on weekends / NSE trading holidays. Pass --force to run
regardless (useful for manual backfills).
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from typing import Optional, Set
from zoneinfo import ZoneInfo

import requests

IST = ZoneInfo("Asia/Kolkata")
_HOLIDAYS: dict = {"loaded": "", "dates": set()}


def _ist_today() -> date:
    return datetime.now(IST).date()


def load_trading_holidays(force: bool = False) -> Set[str]:
    """NSE trading-holiday dates (ISO strings), cached for the day."""
    today_key = _ist_today().isoformat()
    if not force and _HOLIDAYS["loaded"] == today_key:
        return _HOLIDAYS["dates"]
    dates: Set[str] = set()
    try:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
                "Referer": "https://www.nseindia.com/",
            }
        )
        session.get("https://www.nseindia.com", timeout=15)
        resp = session.get("https://www.nseindia.com/api/holiday-master?type=trading", timeout=20)
        resp.raise_for_status()
        for section in resp.json().values():
            if not isinstance(section, list):
                continue
            for row in section:
                raw = str(row.get("tradingDate") or "")
                if not raw:
                    continue
                try:
                    dates.add(datetime.strptime(raw, "%d-%b-%Y").date().isoformat())
                except ValueError:
                    continue
    except Exception as exc:  # network/parse failure -> treat as no holidays
        print(f"[jobs] holiday fetch failed ({exc}); assuming trading day")
    _HOLIDAYS["loaded"] = today_key
    _HOLIDAYS["dates"] = dates
    return dates


def is_trading_day(day: Optional[date] = None) -> bool:
    current = day or _ist_today()
    if current.weekday() >= 5:
        return False
    return current.isoformat() not in load_trading_holidays()


def _delegate(module_main, argv: list[str]) -> None:
    """Run a module's argparse-based main() with a synthetic argv."""
    saved = sys.argv
    sys.argv = argv
    try:
        module_main()
    finally:
        sys.argv = saved


def cmd_morning(_args: argparse.Namespace) -> None:
    from nifty.morning.phases import run_morning_pipeline

    summary = run_morning_pipeline()
    print(f"[jobs] morning pipeline complete: {summary.get('phases_completed')}")


def cmd_premarket(_args: argparse.Namespace) -> None:
    from nifty.morning.phases import run_premarket_scan

    summary = run_premarket_scan()
    print(f"[jobs] premarket scan complete: bias={summary.get('combined_bias')}")


def cmd_eod(args: argparse.Namespace) -> None:
    from nifty.sources import nse_eod

    argv = ["nse_eod"]
    if args.targets:
        argv += ["--targets", args.targets]
    if args.retry_missing:
        argv += ["--retry-missing"]
    if args.previous:
        argv += ["--previous"]
    _delegate(nse_eod.main, argv)


def cmd_eod_filing(_args: argparse.Namespace) -> None:
    from nifty.eod import filing

    _delegate(filing.main, ["eod_filing"])


def cmd_session_report(_args: argparse.Namespace) -> None:
    from nifty.eod import session_report

    _delegate(session_report.main, ["session_report"])


def cmd_email_report(_args: argparse.Namespace) -> None:
    from nifty.eod import email_report

    _delegate(email_report.main, ["email_report"])


_COMMANDS = {
    "morning": cmd_morning,
    "premarket": cmd_premarket,
    "eod": cmd_eod,
    "eod-filing": cmd_eod_filing,
    "session-report": cmd_session_report,
    "email-report": cmd_email_report,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="NIFTY desk scheduled jobs")
    parser.add_argument("command", choices=sorted(_COMMANDS), help="Job to run")
    parser.add_argument("--force", action="store_true", help="Run even on weekends/holidays")
    parser.add_argument("--targets", default="", help="(eod) comma-separated NSE targets")
    parser.add_argument("--retry-missing", action="store_true", help="(eod) retry failed targets only")
    parser.add_argument("--previous", action="store_true", help="(eod) use previous trading day")
    args = parser.parse_args()

    if not args.force and not is_trading_day():
        print(f"[jobs] {_ist_today().isoformat()} is not a trading day — skipping '{args.command}'. Use --force to override.")
        return

    _COMMANDS[args.command](args)


if __name__ == "__main__":
    main()
