#!/usr/bin/env python3
"""
Quantitative trade research fields for the NIFTY desk journal.

Ported faithfully from quant-desk-engine's nifty_trade_research.py
(mentor-authored). No logic changed. Fully self-contained — duck-types the
engine state via getattr() rather than importing OIVelocityState, so it
never needed adaptation.

Turns paper-trade rows into a research database (not just a diary): a
stable Setup ID, inferred market regime, entry quality score, running
MAE/MFE, an R-multiple on close, and a plain-English inferred lesson.

market_profile / volatility_engine inputs degrade the same documented way
as elsewhere in this porting batch: nifty-dashboard's OIVelocityState has no
market_profile or _volatility_engine_summary attribute yet, so
getattr(..., None) reads {} and the regime falls back to the playbook-phase
heuristic — honest, not broken. options_analytics IS real here (state.py's
own analyze_option_chain output), so iv_rank/gex_regime are live figures
once wired in.

Not yet wired into the live pipeline.
Self-check: python -m nifty.analytics.trade_research
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def infer_setup_id(decision: str, playbook_phase: str = "", alert: Optional[Dict[str, Any]] = None) -> str:
    """Map participant setup to a stable Setup ID (e.g. OI-03)."""
    phase = (playbook_phase or "UNKNOWN").upper()
    side = str(decision or "").upper()
    key = (phase, side)
    mapping = {
        ("GAP_UP", "BUY_CE"): "OI-01",
        ("GAP_UP", "BUY_PE"): "OI-02",
        ("GAP_DOWN", "BUY_PE"): "OI-03",
        ("GAP_DOWN", "BUY_CE"): "OI-04",
        ("FLAT_OPEN", "BUY_CE"): "OI-05",
        ("FLAT_OPEN", "BUY_PE"): "OI-06",
        ("EXPIRY_WATCH", "BUY_CE"): "OI-07",
        ("EXPIRY_WATCH", "BUY_PE"): "OI-08",
        ("GAP_UP", "SELL_CE"): "OI-11",
        ("GAP_UP", "SELL_PE"): "OI-12",
        ("GAP_DOWN", "SELL_PE"): "OI-13",
        ("GAP_DOWN", "SELL_CE"): "OI-14",
    }
    setup = mapping.get(key)
    if setup:
        return setup
    reasons = alert.get("key_area_reasons") if alert else None
    if isinstance(reasons, list) and "ORB low" in reasons:
        return "OI-09" if side == "BUY_CE" else "OI-10"
    return f"OI-{side[-2:] if len(side) >= 2 else 'XX'}-99"


def infer_market_regime(state: Any, market_profile: Optional[Dict[str, Any]] = None) -> str:
    mp = market_profile or getattr(state, "market_profile", None) or {}
    if isinstance(mp, dict) and mp.get("status") == "READY":
        balance = str(mp.get("balance_state") or "")
        if "IMBALANCED" in balance:
            return "Trend"
        if "BALANCED" in balance:
            return "Range"
    playbook = getattr(state, "_playbook_phase", None) or ""
    if not playbook and hasattr(state, "intraday_playbook"):
        pb = getattr(state, "intraday_playbook", {}) or {}
        playbook = pb.get("phase") if isinstance(pb, dict) else ""
    phase = str(playbook or "").upper()
    if phase in {"GAP_UP", "GAP_DOWN"}:
        return "Trend"
    return "Range"


def entry_quality_from_grade(grade: str, score: Optional[float] = None) -> int:
    grade_map = {"A": 5, "B": 4, "C": 3, "D": 2, "F": 1}
    base = grade_map.get(str(grade or "").upper(), 3)
    if score is not None and score >= 90:
        return min(5, base + 1)
    if score is not None and score < 65:
        return max(1, base - 1)
    return base


def build_entry_research(
    state: Any,
    *,
    decision: str,
    grade: str,
    total_score: Optional[float],
    alert: Optional[Dict[str, Any]] = None,
    playbook: Optional[Dict[str, Any]] = None,
    market_profile: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    oa = getattr(state, "options_analytics", {}) or {}
    ve = getattr(state, "volatility_engine", {}) or {}
    if not ve and hasattr(state, "_volatility_engine_summary"):
        try:
            ve = state._volatility_engine_summary()
        except Exception:
            ve = {}
    pb = playbook or {}
    generated = datetime.now().strftime("%H:%M")
    iv_rank = oa.get("iv_rank")
    if iv_rank is None:
        iv_rank = ve.get("implied_vol")
    gex = str(oa.get("gex_regime") or "UNKNOWN")
    if "POSITIVE" in gex:
        gamma_regime = "Positive"
    elif "NEGATIVE" in gex:
        gamma_regime = "Negative"
    else:
        gamma_regime = "Neutral"
    entry_spot = _as_float(getattr(state, "spot", 0))
    return {
        "setup_id": infer_setup_id(decision, str(pb.get("phase") or ""), alert),
        "market_regime": infer_market_regime(state, market_profile),
        "iv_rank": round(_as_float(iv_rank), 1) if iv_rank is not None else None,
        "gamma_regime": gamma_regime,
        "time_of_day": generated,
        "entry_quality": entry_quality_from_grade(grade, total_score),
        "entry_spot": entry_spot,
        "mae_pct": 0.0,
        "mfe_pct": 0.0,
        "mae_spot_pts": 0.0,
        "mfe_spot_pts": 0.0,
        "weekday": WEEKDAY_NAMES[datetime.now().weekday()],
        "is_expiry_day": bool(pb.get("is_expiry_day")),
        "playbook_phase": pb.get("phase"),
        "lesson": "",
        "result_r": None,
    }


def update_mae_mfe(signal: Dict[str, Any], *, current_price: float, spot: float) -> None:
    research = signal.setdefault("research", {})
    entry_price = _as_float(signal.get("entry_price"))
    entry_spot = _as_float(research.get("entry_spot") or signal.get("spot"))
    if entry_price > 0 and current_price > 0:
        pnl_pct = ((current_price - entry_price) / entry_price) * 100.0
        research["mae_pct"] = round(min(_as_float(research.get("mae_pct"), 0.0), pnl_pct), 2)
        research["mfe_pct"] = round(max(_as_float(research.get("mfe_pct"), 0.0), pnl_pct), 2)
    if entry_spot > 0 and spot > 0:
        spot_delta = spot - entry_spot
        research["mae_spot_pts"] = round(min(_as_float(research.get("mae_spot_pts"), 0.0), spot_delta), 2)
        research["mfe_spot_pts"] = round(max(_as_float(research.get("mfe_spot_pts"), 0.0), spot_delta), 2)


def compute_result_r(signal: Dict[str, Any]) -> Optional[float]:
    entry = _as_float(signal.get("entry_price"))
    exit_px = _as_float(signal.get("exit_price") or signal.get("current_price"))
    stop = _as_float(signal.get("stop_price"))
    if entry <= 0 or exit_px <= 0:
        return None
    risk = entry - stop if stop > 0 else entry * 0.30
    if risk <= 0:
        return None
    reward = exit_px - entry
    return round(reward / risk, 2)


def infer_lesson(signal: Dict[str, Any], research: Dict[str, Any]) -> str:
    exit_reason = str(signal.get("exit_reason") or "")
    result_r = research.get("result_r")
    mae = _as_float(research.get("mae_pct"))
    quality = int(research.get("entry_quality") or 3)
    if exit_reason == "OI_CONVICTION_BROKEN":
        return "Participant thesis broke before price stop — wait for re-confirmation."
    if result_r is not None and result_r >= 1.5:
        return "Setup worked — replicate context and timing."
    if result_r is not None and result_r < 0 and quality >= 4 and mae < -15:
        return "Early entry before confirmation — tighten spot/OI gate."
    if exit_reason == "STOP_HIT":
        return "Stop hit — check if entry was against chain bias or vol expansion."
    if exit_reason == "TARGET_HIT":
        return "Target achieved — review if partial exit rules would improve expectancy."
    return "Review tape: was OI velocity sustained or one-minute noise?"


def finalize_research_on_close(signal: Dict[str, Any]) -> Dict[str, Any]:
    research = dict(signal.get("research") or {})
    research["result_r"] = compute_result_r(signal)
    research["lesson"] = research.get("lesson") or infer_lesson(signal, research)
    signal["research"] = research
    return research


def research_row_for_export(signal: Dict[str, Any]) -> Dict[str, Any]:
    r = signal.get("research") or {}
    return {
        "setup_id": r.get("setup_id"),
        "market_regime": r.get("market_regime"),
        "iv_rank": r.get("iv_rank"),
        "gamma_regime": r.get("gamma_regime"),
        "time_of_day": r.get("time_of_day"),
        "entry_quality": r.get("entry_quality"),
        "mae_pct": r.get("mae_pct"),
        "mfe_pct": r.get("mfe_pct"),
        "mae_spot_pts": r.get("mae_spot_pts"),
        "mfe_spot_pts": r.get("mfe_spot_pts"),
        "result_r": r.get("result_r"),
        "lesson": r.get("lesson"),
        "weekday": r.get("weekday"),
        "is_expiry_day": r.get("is_expiry_day"),
    }


def _selftest() -> None:
    class _FakeState:
        options_analytics = {"iv_rank": 38.5, "gex_regime": "NEGATIVE_GAMMA"}
        spot = 23085.4

    assert infer_setup_id("BUY_PE", "GAP_DOWN") == "OI-03"
    assert infer_setup_id("BUY_CE", "UNKNOWN", {"key_area_reasons": ["ORB low"]}) == "OI-09"
    assert infer_setup_id("BUY_PE", "UNKNOWN") == "OI-XX-99" or infer_setup_id("BUY_PE", "UNKNOWN").endswith("-99")

    assert infer_market_regime(_FakeState()) == "Range"  # no market_profile, no playbook phase set
    assert entry_quality_from_grade("A", 95) == 5
    assert entry_quality_from_grade("C", 50) == 2  # low score shaves a point off the base

    research = build_entry_research(
        _FakeState(), decision="BUY_PE", grade="B", total_score=72,
        playbook={"phase": "GAP_DOWN", "is_expiry_day": False},
    )
    assert research["setup_id"] == "OI-03"
    assert research["iv_rank"] == 38.5
    assert research["gamma_regime"] == "Negative"
    assert research["entry_spot"] == 23085.4

    signal = {"entry_price": 100.0, "research": dict(research)}
    update_mae_mfe(signal, current_price=90.0, spot=23070.0)
    assert signal["research"]["mae_pct"] == -10.0
    update_mae_mfe(signal, current_price=115.0, spot=23100.0)
    assert signal["research"]["mfe_pct"] == 15.0
    assert signal["research"]["mae_pct"] == -10.0  # MAE doesn't improve on a later up-move

    signal.update({"exit_price": 115.0, "stop_price": 70.0, "exit_reason": "TARGET_HIT"})
    r = compute_result_r(signal)
    assert r == round((115.0 - 100.0) / (100.0 - 70.0), 2)

    final = finalize_research_on_close(signal)
    assert final["result_r"] == r
    assert "review" in final["lesson"].lower() or "achieved" in final["lesson"].lower()

    row = research_row_for_export(signal)
    assert row["setup_id"] == "OI-03" and row["result_r"] == r

    print("[analytics.trade_research] selftest OK: setup id, regime, quality, MAE/MFE, R-multiple, lesson")


if __name__ == "__main__":
    _selftest()
