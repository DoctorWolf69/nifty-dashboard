#!/usr/bin/env python3
"""EOD model governance — calibrate weights, counterfactuals, governance metrics.

Ported faithfully from quant-desk-engine's desk_model_governance.py
(mentor-authored). No logic changed — a thin CLI wrapper around
nifty.analytics.model_learning.run_daily_model_governance.

Kept as a standalone CLI module, matching this repo's eod/*.py convention
(each has its own main()/parse_args(), dispatched via nifty.jobs when a job
is actually wired up). NOT added to nifty/jobs.py yet — that is a scheduling
decision separate from porting the code, and this whole EOD chain still
needs feature_drift.py + research_db.py + model_learning.py all populated
from real signal_candidates journal data to produce anything meaningful.

Not yet wired into the live pipeline.
Manual run: python -m nifty.analytics.model_governance --date 2026-06-19
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime

from nifty.analytics.model_learning import run_daily_model_governance
from nifty.analytics.probability_engine import refresh_evidence_weights


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run desk model governance (calibration + counterfactuals)")
    parser.add_argument("--date", help="Trade date YYYY-MM-DD (default: today)")
    parser.add_argument("--lookback", type=int, default=30, help="Training lookback days")
    parser.add_argument("--no-save-weights", action="store_true", help="Skip writing config/evidence_weights.json")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    trade_date = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else date.today()
    payload = run_daily_model_governance(
        trade_date,
        lookback_days=args.lookback,
        save_weights=not args.no_save_weights,
    )
    refresh_evidence_weights()
    print(json.dumps(
        {
            "status": payload.get("status") or "OK",
            "date": payload.get("date"),
            "labeled_count": payload.get("labeled_count"),
            "governance_path": payload.get("governance_path"),
            "counterfactual_path": payload.get("counterfactual_path"),
            "weights_saved": payload.get("weights_saved"),
            "shadow": (payload.get("governance") or {}).get("shadow_comparison"),
            "brier": (payload.get("governance") or {}).get("brier_score"),
            "top_features": [f["feature"] for f in (payload.get("feature_importance") or [])[:5]],
        },
        indent=2,
    ))


def _selftest() -> None:
    import tempfile
    from pathlib import Path

    # No journal data -> run_daily_model_governance's own NO_JOURNAL_DATA guard,
    # proving the wrapper's plumbing (imports, payload shape) without needing
    # a populated journal.
    empty_dir = Path(tempfile.mkdtemp(prefix="model-governance-selftest-"))
    payload = run_daily_model_governance(date(2026, 6, 19), journal_dir=empty_dir)
    assert payload["status"] == "NO_JOURNAL_DATA"
    assert payload["date"] == "2026-06-19"

    print("[analytics.model_governance] selftest OK: CLI wrapper imports clean, no-data guard reached")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        main()
    else:
        _selftest()
