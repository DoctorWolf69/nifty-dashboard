#!/usr/bin/env python3
"""9:01 IST pre-market scan — refresh GIFT, Kite gap, bias, key levels, desk brief."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime

from nifty.morning.phases import run_premarket_scan


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pre-market scan at 9:01 IST (NSE pre-open window)")
    parser.add_argument("--date", default="", help="Trade date YYYY-MM-DD label")
    parser.add_argument("--print", action="store_true", help="Print summary JSON to stdout")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    trade_date = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else date.today()
    result = run_premarket_scan(trade_date)
    path = result["summary"]["files"]["premarket_scan"]
    print(f"Pre-market scan written: {path}")
    print(f"  Bias: {result['summary'].get('combined_bias')} | Gap: {result['summary'].get('cash_open_gap')}")
    if args.print:
        print(json.dumps(result["summary"], indent=2, default=str))


if __name__ == "__main__":
    main()
