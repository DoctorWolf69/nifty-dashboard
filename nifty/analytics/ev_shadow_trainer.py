#!/usr/bin/env python3
"""
EV shadow calibration trainer — batch learning from journal history.

Shadow-only: writes config/ev_calibration_shadow.json and a training report.
Does not change live paper gates (USE_EV_MODEL_FOR_PAPER stays False).

Ported faithfully from quant-desk-engine v4/ATLAS's ev_shadow_trainer.py
(mentor-authored). No logic changed. Adaptations:
- `from nifty_model_learning import (...)` -> nifty.analytics.model_learning.
  Every dependency (_brier_score, _calibration_buckets, _calibration_error,
  _candidate_id, _parse_day, build_labeled_dataset, calibrate_all_regimes,
  extract_feature_flags, list_available_journal_days, load_signal_candidates,
  save_learned_weights) already exists there with identical names/signatures
  - confirming nifty.analytics.model_learning IS the same nifty_model_learning.py
  lineage this trainer was always built against (not a different/parallel
  counterfactual system, as initially suspected before checking this file's
  own imports).
- BASE_DIR/CONFIG_DIR/JOURNAL_DIR/CALIBRATION_PATH resolve via nifty.paths
  (CONFIG_DIR = PROJECT_ROOT / "config", matching model_learning.py's own
  WEIGHTS_PATH convention; CALIBRATION_PATH matches the same constant added
  to nifty.analytics.probability_engine this session).
- desk_ev_shadow_trainer.py's CLI (`main()`, argparse) is folded in at the
  bottom rather than kept as a separate 56-line wrapper file, matching how
  cumulative_pnl_report.py/consolidated_pnl_report.py already keep their CLI
  entry points in the same module (bare `python -m ...` runs _selftest();
  passing args runs main()).

Note: refresh_evidence_weights() (called from main(), in
nifty.analytics.probability_engine) reads WEIGHTS_PATH = DATA_DIR /
"evidence_weights.json", but save_learned_weights() (in model_learning.py,
called from this trainer) writes to CONFIG_DIR / "evidence_weights.json" -
a pre-existing path mismatch between those two already-ported modules,
not introduced here. Flagging rather than silently changing either
existing constant.

Not yet wired into the live pipeline (CLI-only, like the source).
Self-check: python -m nifty.analytics.ev_shadow_trainer
"""

from __future__ import annotations

import json
import math
import statistics
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from nifty.analytics.model_learning import (
    _brier_score,
    _calibration_buckets,
    _calibration_error,
    _candidate_id,
    _parse_day,
    build_labeled_dataset,
    calibrate_all_regimes,
    extract_feature_flags,
    list_available_journal_days,
    load_signal_candidates,
    save_learned_weights,
)
from nifty.paths import JOURNAL_DIR, PROJECT_ROOT

CONFIG_DIR = PROJECT_ROOT / "config"
CALIBRATION_PATH = CONFIG_DIR / "ev_calibration_shadow.json"
TRAINING_REPORT_DIR = JOURNAL_DIR

ACTUAL_WEIGHT = 1.0
COUNTERFACTUAL_WEIGHT = 0.35
MIN_BIN_SAMPLES = 25
DEFAULT_BUCKETS = 10


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _prediction_bias_pp(rows: List[Dict[str, Any]], *, prob_key: str = "predicted_probability") -> float:
    if not rows:
        return 0.0
    pred = statistics.mean(_as_float(r.get(prob_key)) for r in rows) * 100
    actual = sum(1 for r in rows if r["outcome"]["win"]) / len(rows) * 100
    return round(pred - actual, 2)


def _row_weight(row: Dict[str, Any]) -> float:
    source = str((row.get("outcome") or {}).get("source") or "")
    return ACTUAL_WEIGHT if source == "actual" else COUNTERFACTUAL_WEIGHT


