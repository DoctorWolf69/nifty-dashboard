#!/usr/bin/env python3
"""Regenerate the EOD report (now with FII/DII) and email it to recipients.

Fired by the nifty-email-report timer (~20:15 IST, after the EOD downloads).
Reads SMTP config from .env; if recipients/password are unset it logs and no-ops
so the timer never fails. One email is sent to TO + CC together (everyone CC'd).
"""

from __future__ import annotations

import argparse
import os
import smtplib
import zipfile
from datetime import date, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import List

from dotenv import load_dotenv

from nifty.paths import ENV_FILE, JOURNAL_DIR
from nifty.eod.session_report import build_performance_summary, write_report_files


def _split_emails(raw: str) -> List[str]:
    """Comma/semicolon-separated address list -> clean, de-duped list."""
    out: List[str] = []
    for piece in str(raw or "").replace(";", ",").split(","):
        addr = piece.strip()
        if addr and addr not in out:
            out.append(addr)
    return out


def _report_files(label: str) -> List[Path]:
    """The small, shareable report artifacts for a day (never the tick SQLite)."""
    candidates = [
        JOURNAL_DIR / f"report_{label}.html",
        JOURNAL_DIR / f"eod_{label}_nifty.md",
        JOURNAL_DIR / f"signal_list_{label}.html",
        JOURNAL_DIR / f"nse_eod_filing_{label}.json",
    ]
    return [p for p in candidates if p.exists()]


def _build_zip(label: str) -> Path:
    zip_path = JOURNAL_DIR / f"report_{label}.zip"
    files = _report_files(label)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            zf.write(path, arcname=path.name)
    return zip_path


def _summary_text(label: str) -> str:
    s = build_performance_summary(date.fromisoformat(label))

    def fmt(v, default="—"):
        return default if v is None else v

    # Normal vs signed (shadow) grader agreement on closed trades.
    scored = [
        t for t in s.get("trades", [])
        if t.get("exit_time") and t.get("confluence_grade") and t.get("confluence_grade_signed")
    ]
    agree = sum(1 for t in scored if t.get("confluence_grade") == t.get("confluence_grade_signed"))
    grader_line = f"Grader agree:    {agree}/{len(scored)} (normal vs signed shadow)\n" if scored else ""

    public_url = os.getenv("REPORT_PUBLIC_URL", "").strip()
    link = f"\nLive report: {public_url.rstrip('/')}/report_latest.html\n" if public_url else ""
    return (
        f"NIFTY desk paper report — {label}\n"
        f"{'=' * 40}\n"
        f"Net P&L:        Rs {fmt(s['net_total'])}\n"
        f"Trades (closed): {s['total_trades']} ({s['closed_count']})\n"
        f"Wins / Losses:   {s['wins']} / {s['losses']}\n"
        f"Win rate:        {fmt(s['win_rate'])}%\n"
        f"Profit factor:   {fmt(s['profit_factor'])}\n"
        f"Max drawdown:    Rs {fmt(s['max_drawdown'])}\n"
        f"Avg hold (min):  {fmt(s['avg_hold_min'])}\n"
        f"{grader_line}"
        f"{link}"
        f"\nFull report + signal list attached (report_{label}.zip).\n"
    )


def send_report_email(trade_date: date) -> bool:
    """Returns True if an email was sent, False if not configured."""
    load_dotenv(ENV_FILE)
    label = trade_date.isoformat()

    sender = os.getenv("REPORT_EMAIL_FROM", "").strip()
    password = os.getenv("REPORT_EMAIL_APP_PASSWORD", "").strip()
    to_list = _split_emails(os.getenv("REPORT_EMAIL_TO", ""))
    cc_list = _split_emails(os.getenv("REPORT_EMAIL_CC", ""))
    host = os.getenv("REPORT_SMTP_HOST", "smtp.gmail.com").strip()
    port = int(os.getenv("REPORT_SMTP_PORT", "465") or "465")

    if not sender or not password or not to_list:
        print("[email-report] email not configured (REPORT_EMAIL_FROM / _APP_PASSWORD / _TO) — skipping send.")
        return False

    s = build_performance_summary(trade_date)
    subject = (
        f"NIFTY desk {label}: net Rs {s['net_total']} | "
        f"{s['wins']}W/{s['losses']}L | {s['total_trades']} trades"
    )

    zip_path = _build_zip(label)

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    msg["Subject"] = subject
    msg.set_content(_summary_text(label))

    with zip_path.open("rb") as fh:
        msg.add_attachment(
            fh.read(), maintype="application", subtype="zip", filename=zip_path.name
        )

    recipients = to_list + [a for a in cc_list if a not in to_list]
    with smtplib.SMTP_SSL(host, port) as smtp:
        smtp.login(sender, password)
        smtp.send_message(msg, from_addr=sender, to_addrs=recipients)

    print(f"[email-report] sent {label} report to {len(recipients)} recipient(s): {', '.join(recipients)}")
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Regenerate + email the EOD report")
    parser.add_argument("--date", default="", help="Trade date YYYY-MM-DD (default: today)")
    parser.add_argument("--skip-nse", action="store_true", help="Do not (re)build the NSE filing")
    parser.add_argument("--no-send", action="store_true", help="Regenerate files only, do not email")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    trade_date = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else date.today()
    # Re-render with whatever FII/DII filing exists by now.
    write_report_files(trade_date, skip_nse=args.skip_nse)
    if args.no_send:
        print("[email-report] --no-send: regenerated report files only.")
        return
    send_report_email(trade_date)


if __name__ == "__main__":
    main()
