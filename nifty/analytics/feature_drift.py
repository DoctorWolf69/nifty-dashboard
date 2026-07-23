#!/usr/bin/env python3
"""Feature drift detection — monitor predictive power evolution (no auto-retrain).

Ported faithfully from quant-desk-engine's nifty_feature_drift.py
(mentor-authored). No formula, threshold, or branch changed. Adaptations:
  - nifty_model_learning -> nifty.analytics.model_learning (ported)
  - nifty_journal_store.JOURNAL_DIR -> nifty.paths.JOURNAL_DIR
  - nifty_relationships_lab.list_available_journal_days ->
    nifty.analytics.journal_reader.list_available_journal_days (extracted there)

Not yet wired into the live pipeline.
Self-check: python -m nifty.analytics.feature_drift
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from nifty.paths import JOURNAL_DIR
from nifty.analytics.journal_reader import list_available_journal_days
from nifty.analytics.model_learning import build_labeled_dataset, calibrate_log_odds

TREND_STRONGER = "STRONGER"
TREND_WEAKER = "WEAKER"
TREND_STABLE = "STABLE"
TREND_UNSTABLE = "UNSTABLE"


def _parse_day(value: str) -> date:
    return datetime.strptime(value[:10], "%Y-%m-%d").date()


def _importance_from_calibration(cal: Dict[str, Any]) -> Dict[str, float]:
    stats = cal.get("feature_stats") or {}
    weights = cal.get("weights") or {}
    out: Dict[str, float] = {}
    for feature, meta in stats.items():
        w = weights.get(feature, 0.0)
        if meta.get("calibrated"):
            out[feature] = abs(float(w))
        else:
            out[feature] = abs(float(w)) * 0.3
    return out


def _trend_and_drift(
    short_imp: float,
    long_imp: float,
    overall_imp: float,
) -> Dict[str, Any]:
    if long_imp < 1e-6:
        drift = 0.0
    else:
        drift = (short_imp - long_imp) / long_imp
    if abs(drift) < 0.15:
        trend = TREND_STABLE
    elif drift > 0.25:
        trend = TREND_STRONGER
    elif drift < -0.25:
        trend = TREND_WEAKER
    else:
        trend = TREND_STABLE
    volatility = abs(short_imp - overall_imp) / max(overall_imp, 0.01)
    if volatility > 0.4:
        trend = TREND_UNSTABLE
    confidence = max(0.0, min(1.0, 1.0 - volatility))
    return {
        "drift_score": round(drift, 3),
        "trend": trend,
        "confidence": round(confidence, 2),
    }


def compute_feature_drift(
    journal_dir=JOURNAL_DIR,
    *,
    short_days: int = 30,
    long_days: int = 90,
    as_of: Optional[date] = None,
    labeled_rows: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Compare rolling 30d vs 90d vs overall feature importance.
    Monitor only — does not retrain weights.
    """
    as_of = as_of or date.today()
    available = list_available_journal_days(journal_dir)
    if not available:
        return {"status": "NO_DATA", "features": []}

    def days_window(n: int) -> List[str]:
        cutoff = as_of - timedelta(days=n)
        return [d for d in available if _parse_day(d) >= cutoff]

    short_days_list = days_window(short_days)
    long_days_list = days_window(long_days)

    if labeled_rows is None:
        labeled_rows = build_labeled_dataset(journal_dir, days=available)

    labeled_short = [r for r in labeled_rows if r.get("session_day") in short_days_list]
    labeled_long = [r for r in labeled_rows if r.get("session_day") in long_days_list]
    labeled_all = labeled_rows

    cal_short = calibrate_log_odds(labeled_short) if labeled_short else {"weights": {}, "feature_stats": {}}
    cal_long = calibrate_log_odds(labeled_long) if labeled_long else {"weights": {}, "feature_stats": {}}
    cal_all = calibrate_log_odds(labeled_all) if labeled_all else {"weights": {}, "feature_stats": {}}

    imp_short = _importance_from_calibration(cal_short)
    imp_long = _importance_from_calibration(cal_long)
    imp_all = _importance_from_calibration(cal_all)

    features = sorted(set(imp_short) | set(imp_long) | set(imp_all))
    report_features: List[Dict[str, Any]] = []
    stronger: List[str] = []
    weaker: List[str] = []
    unstable: List[str] = []

    for feature in features:
        s = imp_short.get(feature, 0.0)
        lg = imp_long.get(feature, 0.0)
        o = imp_all.get(feature, 0.0)
        meta = _trend_and_drift(s, lg, o)
        row = {
            "feature": feature,
            "importance_30d": round(s, 4),
            "importance_90d": round(lg, 4),
            "importance_overall": round(o, 4),
            **meta,
        }
        report_features.append(row)
        if meta["trend"] == TREND_STRONGER:
            stronger.append(feature)
        elif meta["trend"] == TREND_WEAKER:
            weaker.append(feature)
        elif meta["trend"] == TREND_UNSTABLE:
            unstable.append(feature)

    report_features.sort(key=lambda r: r["importance_overall"], reverse=True)

    return {
        "status": "OK",
        "as_of": as_of.isoformat(),
        "windows": {"short_days": short_days, "long_days": long_days},
        "sample_sizes": {
            "short": len(labeled_short),
            "long": len(labeled_long),
            "overall": len(labeled_all),
        },
        "features": report_features,
        "summary": {
            "strengthening": stronger[:8],
            "weakening": weaker[:8],
            "unstable": unstable[:8],
        },
        "auto_retrain": False,
        "note": "Monitor only — weights are not auto-updated from drift report.",
    }


def _selftest() -> None:
    assert _trend_and_drift(1.0, 1.0, 1.0)["trend"] == TREND_STABLE
    assert _trend_and_drift(1.5, 1.0, 1.2)["trend"] == TREND_STRONGER
    assert _trend_and_drift(0.6, 1.0, 0.9)["trend"] == TREND_WEAKER
    assert _trend_and_drift(0.0, 0.0, 0.0)["drift_score"] == 0.0  # long_imp < 1e-6 guard

    cal = {"feature_stats": {"spot_confirms": {"calibrated": True}, "chain_aligns": {"calibrated": False}},
           "weights": {"spot_confirms": 0.5, "chain_aligns": 0.3}}
    imp = _importance_from_calibration(cal)
    assert imp["spot_confirms"] == 0.5
    assert imp["chain_aligns"] == round(0.3 * 0.3, 10)  # uncalibrated features are damped 70%

    import tempfile
    from pathlib import Path
    empty_dir = Path(tempfile.mkdtemp(prefix="feature-drift-selftest-"))
    result = compute_feature_drift(empty_dir)
    assert result["status"] == "NO_DATA"

    print("[analytics.feature_drift] selftest OK: trend classification, importance damping, no-data guard")


if __name__ == "__main__":
    _selftest()
