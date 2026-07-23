#!/usr/bin/env python3
"""
Legacy scoreboard v2 trainer (shadow only).

Learns dimension weights, blocker policy, and score thresholds from journals.
Does not modify nifty.analytics.confluence.py (legacy v1 live path).

Ported faithfully from quant-desk-engine v4/ATLAS's legacy_score_trainer.py
(mentor-authored). No logic changed. Adaptations:
- `from ev_shadow_trainer import dedupe_thesis_rows,
  load_labeled_from_counterfactual_journal` -> nifty.analytics.ev_shadow_trainer
  (this session's sibling port).
- `from nifty_model_learning import _candidate_id, _parse_day,
  list_available_journal_days, load_signal_candidates` ->
  nifty.analytics.model_learning for _candidate_id/_parse_day/
  load_signal_candidates; list_available_journal_days actually lives in
  nifty.analytics.journal_reader (model_learning.py imports it from there
  too - same re-export pattern used throughout this session's ports).
- `from nifty_signal_confluence import CONFLUENCE_WEIGHTS,
  TRADE_MIN_CONFLUENCE` -> nifty.analytics.confluence (already ported/
  updated this session, identical names).
- BASE_DIR/CONFIG_DIR/JOURNAL_DIR/LEGACY_SCORE_SHADOW_PATH resolve via
  nifty.paths (LEGACY_SCORE_SHADOW_PATH matches
  nifty.analytics.confluence_v2.CONFIG_PATH exactly - this trainer's output
  is confluence_v2.py's input).
- desk_legacy_score_trainer.py's CLI (`main()`, argparse) is folded in at
  the bottom rather than kept as a separate wrapper file, matching the
  ev_shadow_trainer.py sibling port and the cumulative/consolidated PnL
  report modules' own CLI-in-module pattern.

Writes config/legacy_score_shadow.json - exactly the file
nifty.analytics.confluence_v2.rescore_legacy_v2() reads (currently
SHADOW_NOT_TRAINED since that file doesn't exist yet). Running this
trainer is what would move confluence_v2 from untrained to shadow-ready;
it still never touches confluence.py's live paper_eligible/scoring path.

Not yet wired into the live pipeline (CLI-only, like the source).
Self-check: python -m nifty.analytics.legacy_score_trainer
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from nifty.analytics.confluence import CONFLUENCE_WEIGHTS, TRADE_MIN_CONFLUENCE
from nifty.analytics.ev_shadow_trainer import dedupe_thesis_rows, load_labeled_from_counterfactual_journal
from nifty.analytics.journal_reader import list_available_journal_days
from nifty.analytics.model_learning import _candidate_id, _parse_day, load_signal_candidates
from nifty.paths import JOURNAL_DIR, PROJECT_ROOT

CONFIG_DIR = PROJECT_ROOT / "config"
LEGACY_SCORE_SHADOW_PATH = CONFIG_DIR / "legacy_score_shadow.json"

DIMENSION_KEYS = tuple(CONFLUENCE_WEIGHTS.keys())
PAPER_BLOCKERS_V1 = {
    "ORB_NO_TRADE",
    "LATE_SESSION",
    "MAX_OPEN",
    "THESIS_STACK",
    "STRIKE_SPACING",
    "DIRECTION_CONFLICT",
    "COOLDOWN",
    "NO_ENTRY_CONTRACT",
    "BOTH_SIDES_ADDING",
    "STRIKE_INTENT_CONFLICT",
    "CHAIN_DIRECTION_CONFLICT",
    "SPOT_NOT_WEAK_FOR_PE",
    "PE_SPOT_NOT_CONFIRMED",
    "SPOT_NOT_WEAK",
    "GEX_DELTA_CONFLICT",
    "GEX_VOL_EXPANSION",
    "MARKET_PROFILE_CONFLICT",
    "VOL_REGIME_CONFLICT",
    "LIQUIDITY_GRAB_CONFLICT",
    "WATCH_ZONE",
}
MIN_WEIGHT = 3
MAX_WEIGHT = 22


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _win_rate(rows: List[Dict[str, Any]]) -> float:
    if not rows:
        return 0.0
    return sum(1 for r in rows if (r.get("outcome") or {}).get("win")) / len(rows)


def _candidate_meta_index(journal_dir: Path, days: List[str]) -> Dict[str, Dict[str, Any]]:
    index: Dict[str, Dict[str, Any]] = {}
    for candidate in load_signal_candidates(journal_dir, days=days):
        cid = _candidate_id(candidate)
        if not cid:
            continue
        index[cid] = {
            "dimensions": candidate.get("dimensions") or {},
            "blockers": list(candidate.get("blockers") or []),
            "playbook_phase": str(candidate.get("playbook_phase") or "UNKNOWN"),
            "market_state": str(((candidate.get("ev_model") or {}).get("market_state")) or "UNKNOWN"),
            "total_score_v1": _as_float(candidate.get("total_score")),
            "grade_v1": str(candidate.get("grade") or ""),
        }
    return index


def load_scoring_training_rows(journal_dir: Path, days: List[str]) -> List[Dict[str, Any]]:
    labeled = dedupe_thesis_rows(load_labeled_from_counterfactual_journal(journal_dir, days=days))
    meta_index = _candidate_meta_index(journal_dir, days)
    rows: List[Dict[str, Any]] = []
    for row in labeled:
        meta = meta_index.get(str(row.get("candidate_id") or ""), {})
        dimensions = meta.get("dimensions") or {}
        if not dimensions:
            continue
        rows.append(
            {
                **row,
                "dimensions": dimensions,
                "blockers": meta.get("blockers") or [],
                "playbook_phase": meta.get("playbook_phase") or "UNKNOWN",
                "market_state": meta.get("market_state") or "UNKNOWN",
                "total_score_v1": meta.get("total_score_v1", 0.0),
                "grade_v1": meta.get("grade_v1", ""),
            }
        )
    return rows


def _dimension_lift(rows: List[Dict[str, Any]], dimension: str) -> Dict[str, Any]:
    pass_rows = [r for r in rows if bool((r.get("dimensions") or {}).get(dimension, {}).get("pass"))]
    fail_rows = [r for r in rows if not bool((r.get("dimensions") or {}).get(dimension, {}).get("pass"))]
    pass_wr = _win_rate(pass_rows)
    fail_wr = _win_rate(fail_rows)
    return {
        "dimension": dimension,
        "n_pass": len(pass_rows),
        "n_fail": len(fail_rows),
        "win_rate_pass": round(pass_wr * 100, 1),
        "win_rate_fail": round(fail_wr * 100, 1),
        "lift_pp": round((pass_wr - fail_wr) * 100, 1),
    }


def learn_weights_v2(rows: List[Dict[str, Any]]) -> Tuple[Dict[str, int], List[Dict[str, Any]]]:
    lifts = [_dimension_lift(rows, dim) for dim in DIMENSION_KEYS]
    avg_abs_lift = statistics.mean(abs(l["lift_pp"]) for l in lifts) or 1.0
    weights_v2: Dict[str, int] = {}
    changes: List[Dict[str, Any]] = []

    for item in lifts:
        dim = item["dimension"]
        v1 = int(CONFLUENCE_WEIGHTS[dim])
        lift = _as_float(item["lift_pp"])
        multiplier = 1.0 + max(-0.35, min(0.35, (lift / max(avg_abs_lift, 1.0)) * 0.25))
        v2 = int(round(max(MIN_WEIGHT, min(MAX_WEIGHT, v1 * multiplier))))
        weights_v2[dim] = v2
        changes.append(
            {
                "dimension": dim,
                "weight_v1": v1,
                "weight_v2": v2,
                "delta": v2 - v1,
                "lift_pp": item["lift_pp"],
                "win_rate_pass": item["win_rate_pass"],
                "win_rate_fail": item["win_rate_fail"],
                "n_pass": item["n_pass"],
                "n_fail": item["n_fail"],
            }
        )
    changes.sort(key=lambda row: abs(row["delta"]), reverse=True)
    return weights_v2, changes


def learn_blocker_policy_v2(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    global_wr = _win_rate(rows)
    blocker_rows: List[Dict[str, Any]] = []
    for blocker in sorted(PAPER_BLOCKERS_V1):
        with_blocker = [r for r in rows if blocker in (r.get("blockers") or [])]
        without_blocker = [r for r in rows if blocker not in (r.get("blockers") or [])]
        if len(with_blocker) < 3:
            continue
        with_wr = _win_rate(with_blocker)
        without_wr = _win_rate(without_blocker)
        delta_pp = (with_wr - without_wr) * 100
        if with_wr <= global_wr - 0.05:
            action = "keep_hard"
            reason = "Protective — lower win rate when blocker fires"
        elif with_wr >= global_wr + 0.08:
            action = "watch_only"
            reason = "Over-strict — winners often blocked"
        else:
            action = "keep_hard"
            reason = "Neutral impact"
        blocker_rows.append(
            {
                "blocker": blocker,
                "n_with": len(with_blocker),
                "n_without": len(without_blocker),
                "win_rate_with": round(with_wr * 100, 1),
                "win_rate_without": round(without_wr * 100, 1),
                "delta_pp": round(delta_pp, 1),
                "action_v2": action,
                "reason": reason,
            }
        )

    watch_only = [row["blocker"] for row in blocker_rows if row["action_v2"] == "watch_only"]
    hard_blockers = sorted(set(PAPER_BLOCKERS_V1) - set(watch_only))
    return {
        "hard_blockers_v2": hard_blockers,
        "watch_only_blockers_v2": sorted(watch_only),
        "blocker_audit": sorted(blocker_rows, key=lambda row: abs(row["delta_pp"]), reverse=True),
    }


def learn_thresholds_v2(rows: List[Dict[str, Any]], *, default_min: int = TRADE_MIN_CONFLUENCE) -> Dict[str, Any]:
    thresholds: Dict[str, int] = {"GLOBAL": default_min}
    by_regime: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        regime = str(row.get("playbook_phase") or row.get("market_state") or "UNKNOWN")
        by_regime[regime].append(row)

    sweep_details: List[Dict[str, Any]] = []
    for regime, subset in by_regime.items():
        if len(subset) < 12:
            continue
        best_threshold = default_min
        best_gap = -1.0
        for threshold in range(55, 76):
            above = [r for r in subset if _as_float(r.get("total_score_v1")) >= threshold]
            below = [r for r in subset if _as_float(r.get("total_score_v1")) < threshold]
            if len(above) < 3 or len(below) < 3:
                continue
            gap = _win_rate(above) - _win_rate(below)
            if gap > best_gap:
                best_gap = gap
                best_threshold = threshold
        thresholds[regime] = best_threshold
        sweep_details.append(
            {
                "regime": regime,
                "sample_size": len(subset),
                "threshold_v1": default_min,
                "threshold_v2": best_threshold,
                "win_rate_gap_pp": round(best_gap * 100, 1),
            }
        )
    return {"thresholds_v2": thresholds, "threshold_audit": sweep_details}


def score_rank_quality(rows: List[Dict[str, Any]], *, score_key: str = "total_score_v1") -> Dict[str, Any]:
    if not rows:
        return {"top_decile_win_rate": None, "bottom_decile_win_rate": None, "separation_pp": None}
    sorted_rows = sorted(rows, key=lambda r: _as_float(r.get(score_key)))
    n = max(1, len(sorted_rows) // 10)
    bottom = sorted_rows[:n]
    top = sorted_rows[-n:]
    top_wr = _win_rate(top)
    bottom_wr = _win_rate(bottom)
    return {
        "top_decile_win_rate": round(top_wr * 100, 1),
        "bottom_decile_win_rate": round(bottom_wr * 100, 1),
        "separation_pp": round((top_wr - bottom_wr) * 100, 1),
    }


def train_legacy_score_v2(
    trade_date: Optional[date] = None,
    *,
    journal_dir: Path = JOURNAL_DIR,
    lookback_days: int = 30,
    save: bool = True,
) -> Dict[str, Any]:
    d = trade_date or date.today()
    available = list_available_journal_days(journal_dir)
    if not available:
        return {"status": "NO_JOURNAL_DATA", "date": d.isoformat()}

    cutoff = d - timedelta(days=lookback_days)
    days = [day for day in available if _parse_day(day) >= cutoff]
    if d.isoformat() not in days:
        days.insert(0, d.isoformat())

    rows = load_scoring_training_rows(journal_dir, days)
    if not rows:
        return {"status": "NO_SCORING_ROWS", "date": d.isoformat(), "sessions_included": days}

    weights_v2, weight_changes = learn_weights_v2(rows)
    blocker_policy = learn_blocker_policy_v2(rows)
    threshold_policy = learn_thresholds_v2(rows)

    rank_v1 = score_rank_quality(rows, score_key="total_score_v1")

    payload = {
        "version": "legacy_score_shadow_v1",
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "trainer": "legacy_score_trainer_v1",
        "date": d.isoformat(),
        "lookback_days": lookback_days,
        "sessions_included": days,
        "journal_sources": [
            "nifty_signal_candidates_*.jsonl",
            "counterfactual_outcomes_*.jsonl",
            "nifty_paper_trades_*.jsonl",
        ],
        "sample_size_thesis": len(rows),
        "weights_v1": dict(CONFLUENCE_WEIGHTS),
        "weights_v2": weights_v2,
        "min_score_v1": TRADE_MIN_CONFLUENCE,
        "min_score_v2": threshold_policy["thresholds_v2"].get("GLOBAL", TRADE_MIN_CONFLUENCE),
        "thresholds_v2": threshold_policy["thresholds_v2"],
        "hard_blockers_v1": sorted(PAPER_BLOCKERS_V1),
        "hard_blockers_v2": blocker_policy["hard_blockers_v2"],
        "watch_only_blockers_v2": blocker_policy["watch_only_blockers_v2"],
        "weight_changes": weight_changes,
        "blocker_audit": blocker_policy["blocker_audit"],
        "threshold_audit": threshold_policy["threshold_audit"],
        "rank_quality_v1": rank_v1,
        "shadow_only": True,
        "live_gate_impact": "none",
        "changelog": _build_changelog(weight_changes, blocker_policy, threshold_policy),
    }

    report_path = journal_dir / f"legacy_score_training_{d.isoformat()}.json"
    if save:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        LEGACY_SCORE_SHADOW_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    payload["config_path"] = str(LEGACY_SCORE_SHADOW_PATH)
    payload["training_report_path"] = str(report_path)
    payload["status"] = "OK"
    return payload


def _build_changelog(
    weight_changes: List[Dict[str, Any]],
    blocker_policy: Dict[str, Any],
    threshold_policy: Dict[str, Any],
) -> Dict[str, Any]:
    increased = [c for c in weight_changes if c["delta"] > 0][:5]
    decreased = [c for c in weight_changes if c["delta"] < 0][:5]
    moved_to_watch = blocker_policy.get("watch_only_blockers_v2") or []
    threshold_shifts = [
        row for row in (threshold_policy.get("threshold_audit") or [])
        if row.get("threshold_v2") != row.get("threshold_v1")
    ]
    return {
        "weights_increased": increased,
        "weights_decreased": decreased,
        "blockers_moved_to_watch_only": moved_to_watch,
        "threshold_shifts": threshold_shifts,
    }


def main() -> None:
    """CLI entry point, folded in from v4's desk_legacy_score_trainer.py wrapper."""
    import argparse

    from nifty.analytics.confluence_v2 import reload_legacy_score_shadow

    parser = argparse.ArgumentParser(description="Train legacy scoreboard v2 from journals (shadow only)")
    parser.add_argument("--date", help="As-of trade date YYYY-MM-DD (default: today)")
    parser.add_argument("--lookback", type=int, default=30, help="Training lookback days")
    args = parser.parse_args()

    trade_date = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else date.today()
    payload = train_legacy_score_v2(trade_date, lookback_days=args.lookback)
    reload_legacy_score_shadow()
    print(
        json.dumps(
            {
                "status": payload.get("status"),
                "date": payload.get("date"),
                "sample_size_thesis": payload.get("sample_size_thesis"),
                "config_path": payload.get("config_path"),
                "training_report_path": payload.get("training_report_path"),
                "changelog": payload.get("changelog"),
                "weight_changes": payload.get("weight_changes"),
                "watch_only_blockers_v2": payload.get("watch_only_blockers_v2"),
                "thresholds_v2": payload.get("thresholds_v2"),
            },
            indent=2,
        )
    )


