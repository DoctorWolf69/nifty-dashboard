#!/usr/bin/env python3
"""
Poll GIFT Nifty after India cash close and persist ticks for overnight desk context.

GIFT trades ~16:35–02:45 IST (session 2) when NSE cash is closed — this script
keeps capturing that move for tomorrow's morning desk.

Examples:
  python gift_nifty_monitor.py --once
  python gift_nifty_monitor.py --poll-seconds 60
  python gift_nifty_monitor.py --poll-seconds 30 --print
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from nifty.sources.gift import build_gift_nifty_snapshot, gift_session_status

from nifty.paths import PROJECT_ROOT as BASE_DIR
DATA_DIR = BASE_DIR / "data" / "gift_nifty"
DB_PATH = DATA_DIR / "gift_nifty.sqlite"
JSONL_PATH = BASE_DIR / "journal" / "gift_nifty_ticks.jsonl"


def ensure_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gift_ticks (
                ts TEXT NOT NULL,
                gift_last REAL,
                gift_expiry TEXT,
                nse_close REAL,
                premium REAL,
                premium_pct REAL,
                overnight_bias TEXT,
                gift_session_status TEXT,
                contracts_traded INTEGER,
                raw_json TEXT
            )
            """
        )
        conn.commit()


def persist_snapshot(db_path: Path, jsonl_path: Path, snapshot: Dict[str, Any]) -> None:
    front = snapshot.get("front_month") or {}
    cash = snapshot.get("nse_cash") or {}
    session = snapshot.get("session") or {}
    row = {
        "ts": snapshot.get("generated_at"),
        "gift_last": front.get("last_price"),
        "gift_expiry": front.get("expiry"),
        "nse_close": cash.get("last") or cash.get("previous_close"),
        "premium": snapshot.get("premium_vs_nse_close"),
        "premium_pct": snapshot.get("premium_pct"),
        "overnight_bias": snapshot.get("overnight_bias"),
        "gift_session_status": session.get("status"),
        "contracts_traded": front.get("contracts_traded"),
    }
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO gift_ticks
            (ts, gift_last, gift_expiry, nse_close, premium, premium_pct,
             overnight_bias, gift_session_status, contracts_traded, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["ts"],
                row["gift_last"],
                row["gift_expiry"],
                row["nse_close"],
                row["premium"],
                row["premium_pct"],
                row["overnight_bias"],
                row["gift_session_status"],
                row["contracts_traded"],
                json.dumps(snapshot, separators=(",", ":")),
            ),
        )
        conn.commit()
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with jsonl_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, separators=(",", ":")) + "\n")


def print_snapshot(snapshot: Dict[str, Any]) -> None:
    front = snapshot.get("front_month") or {}
    session = snapshot.get("session") or {}
    print(
        f"[{snapshot.get('generated_at')}] "
        f"GIFT {front.get('last_price')} ({front.get('expiry')}) | "
        f"NSE close {snapshot.get('nse_cash', {}).get('last')} | "
        f"Premium {snapshot.get('premium_vs_nse_close')} ({snapshot.get('premium_pct')}%) | "
        f"Bias {snapshot.get('overnight_bias')} | "
        f"GIFT session {session.get('status')} ({session.get('active_session_label')})"
    )
    if snapshot.get("errors"):
        print("  errors:", "; ".join(snapshot["errors"]))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor GIFT Nifty after India close")
    parser.add_argument("--poll-seconds", type=int, default=0, help="Poll interval; 0 = run once")
    parser.add_argument("--once", action="store_true", help="Fetch one snapshot and exit")
    parser.add_argument("--db", default=str(DB_PATH), help="SQLite path")
    parser.add_argument("--jsonl", default=str(JSONL_PATH), help="JSONL journal path")
    parser.add_argument("--print", dest="do_print", action="store_true", help="Print each snapshot")
    parser.add_argument(
        "--only-when-open",
        action="store_true",
        help="Skip polling when GIFT session is CLOSED",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    db_path = Path(args.db)
    jsonl_path = Path(args.jsonl)
    ensure_db(db_path)

    def run_once() -> Dict[str, Any]:
        if args.only_when_open and gift_session_status().get("status") != "OPEN":
            snapshot = {"generated_at": datetime.now().isoformat(), "skipped": "gift_session_closed"}
            if args.do_print:
                print("GIFT session closed — skipping fetch")
            return snapshot
        snapshot = build_gift_nifty_snapshot()
        persist_snapshot(db_path, jsonl_path, snapshot)
        if args.do_print:
            print_snapshot(snapshot)
        return snapshot

    if args.once or args.poll_seconds <= 0:
        run_once()
        return

    print(f"GIFT Nifty monitor started — polling every {args.poll_seconds}s")
    print(f"SQLite: {db_path}")
    print(f"JSONL:  {jsonl_path}")
    while True:
        try:
            run_once()
        except Exception as exc:
            print(f"monitor error: {exc}")
        time.sleep(max(15, args.poll_seconds))


if __name__ == "__main__":
    main()
