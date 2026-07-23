#!/usr/bin/env python3
"""Volatility-adaptive OI conviction for open paper positions and strike behavior.

Ported faithfully from quant-desk-engine's nifty_oi_conviction.py (mentor-authored).
No formula, threshold, or branch changed from the source.

Not yet wired into the live pipeline. This is designed to REPLACE state.py's
_analyze_pe_strike_row / _conviction_score / _evaluate_open_oi_conviction, which
use fixed thresholds (PLAYBOOK_VELOCITY_ADD_PCT=2.0, PLAYBOOK_SPOT_FLAT_PTS=8.0,
CONVICTION_FADE_STREAK=2 always) and a coarse 4-bucket score
(CONVICTION_LEVEL_SCORE = STRONG:100/NEUTRAL:50/WEAK:25/INVALIDATED:0). This
module instead scales those thresholds from expected-move/ATR/vol-regime and
produces a continuous 0-100 evidence-weighted score. Wiring this in is a
separate, deliberately-flagged step (decision behavior IS meant to change) —
see MIGRATION_PLAN.md / the porting todo list.

market_profile / liquidity_engine / volatility_engine are accepted per the
mentor's own interface but have no producer anywhere yet (confirmed: even in
quant-desk-engine every call site passes {} / a bare stub) — they degrade to
a no-op adjustment (delta=0) until those engines exist, exactly as designed.

Self-check: python -m nifty.analytics.oi_conviction
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from nifty.core.commission import CommissionConfig, commission_conviction_check

# Legacy defaults — used only when no vol context is available.
DEFAULT_SPOT_FLAT_PTS = 3.0
DEFAULT_VELOCITY_ADD_PCT = 2.0
DEFAULT_VELOCITY_UNWIND_PCT = -2.0
DEFAULT_INVALIDATION_STREAK = 2


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def vol_adaptive_oi_thresholds(
    *,
    expected_move_pts: float = 0.0,
    atr14_pts: float = 0.0,
    volatility_engine: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Scale spot/OI gates from expected move + ATR + vol regime.

    Quiet day → tighter OI % but wider spot noise band in points (from ATR).
    High vol / expansion → higher OI % bar + more invalidation patience (streak).
    """
    em = max(_as_float(expected_move_pts), 0.0)
    atr = max(_as_float(atr14_pts), 0.0)
    em_component = em * 0.06 if em > 0 else 0.0
    atr_component = atr * 0.12 if atr > 0 else 0.0
    spot_flat_pts = max(5.0, em_component, atr_component)
    spot_flat_pts = min(35.0, spot_flat_pts)

    ve = volatility_engine or {}
    regime = str(ve.get("volatility_regime") or "NORMAL").upper()
    expansion = bool(ve.get("vol_expansion"))
    compression = bool(ve.get("vol_compression"))

    if regime in {"HIGH_VOL", "NORMAL_HIGH"} or expansion:
        velocity_add_pct = 3.0
        velocity_unwind_pct = -3.0
        invalidation_streak = 3
        spot_mult = 1.15
        source = "high_vol"
    elif regime in {"LOW_VOL", "NORMAL_LOW"} or compression:
        velocity_add_pct = 1.5
        velocity_unwind_pct = -1.5
        invalidation_streak = 2
        spot_mult = 0.85
        source = "low_vol"
    else:
        velocity_add_pct = 2.0
        velocity_unwind_pct = -2.0
        invalidation_streak = 2
        spot_mult = 1.0
        source = "normal"

    spot_flat_pts = round(min(35.0, max(5.0, spot_flat_pts * spot_mult)), 2)
    spot_band_margin = round(max(2.0, spot_flat_pts * 0.20), 2)

    return {
        "spot_flat_pts": spot_flat_pts,
        "spot_weak_pts": spot_flat_pts,
        "spot_band_margin": spot_band_margin,
        "velocity_add_pct": velocity_add_pct,
        "velocity_unwind_pct": velocity_unwind_pct,
        "invalidation_streak": invalidation_streak,
        "volatility_regime": regime,
        "expected_move_pts": round(em, 1) if em > 0 else None,
        "atr14_pts": round(atr, 1) if atr > 0 else None,
        "source": source,
    }