def _selftest() -> None:
    import tempfile

    # _win_rate / _dimension_lift on synthetic labeled+dimension rows.
    rows = [
        {"outcome": {"win": True}, "dimensions": {"atm_proximity": {"pass": True}}, "blockers": [], "playbook_phase": "GLOBAL", "total_score_v1": 70.0},
        {"outcome": {"win": True}, "dimensions": {"atm_proximity": {"pass": True}}, "blockers": [], "playbook_phase": "GLOBAL", "total_score_v1": 68.0},
        {"outcome": {"win": False}, "dimensions": {"atm_proximity": {"pass": False}}, "blockers": ["COOLDOWN"], "playbook_phase": "GLOBAL", "total_score_v1": 40.0},
        {"outcome": {"win": False}, "dimensions": {"atm_proximity": {"pass": False}}, "blockers": ["COOLDOWN"], "playbook_phase": "GLOBAL", "total_score_v1": 42.0},
    ]
    assert _win_rate(rows) == 0.5
    assert _win_rate([]) == 0.0

    lift = _dimension_lift(rows, "atm_proximity")
    assert lift["win_rate_pass"] == 100.0
    assert lift["win_rate_fail"] == 0.0
    assert lift["lift_pp"] == 100.0

    weights_v2, changes = learn_weights_v2(rows)
    assert set(weights_v2.keys()) == set(CONFLUENCE_WEIGHTS.keys())
    assert all(MIN_WEIGHT <= v <= MAX_WEIGHT for v in weights_v2.values())
    assert changes and changes[0]["dimension"] in CONFLUENCE_WEIGHTS

    # 3+ occurrences needed for a blocker to be scored.
    many_rows = rows * 3  # 12 rows total, 6 with COOLDOWN, 6 without
    blocker_policy = learn_blocker_policy_v2(many_rows)
    audited = {b["blocker"] for b in blocker_policy["blocker_audit"]}
    assert "COOLDOWN" in audited
    assert "COOLDOWN" in blocker_policy["hard_blockers_v2"] or "COOLDOWN" in blocker_policy["watch_only_blockers_v2"]

    thresholds = learn_thresholds_v2(rows)
    assert thresholds["thresholds_v2"]["GLOBAL"] == TRADE_MIN_CONFLUENCE  # default, too few rows for a sweep

    rank = score_rank_quality(rows)
    assert rank["top_decile_win_rate"] is not None
    assert score_rank_quality([]) == {"top_decile_win_rate": None, "bottom_decile_win_rate": None, "separation_pp": None}

    # train_legacy_score_v2: no journal data -> graceful status, never raises.
    tmp = Path(tempfile.mkdtemp(prefix="legacy-score-trainer-selftest-"))
    result = train_legacy_score_v2(date(2026, 7, 21), journal_dir=tmp)
    assert result["status"] == "NO_JOURNAL_DATA"

    print("[analytics.legacy_score_trainer] selftest OK: win rate, dimension lift, weight/blocker/threshold learning, no-data guard")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        main()
    else:
        _selftest()