def dedupe_thesis_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    One training row per (session_day, signal_key).
    Prefer first EV-eligible tick; else highest legacy score; else earliest evaluated_at.
    """
    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for row in rows:
        key = (str(row.get("session_day") or ""), str(row.get("signal_key") or ""))
        grouped.setdefault(key, []).append(row)

    out: List[Dict[str, Any]] = []
    for bucket in grouped.values():
        ev_rows = [r for r in bucket if r.get("ev_trade_eligible")]
        if ev_rows:
            pick = min(ev_rows, key=lambda r: str(r.get("evaluated_at") or ""))
        else:
            pick = max(
                bucket,
                key=lambda r: (
                    _as_float(r.get("legacy_score")),
                    str(r.get("evaluated_at") or ""),
                ),
            )
        out.append(pick)
    out.sort(key=lambda r: (str(r.get("session_day") or ""), str(r.get("evaluated_at") or "")))
    return out


def _weighted_mean(values: List[Tuple[float, float]]) -> float:
    if not values:
        return 0.0
    num = sum(v * w for v, w in values)
    den = sum(w for _, w in values)
    return num / den if den else 0.0


def fit_isotonic_calibration(
    rows: List[Dict[str, Any]],
    *,
    buckets: int = DEFAULT_BUCKETS,
    min_bin_samples: int = MIN_BIN_SAMPLES,
) -> Dict[str, Any]:
    """Bin-based isotonic mapping: raw predicted probability -> calibrated probability."""
    if not rows:
        return {"status": "NO_DATA", "anchors": [], "buckets": buckets}

    sorted_rows = sorted(rows, key=lambda r: _as_float(r.get("predicted_probability")))
    target_size = max(min_bin_samples, math.ceil(len(sorted_rows) / buckets))
    raw_bins: List[Dict[str, Any]] = []

    for i in range(0, len(sorted_rows), target_size):
        chunk = sorted_rows[i : i + target_size]
        if not chunk:
            continue
        pairs = [
            (_as_float(r.get("predicted_probability")), _row_weight(r))
            for r in chunk
        ]
        outcomes = [
            (1.0 if r["outcome"]["win"] else 0.0, _row_weight(r))
            for r in chunk
        ]
        raw_bins.append(
            {
                "raw_mean": _weighted_mean(pairs),
                "actual_mean": _weighted_mean(outcomes),
                "n": len(chunk),
                "n_actual": sum(1 for r in chunk if (r.get("outcome") or {}).get("source") == "actual"),
            }
        )

    if not raw_bins:
        return {"status": "NO_BINS", "anchors": [], "buckets": buckets}

    # Pool-adjacent-violators style monotonic pass on actual means.
    calibrated = [b["actual_mean"] for b in raw_bins]
    i = 0
    while i < len(calibrated) - 1:
        if calibrated[i] > calibrated[i + 1]:
            pooled = (calibrated[i] + calibrated[i + 1]) / 2.0
            calibrated[i] = pooled
            calibrated[i + 1] = pooled
            if i > 0:
                i -= 1
                continue
        i += 1

    anchors = []
    for idx, bucket in enumerate(raw_bins):
        anchors.append(
            {
                "raw_probability": round(bucket["raw_mean"], 4),
                "calibrated_probability": round(max(0.01, min(0.99, calibrated[idx])), 4),
                "n": bucket["n"],
                "n_actual": bucket["n_actual"],
                "raw_mean_pct": round(bucket["raw_mean"] * 100, 1),
                "actual_mean_pct": round(bucket["actual_mean"] * 100, 1),
                "calibrated_mean_pct": round(calibrated[idx] * 100, 1),
            }
        )

    return {
        "status": "OK",
        "buckets": len(anchors),
        "anchors": anchors,
        "sample_size": len(rows),
    }


def apply_isotonic_calibration(raw_probability: float, anchors: List[Dict[str, Any]]) -> float:
    """Piecewise-linear map from raw probability to calibrated probability."""
    p = max(0.01, min(0.99, _as_float(raw_probability)))
    if not anchors:
        return p
    if p <= _as_float(anchors[0].get("raw_probability"), p):
        return _as_float(anchors[0].get("calibrated_probability"), p)
    if p >= _as_float(anchors[-1].get("raw_probability"), p):
        return _as_float(anchors[-1].get("calibrated_probability"), p)

    for left, right in zip(anchors, anchors[1:]):
        x0 = _as_float(left.get("raw_probability"))
        x1 = _as_float(right.get("raw_probability"))
        if x0 <= p <= x1:
            y0 = _as_float(left.get("calibrated_probability"))
            y1 = _as_float(right.get("calibrated_probability"))
            if x1 <= x0:
                return y1
            t = (p - x0) / (x1 - x0)
            return max(0.01, min(0.99, y0 + t * (y1 - y0)))
    return p


def evaluate_probability_rows(
    rows: List[Dict[str, Any]],
    *,
    prob_key: str = "predicted_probability",
) -> Dict[str, Any]:
    if not rows:
        return {
            "sample_size": 0,
            "brier_score": None,
            "calibration_error_ece": None,
            "prediction_bias_pp": None,
            "calibration": [],
        }
    cal_buckets = _calibration_buckets(
        [{**r, "predicted_probability": _as_float(r.get(prob_key))} for r in rows]
    )
    cal_err = _calibration_error(cal_buckets)
    return {
        "sample_size": len(rows),
        "brier_score": round(_brier_score([{**r, "predicted_probability": _as_float(r.get(prob_key))} for r in rows]), 4),
        "calibration_error_ece": cal_err["ece"],
        "prediction_bias_pp": _prediction_bias_pp(rows, prob_key=prob_key),
        "calibration": cal_buckets,
    }


def _normalize_probability(value: Any) -> float:
    p = _as_float(value, 0.5)
    return p / 100.0 if p > 1.0 else p


def _candidate_feature_index(journal_dir: Path, days: List[str]) -> Dict[str, Dict[str, Any]]:
    index: Dict[str, Dict[str, Any]] = {}
    for candidate in load_signal_candidates(journal_dir, days=days):
        cid = _candidate_id(candidate)
        if not cid:
            continue
        ev = candidate.get("ev_model") or {}
        index[cid] = {
            "legacy_score": _as_float(candidate.get("total_score")),
            "market_state": ev.get("market_state"),
            "features": extract_feature_flags(candidate),
        }
    return index


def load_labeled_from_counterfactual_journal(
    journal_dir: Path,
    *,
    days: List[str],
) -> List[Dict[str, Any]]:
    """Fast path: reuse EOD counterfactual_outcomes files (no tick re-simulation)."""
    feature_index = _candidate_feature_index(journal_dir, days)
    rows: List[Dict[str, Any]] = []
    for day_str in days:
        path = journal_dir / f"counterfactual_outcomes_{day_str}.jsonl"
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            cid = str(raw.get("candidate_id") or "")
            meta = feature_index.get(cid) or {}
            outcome = raw.get("outcome") or {}
            rows.append(
                {
                    "candidate_id": cid,
                    "signal_key": raw.get("signal_key"),
                    "session_day": raw.get("session_day") or day_str,
                    "evaluated_at": raw.get("evaluated_at"),
                    "legacy_paper_eligible": bool(raw.get("legacy_paper_eligible")),
                    "legacy_score": meta.get("legacy_score", 0.0),
                    "ev_trade_eligible": bool(raw.get("ev_trade_eligible")),
                    "predicted_probability": _normalize_probability(raw.get("predicted_probability")),
                    "predicted_ev_rupees": _as_float(raw.get("predicted_ev_rupees")),
                    "market_state": meta.get("market_state"),
                    "features": meta.get("features") or {},
                    "outcome": outcome,
                }
            )
    return rows


def train_ev_shadow_calibration(
    trade_date: Optional[date] = None,
    *,
    journal_dir: Path = JOURNAL_DIR,
    lookback_days: int = 30,
    save_weights: bool = True,
    save_calibration: bool = True,
    rebuild_labels: bool = False,
) -> Dict[str, Any]:
    """Batch trainer: dedupe thesis rows, refresh evidence weights, fit isotonic calibration."""
    d = trade_date or date.today()
    available = list_available_journal_days(journal_dir)
    if not available:
        return {"status": "NO_JOURNAL_DATA", "date": d.isoformat()}

    cutoff = d - timedelta(days=lookback_days)
    days = [day for day in available if _parse_day(day) >= cutoff]
    if d.isoformat() not in days:
        days.insert(0, d.isoformat())

    labeled_raw = (
        build_labeled_dataset(journal_dir, days=days)
        if rebuild_labels
        else load_labeled_from_counterfactual_journal(journal_dir, days=days)
    )
    if not labeled_raw and not rebuild_labels:
        labeled_raw = build_labeled_dataset(journal_dir, days=days)
    labeled = dedupe_thesis_rows(labeled_raw)
    calibration_fit = fit_isotonic_calibration(labeled)

    calibrated_rows = []
    anchors = calibration_fit.get("anchors") or []
    for row in labeled:
        raw_p = _as_float(row.get("predicted_probability"))
        cal_p = apply_isotonic_calibration(raw_p, anchors)
        calibrated_rows.append({**row, "predicted_probability_calibrated": cal_p})

    before = evaluate_probability_rows(labeled, prob_key="predicted_probability")
    after = evaluate_probability_rows(calibrated_rows, prob_key="predicted_probability_calibrated")

    evidence_calibration = calibrate_all_regimes(labeled)
    weights_saved = False
    if save_weights and labeled:
        weights_doc = {
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "training_sessions": days,
            "sample_size": len(labeled),
            "sample_size_raw": len(labeled_raw),
            "trainer": "ev_shadow_trainer_v1",
            "global_weights": evidence_calibration["global"]["weights"],
            "regime_weights": {k: v["weights"] for k, v in evidence_calibration["by_regime"].items()},
            "feature_stats": evidence_calibration["global"]["feature_stats"],
        }
        save_learned_weights(weights_doc)
        weights_saved = True

    calibration_doc = {
        "version": "ev_calibration_shadow_v1",
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "trainer": "ev_shadow_trainer_v1",
        "training_sessions": days,
        "lookback_days": lookback_days,
        "sample_size_raw": len(labeled_raw),
        "sample_size_thesis": len(labeled),
        "label_weights": {
            "actual": ACTUAL_WEIGHT,
            "counterfactual": COUNTERFACTUAL_WEIGHT,
        },
        "fit": calibration_fit,
        "metrics_before": before,
        "metrics_after": after,
        "improvement": {
            "brier_delta": round((before.get("brier_score") or 0) - (after.get("brier_score") or 0), 4),
            "ece_delta": round((before.get("calibration_error_ece") or 0) - (after.get("calibration_error_ece") or 0), 4),
            "bias_pp_delta": round((before.get("prediction_bias_pp") or 0) - (after.get("prediction_bias_pp") or 0), 2),
        },
        "shadow_only": True,
        "live_gate_impact": "none",
    }

    calibration_path = CALIBRATION_PATH
    report_path = TRAINING_REPORT_DIR / f"ev_shadow_training_{d.isoformat()}.json"
    if save_calibration:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        calibration_path.write_text(json.dumps(calibration_doc, indent=2), encoding="utf-8")
        report_path.write_text(json.dumps(calibration_doc, indent=2), encoding="utf-8")

    return {
        "status": "OK",
        "date": d.isoformat(),
        "sessions_included": days,
        "sample_size_raw": len(labeled_raw),
        "sample_size_thesis": len(labeled),
        "weights_saved": weights_saved,
        "calibration_saved": save_calibration,
        "calibration_path": str(calibration_path),
        "training_report_path": str(report_path),
        "metrics_before": before,
        "metrics_after": after,
        "improvement": calibration_doc["improvement"],
        "anchors": anchors,
    }


def main() -> None:
    """CLI entry point, folded in from v4's desk_ev_shadow_trainer.py wrapper."""
    import argparse

    from nifty.analytics.probability_engine import reload_shadow_calibration, refresh_evidence_weights

    parser = argparse.ArgumentParser(description="Train EV shadow calibration from journal history")
    parser.add_argument("--date", help="As-of trade date YYYY-MM-DD (default: today)")
    parser.add_argument("--lookback", type=int, default=30, help="Training lookback days")
    parser.add_argument("--no-save-weights", action="store_true", help="Skip evidence_weights.json update")
    parser.add_argument("--rebuild-labels", action="store_true", help="Rebuild counterfactual labels from scratch (slow)")
    parser.add_argument("--no-save-calibration", action="store_true", help="Skip ev_calibration_shadow.json update")
    args = parser.parse_args()

    trade_date = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else date.today()
    payload = train_ev_shadow_calibration(
        trade_date,
        lookback_days=args.lookback,
        save_weights=not args.no_save_weights,
        save_calibration=not args.no_save_calibration,
        rebuild_labels=args.rebuild_labels,
    )
    refresh_evidence_weights()
    reload_shadow_calibration()
    print(
        json.dumps(
            {
                "status": payload.get("status"),
                "date": payload.get("date"),
                "sample_size_raw": payload.get("sample_size_raw"),
                "sample_size_thesis": payload.get("sample_size_thesis"),
                "metrics_before": payload.get("metrics_before"),
                "metrics_after": payload.get("metrics_after"),
                "improvement": payload.get("improvement"),
                "calibration_path": payload.get("calibration_path"),
                "training_report_path": payload.get("training_report_path"),
                "weights_saved": payload.get("weights_saved"),
            },
            indent=2,
        )
    )


