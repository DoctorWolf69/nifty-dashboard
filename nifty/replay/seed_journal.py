"""Seed the real desk journal with an archived day's replayed paper trades.

The standard EOD report (`python -m nifty.eod.session_report --date <day>`) reads
`journal/nifty_paper_trades_<day>.jsonl`. On a host that did not run the live
engine that day (or after an engine code change), that file is missing/stale.

This helper re-runs the engine over the archived ticks for a day and writes the
resulting paper-trade lifecycle events into the real journal, so the *normal*
EOD report can then be produced for that day under the current code. Useful for
regenerating a day's report after a logic change (e.g. comparing two code
states on the same archived ticks).

Usage:
    python -m nifty.replay.seed_journal 2026-06-19 [--rebuild]
"""

from __future__ import annotations

import argparse
import json

from nifty.eod.session_report import JOURNAL_DIR
from nifty.replay.session import ReplayTimeline


def seed_day(day: str, rebuild: bool = False) -> str:
    """Re-run the engine over `day`'s archived ticks; write trades to the journal."""
    timeline = ReplayTimeline(day, rebuild=rebuild)
    signals = timeline.signals
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    path = JOURNAL_DIR / f"nifty_paper_trades_{day}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for sig in signals:
            fh.write(json.dumps({"event": "SIGNAL_GENERATED", **sig}, default=str) + "\n")
            if sig.get("exit_time"):
                fh.write(json.dumps({"event": "SIGNAL_CLOSED", **sig}, default=str) + "\n")
    print(f"[seed] wrote {len(signals)} replayed signals -> {path}")
    return str(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the journal from an archived day's replay")
    parser.add_argument("day", help="Trade date YYYY-MM-DD (must have an archived tick DB)")
    parser.add_argument("--rebuild", action="store_true", help="Force rebuild the timeline cache")
    args = parser.parse_args()
    seed_day(args.day, rebuild=args.rebuild)


if __name__ == "__main__":
    main()