def _spot_band(spot_delta: float, thresholds: Dict[str, Any]) -> str:
    flat = _as_float(thresholds.get("spot_flat_pts"), DEFAULT_SPOT_FLAT_PTS)
    margin = _as_float(thresholds.get("spot_band_margin"), flat * 0.20)
    up_line = flat + margin
    down_line = -(flat + margin)
    if spot_delta > up_line:
        return "UP"
    if spot_delta < down_line:
        return "DOWN"
    return "FLAT"


def analyze_pe_strike_behavior(
    pair: Optional[Dict[str, Any]],
    strike: int,
    spot_v5: Dict[str, float],
    *,
    spot: float = 0.0,
    thresholds: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Classify PE/CE OI vs spot at a strike using vol-adaptive gates."""
    th = thresholds or vol_adaptive_oi_thresholds()
    add_pct = _as_float(th.get("velocity_add_pct"), DEFAULT_VELOCITY_ADD_PCT)
    unwind_pct = _as_float(th.get("velocity_unwind_pct"), DEFAULT_VELOCITY_UNWIND_PCT)

    if not pair:
        return {
            "strike": strike,
            "tracked": False,
            "behavior": "NOT_IN_CHAIN",
            "read": "Strike not in subscription window",
            "status": "info",
            "thresholds": th,
        }

    pe = pair.get("pe") or {}
    ce = pair.get("ce") or {}
    pe_v5 = pe.get("velocity_5m") or {}
    ce_v5 = ce.get("velocity_5m") or {}
    oi_delta = _as_int(pe_v5.get("delta"))
    oi_pct = _as_float(pe_v5.get("pct"))
    pe_premium_delta = _as_float(pe_v5.get("price_delta"))
    spot_delta = _as_float(spot_v5.get("delta"))
    spot_band = _spot_band(spot_delta, th)

    pe_adding = oi_pct >= add_pct or oi_delta > 0
    pe_unwinding = oi_pct <= unwind_pct
    ce_adding = _as_float(ce_v5.get("pct")) >= add_pct

    if pe_adding and spot_band == "UP":
        behavior, read, status = "PE_ADD_SPOT_UP", "PE OI adding + spot rising — support confirmed", "ok"
    elif pe_adding and spot_band == "FLAT":
        behavior, read, status = (
            "PE_ADD_SPOT_FLAT",
            "PE OI adding but spot flat — writers stacking, need price follow-through",
            "warn",
        )
    elif pe_adding and spot_band == "DOWN":
        behavior, read, status = (
            "PE_ADD_SPOT_DOWN",
            "PE OI adding but spot falling — divergence, support not working yet",
            "bad",
        )
    elif pe_unwinding:
        behavior, read, status = "PE_UNWIND", "PE OI unwinding — support leaving", "bad"
    elif ce_adding:
        behavior, read, status = "CE_DOMINANT", "CE OI building at this strike — overhead pressure", "warn"
    else:
        behavior, read, status = "QUIET", "No strong PE/CE footprint at this strike", "info"

    return {
        "strike": strike,
        "tracked": True,
        "behavior": behavior,
        "read": read,
        "status": status,
        "pe_oi": pe.get("oi"),
        "pe_ltp": pe.get("last_price"),
        "ce_ltp": ce.get("last_price"),
        "pe_oi_5m_delta": oi_delta,
        "pe_oi_5m_pct": oi_pct,
        "pe_premium_5m_delta": pe_premium_delta,
        "ce_oi_5m_pct": _as_float(ce_v5.get("pct")),
        "spot_5m_delta": spot_delta,
        "spot_band": spot_band,
        "spot_dist": round(spot - strike, 2) if spot > 0 else None,
        "pair_read": pair.get("read"),
        "thresholds": th,
    }


def _conviction_score_buy_ce(
    pe_behavior: str,
    *,
    ce_5m_pct: float,
    pe_5m_pct: float,
    spot_delta: float,
    thresholds: Dict[str, Any],
) -> Tuple[int, str]:
    score = 50
    notes: List[str] = []
    flat_pts = _as_float(thresholds.get("spot_flat_pts"), DEFAULT_SPOT_FLAT_PTS)

    if pe_behavior == "PE_ADD_SPOT_UP":
        score += 35
        notes.append("PE+spot support aligned")
    elif pe_behavior == "PE_ADD_SPOT_FLAT":
        score -= 10
        notes.append("PE adding, spot not confirming")
    elif pe_behavior in {"PE_ADD_SPOT_DOWN", "PE_UNWIND"}:
        score -= 45
        notes.append("PE support failing")
    elif pe_behavior == "CE_DOMINANT":
        score -= 15
        notes.append("CE overhead building")
    else:
        score -= 8
        notes.append("Quiet footprint")

    if spot_delta > flat_pts:
        score += 5
    elif spot_delta < -flat_pts:
        score -= 10

    if pe_5m_pct >= _as_float(thresholds.get("velocity_add_pct")):
        score += 5
    if ce_5m_pct >= _as_float(thresholds.get("velocity_add_pct")):
        score -= 8

    return max(0, min(100, score)), "; ".join(notes)


def _conviction_score_buy_pe(
    pe_behavior: str,
    *,
    ce_5m_pct: float,
    pe_5m_pct: float,
    writer_5m_pct: float,
    spot_delta: float,
    thresholds: Dict[str, Any],
) -> Tuple[int, str]:
    score = 50
    notes: List[str] = []
    flat_pts = _as_float(thresholds.get("spot_flat_pts"), DEFAULT_SPOT_FLAT_PTS)
    add_pct = _as_float(thresholds.get("velocity_add_pct"), DEFAULT_VELOCITY_ADD_PCT)
    unwind_pct = _as_float(thresholds.get("velocity_unwind_pct"), DEFAULT_VELOCITY_UNWIND_PCT)

    ce_adding = ce_5m_pct >= add_pct or writer_5m_pct >= add_pct
    ce_unwinding = ce_5m_pct <= unwind_pct
    spot_weak = spot_delta <= flat_pts

    if ce_adding and spot_weak:
        score += 35
        notes.append("CE writers adding + spot weak — fade holds")
    elif pe_behavior == "PE_ADD_SPOT_UP":
        score -= 50
        notes.append("PE+spot rising — contradicts fade")
    elif ce_unwinding and spot_delta > flat_pts:
        score -= 40
        notes.append("CE covering + spot rising")
    elif ce_adding:
        score -= 12
        notes.append("CE building but spot not weak enough")
    else:
        score -= 8
        notes.append("Thesis aging — no fresh CE velocity")

    if pe_5m_pct >= add_pct and spot_delta > flat_pts:
        score -= 15
        notes.append("PE support building against fade")

    return max(0, min(100, score)), "; ".join(notes)


def _level_from_score(score: int) -> Tuple[str, str]:
    if score >= 72:
        return "STRONG", "ok"
    if score >= 45:
        return "WEAK", "warn"
    return "INVALIDATED", "bad"


def _engine_conviction_adjustment(
    decision: str,
    *,
    market_profile: Optional[Dict[str, Any]] = None,
    liquidity_engine: Optional[Dict[str, Any]] = None,
    volatility_engine: Optional[Dict[str, Any]] = None,
    options_analytics: Optional[Dict[str, Any]] = None,
) -> Tuple[int, List[str], List[str]]:
    """Adjust conviction score from desk engine layers; returns delta, notes, blockers."""
    delta = 0
    notes: List[str] = []
    blockers: List[str] = []

    mp = market_profile or {}
    if mp.get("status") == "READY":
        if decision == "BUY_CE" and mp.get("acceptance_rejection") == "ACCEPTED_ABOVE_POC" and mp.get("poor_high"):
            delta += 4
            notes.append("poor high fade risk lowered — still above POC")
        if decision == "BUY_PE" and mp.get("balance_state") == "IMBALANCED_EXPANSION":
            delta += 6
            notes.append("profile expansion supports fade")
        if decision == "BUY_CE" and mp.get("balance_state") == "BALANCED_ROTATION":
            delta += 4
            notes.append("balanced rotation supports dip CE")

    liq = liquidity_engine or {}
    if liq.get("status") == "READY":
        grab = str(liq.get("liquidity_grab") or "NONE")
        if grab == "UPSIDE_LIQUIDITY_GRAB" and decision == "BUY_PE":
            delta += 8
            notes.append("upside liquidity grab fade")
        elif grab == "DOWNSIDE_LIQUIDITY_GRAB" and decision == "BUY_CE":
            delta += 8
            notes.append("downside liquidity grab bounce")
        elif grab == "UPSIDE_LIQUIDITY_GRAB" and decision == "BUY_CE":
            delta -= 20
            blockers.append("LIQUIDITY_GRAB_CONFLICT")
        elif grab == "DOWNSIDE_LIQUIDITY_GRAB" and decision == "BUY_PE":
            delta -= 20
            blockers.append("LIQUIDITY_GRAB_CONFLICT")

    ve = volatility_engine or {}
    if ve.get("status") == "READY":
        if decision == "BUY_PE" and (ve.get("vol_expansion") or ve.get("volatility_regime") in {"HIGH_VOL", "NORMAL_HIGH"}):
            delta += 5
            notes.append("vol expansion supports PE")
        if decision == "BUY_CE" and ve.get("vol_compression"):
            delta += 4
            notes.append("vol compression supports CE bounce")

    oa = options_analytics or {}
    if oa and not oa.get("error"):
        gex = str(oa.get("gex_regime") or "")
        if decision == "BUY_CE" and gex == "POSITIVE_GAMMA":
            delta += 5
            notes.append("positive GEX")
        if decision == "BUY_PE" and gex == "NEGATIVE_GAMMA":
            delta += 5
            notes.append("negative GEX")

    return delta, notes, blockers


def evaluate_oi_conviction(
    signal: Dict[str, Any],
    pair: Optional[Dict[str, Any]],
    spot_v5: Dict[str, float],
    *,
    writer_5m_pct: float = 0.0,
    spot: float = 0.0,
    thresholds: Optional[Dict[str, Any]] = None,
    commission_cfg: Optional[CommissionConfig] = None,
    checked_at: str = "",
    market_profile: Optional[Dict[str, Any]] = None,
    liquidity_engine: Optional[Dict[str, Any]] = None,
    volatility_engine: Optional[Dict[str, Any]] = None,
    options_analytics: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Score open-position thesis vs live OI — vol-adaptive, less tick-flip noise."""
    th = thresholds or vol_adaptive_oi_thresholds()
    decision = str(signal.get("decision") or "")
    strike = _as_int(signal.get("strike"))

    pe_analysis = analyze_pe_strike_behavior(
        pair,
        strike,
        spot_v5,
        spot=spot,
        thresholds=th,
    )
    pe_behavior = str(pe_analysis.get("behavior") or "QUIET")
    spot_delta = _as_float(spot_v5.get("delta"))

    ce = (pair or {}).get("ce") or {}
    pe = (pair or {}).get("pe") or {}
    ce_5m_pct = _as_float((ce.get("velocity_5m") or {}).get("pct"))
    pe_5m_pct = _as_float((pe.get("velocity_5m") or {}).get("pct"))
    ce_15m_pct = _as_float((ce.get("velocity_15m") or {}).get("pct"))
    pe_15m_pct = _as_float((pe.get("velocity_15m") or {}).get("pct"))

    if decision == "BUY_CE":
        score, score_note = _conviction_score_buy_ce(
            pe_behavior,
            ce_5m_pct=ce_5m_pct,
            pe_5m_pct=pe_5m_pct,
            spot_delta=spot_delta,
            thresholds=th,
        )
        read = pe_analysis.get("read") or score_note
    elif decision == "BUY_PE":
        score, score_note = _conviction_score_buy_pe(
            pe_behavior,
            ce_5m_pct=ce_5m_pct,
            pe_5m_pct=pe_5m_pct,
            writer_5m_pct=writer_5m_pct,
            spot_delta=spot_delta,
            thresholds=th,
        )
        read = score_note or pe_analysis.get("read") or "Monitoring fade thesis"
    else:
        score, score_note = 50, "Unknown decision"
        read = "Unknown decision"

    eng_delta, eng_notes, eng_blockers = _engine_conviction_adjustment(
        decision,
        market_profile=market_profile,
        liquidity_engine=liquidity_engine,
        volatility_engine=volatility_engine,
        options_analytics=options_analytics,
    )
    score = max(0, min(100, score + eng_delta))
    if eng_notes:
        score_note = f"{score_note}; {'; '.join(eng_notes)}" if score_note else "; ".join(eng_notes)

    level, status = _level_from_score(score)
    if level == "WEAK" and score_note:
        read = f"Thesis cooling — {score_note}"
    elif level == "INVALIDATED":
        read = f"Thesis broken — {score_note or pe_analysis.get('read')}"

    current_price = _as_float(signal.get("current_price") or signal.get("entry_price"))
    target_price = _as_float(signal.get("target_price"))
    lot_size = _as_int(signal.get("lot_size"), (commission_cfg.lot_size if commission_cfg else 65))
    comm = (
        commission_conviction_check(current_price, target_price, lot_size, commission_cfg)
        if commission_cfg and current_price > 0
        else {"passed": False, "reason": "No entry price"}
    )

    return {
        "level": level,
        "status": status,
        "read": read,
        "conviction_score": score,
        "decision": decision,
        "strike": strike,
        "pe_behavior": pe_behavior,
        "spot_band": pe_analysis.get("spot_band"),
        "ce_oi_5m_pct": round(ce_5m_pct, 2),
        "pe_oi_5m_pct": round(pe_5m_pct, 2),
        "ce_oi_15m_pct": round(ce_15m_pct, 2),
        "pe_oi_15m_pct": round(pe_15m_pct, 2),
        "writer_oi_5m_pct": round(writer_5m_pct, 2),
        "spot_5m_delta": spot_delta,
        "thresholds": th,
        "engine_adjustment": eng_delta,
        "engine_notes": eng_notes,
        "engine_blockers": eng_blockers,
        "commission_to_target_pass": comm.get("passed"),
        "commission_reason": comm.get("reason"),
        "checked_at": checked_at,
    }


def required_invalidation_streak(thresholds: Optional[Dict[str, Any]] = None) -> int:
    th = thresholds or {}
    return max(1, _as_int(th.get("invalidation_streak"), DEFAULT_INVALIDATION_STREAK))


def _selftest() -> None:
    # Normal-vol thresholds match the legacy fixed constants exactly (no em/atr/regime context).
    th = vol_adaptive_oi_thresholds()
    assert th["velocity_add_pct"] == 2.0 and th["velocity_unwind_pct"] == -2.0
    assert th["invalidation_streak"] == 2
    assert th["spot_flat_pts"] == 5.0  # floor, no em/atr context

    # High-vol regime widens bands and patience.
    th_hv = vol_adaptive_oi_thresholds(expected_move_pts=200, atr14_pts=150, volatility_engine={"volatility_regime": "HIGH_VOL"})
    assert th_hv["velocity_add_pct"] == 3.0 and th_hv["invalidation_streak"] == 3
    assert th_hv["spot_flat_pts"] > th["spot_flat_pts"]

    pair = {"pe": {"oi": 500000, "last_price": 50.0, "velocity_5m": {"delta": 20000, "pct": 4.0, "price_delta": 1.0}},
            "ce": {"last_price": 40.0, "velocity_5m": {"pct": 0.5}}, "read": "PE writers adding"}
    behavior = analyze_pe_strike_behavior(pair, 23000, {"delta": 12.0}, spot=23000, thresholds=th)
    assert behavior["behavior"] == "PE_ADD_SPOT_UP", behavior

    score, note = _conviction_score_buy_ce("PE_ADD_SPOT_UP", ce_5m_pct=0.5, pe_5m_pct=4.0, spot_delta=12.0, thresholds=th)
    assert score == min(100, 50 + 35 + 5 + 5), (score, note)  # aligned + spot-up + pe-velocity bonuses

    level, status = _level_from_score(90)
    assert level == "STRONG" and status == "ok"
    level, status = _level_from_score(50)
    assert level == "WEAK"
    level, status = _level_from_score(10)
    assert level == "INVALIDATED" and status == "bad"

    # Engine adjustments are a documented no-op until those engines exist.
    delta, notes, blockers = _engine_conviction_adjustment("BUY_CE", market_profile=None, liquidity_engine=None, volatility_engine=None)
    assert delta == 0 and notes == [] and blockers == []

    result = evaluate_oi_conviction(
        {"decision": "BUY_CE", "strike": 23000, "current_price": 55.0, "target_price": 75.0, "lot_size": 65},
        pair, {"delta": 12.0}, thresholds=th,
    )
    assert result["level"] == "STRONG"
    assert result["conviction_score"] == score
    assert required_invalidation_streak(th) == 2
    assert required_invalidation_streak(th_hv) == 3

    print("[analytics.oi_conviction] selftest OK: vol-adaptive thresholds, behavior, scoring, engine no-op")


if __name__ == "__main__":
    _selftest()