def _selftest() -> None:
    import tempfile

    # dedupe_thesis_rows: prefers EV-eligible tick, else highest legacy_score.
    rows = [
        {"session_day": "2026-07-20", "signal_key": "sig1", "ev_trade_eligible": False, "legacy_score": 60.0, "evaluated_at": "t1"},
        {"session_day": "2026-07-20", "signal_key": "sig1", "ev_trade_eligible": True, "legacy_score": 55.0, "evaluated_at": "t2"},
        {"session_day": "2026-07-20", "signal_key": "sig2", "ev_trade_eligible": False, "legacy_score": 70.0, "evaluated_at": "t1"},
        {"session_day": "2026-07-20", "signal_key": "sig2", "ev_trade_eligible": False, "legacy_score": 65.0, "evaluated_at": "t2"},
    ]
    deduped = dedupe_thesis_rows(rows)
    assert len(deduped) == 2
    sig1 = next(r for r in deduped if r["signal_key"] == "sig1")
    assert sig1["ev_trade_eligible"] is True  # EV-eligible tick wins over higher legacy_score
    sig2 = next(r for r in deduped if r["signal_key"] == "sig2")
    assert sig2["legacy_score"] == 70.0  # no EV-eligible rows -> highest legacy_score wins

    assert _normalize_probability(65) == 0.65  # >1 -> treated as percentage
    assert _normalize_probability(0.65) == 0.65

    # fit_isotonic_calibration + apply_isotonic_calibration round-trip.
    training_rows = [
        {"predicted_probability": 0.3 + (i % 10) * 0.05, "outcome": {"win": i % 3 == 0, "source": "actual"}}
        for i in range(60)
    ]
    fit = fit_isotonic_calibration(training_rows, buckets=4, min_bin_samples=10)
    assert fit["status"] == "OK"
    assert fit["anchors"]
    calibrated = apply_isotonic_calibration(0.5, fit["anchors"])
    assert 0.0 <= calibrated <= 1.0

    assert fit_isotonic_calibration([]) == {"status": "NO_DATA", "anchors": [], "buckets": DEFAULT_BUCKETS}
    assert apply_isotonic_calibration(0.5, []) == 0.5  # no anchors -> pass-through (clamped)

    empty_eval = evaluate_probability_rows([])
    assert empty_eval["sample_size"] == 0 and empty_eval["brier_score"] is None

    real_eval = evaluate_probability_rows(training_rows)
    assert real_eval["sample_size"] == 60
    assert real_eval["brier_score"] is not None

    # train_ev_shadow_calibration: no journal data -> graceful status, never raises.
    tmp = Path(tempfile.mkdtemp(prefix="ev-shadow-trainer-selftest-"))
    result = train_ev_shadow_calibration(date(2026, 7, 21), journal_dir=tmp)
    assert result["status"] == "NO_JOURNAL_DATA"

    print("[analytics.ev_shadow_trainer] selftest OK: dedupe, isotonic fit/apply, probability eval, no-data guard")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        main()
    else:
        _selftest()
