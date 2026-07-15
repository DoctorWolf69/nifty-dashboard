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


def cmd_liveness(_args: argparse.Namespace) -> None:
    """09:25 IST tripwire: the desk must have ticks by now on a trading day.

    Exists because the desk ran DARK from 26 Jun to 15 Jul 2026 - no Kite
    login, zero ticks, while every timer stayed green and reports kept
    emailing. Fails the unit (visible red in systemd) and emails the report
    recipients so a missed morning login can never be silent again.
    """
    import os
    import smtplib
    import sqlite3
    import sys
    from email.mime.text import MIMEText

    from dotenv import load_dotenv

    from nifty.paths import DATA_LIVE_OI, ENV_FILE

    label = _ist_today().isoformat()
    rows = 0
    checks = [
        (DATA_LIVE_OI / f"nifty_oi_ticks_{label}.sqlite", "option_ticks"),
        (DATA_LIVE_OI / f"nifty_slim_{label}.sqlite", "tick"),
    ]
    for path, table in checks:
        if not path.exists():
            continue
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            rows += int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            conn.close()
        except sqlite3.Error:
            pass
        if rows:
            break
    if rows:
        print(f"[jobs] liveness OK: {rows} ticks recorded today")
        return

    print(f"[jobs] liveness FAIL: zero ticks on trading day {label}")
    load_dotenv(ENV_FILE)
    sender = os.getenv("REPORT_EMAIL_FROM", "").strip()
    password = os.getenv("REPORT_EMAIL_APP_PASSWORD", "").strip()
    to = [addr.strip() for addr in os.getenv("REPORT_EMAIL_TO", "").split(",") if addr.strip()]
    if sender and password and to:
        base = os.getenv("REPORT_PUBLIC_URL", "").strip().removesuffix("/reports")
        body = (
            f"The NIFTY desk has recorded ZERO ticks today ({label}) as of 09:25 IST.\n\n"
            f"Almost always this means the morning Kite login was missed.\n"
            f"Fix (30 seconds): open {base or 'https://<your-domain>'}/kite/login "
            f"and complete the Zerodha 2FA.\n\n"
            f"Every dark day is tick history lost permanently."
        )
        msg = MIMEText(body)
        msg["Subject"] = f"ALERT: NIFTY desk DARK - no ticks {label}"
        msg["From"] = sender
        msg["To"] = ", ".join(to)
        host = os.getenv("REPORT_SMTP_HOST", "smtp.gmail.com").strip()
        port = int(os.getenv("REPORT_SMTP_PORT", "465") or "465")
        with smtplib.SMTP_SSL(host, port) as smtp:
            smtp.login(sender, password)
            smtp.send_message(msg)
        print(f"[jobs] liveness alert emailed to {len(to)} recipient(s)")
    else:
        print("[jobs] liveness alert NOT emailed (REPORT_EMAIL_* unset) - failing unit instead")
    sys.exit(1)  # red unit in systemd either way


_COMMANDS = {
    "morning": cmd_morning,
    "premarket": cmd_premarket,
    "eod": cmd_eod,
    "eod-filing": cmd_eod_filing,
    "session-report": cmd_session_report,
    "email-report": cmd_email_report,
    "liveness": cmd_liveness,
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
