#!/usr/bin/env python3
"""Multi-factor confluence scoring for NIFTY OI velocity signal candidates."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from nifty.core.commission import CommissionConfig, commission_conviction_check

# Normalized OI-velocity pass thresholds for the oi_velocity dimension (env-overridable).
OIV_Z_DIM = float(os.getenv("OIV_Z_DIM", "1.5"))
OIV_PCT_DIM = float(os.getenv("OIV_PCT_DIM", "90"))

# Signed-grade bands for the shadow (negative-inclusive) grader, on a -100..+100 scale.
SIGNED_GRADE_A = 60
SIGNED_GRADE_B = 30
SIGNED_GRADE_C = 0

CONFLUENCE_WEIGHTS: Dict[str, int] = {
    "key_area": 12,
    "oi_sustained": 15,
    "volume_confirm": 15,
    "oi_velocity": 13,
    "spot_confirm": 15,
    "writer_price": 10,
    "commission": 10,
    "atm_proximity": 10,
}

TRADE_MIN_CONFLUENCE = 65
MIN_POSITIVE_MINUTE_ADDS = 3
MIN_VOLUME_CONFIRMED_MINUTES = 3
PLAYBOOK_SPOT_FLAT_PTS = 8.0
MAX_SIGNAL_STRIKE_DISTANCE_PTS = 150.0


def _dim(score: int, maximum: int, passed: bool, detail: str) -> Dict[str, Any]:
    return {
        "score": score if passed else 0,
        "max": maximum,
        "pass": passed,
        "detail": detail,
    }


def _grade(total: int, max_total: int) -> str:
    pct = (total / max_total * 100) if max_total else 0
    if pct >= 80:
        return "A"
    if pct >= 65:
        return "B"
    if pct >= 50:
        return "C"
    return "WATCH"


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


def score_signal_candidate(
    alert: Dict[str, Any],
    *,
    spot: float,
    spot_v5: Dict[str, Any],
    pe_behavior: str,
    entry_row: Optional[Dict[str, Any]],
    commission_cfg: CommissionConfig,
    playbook: Optional[Dict[str, Any]] = None,
    morning_context: Optional[Dict[str, Any]] = None,
    open_signals_count: int = 0,
    block_same_thesis_stack: bool = True,
    in_orb_no_trade: bool = False,
    late_session: bool = False,
    open_strikes: Optional[List[int]] = None,
    open_decisions: Optional[set] = None,
    single_direction_book: bool = True,
    last_signal_ts: float = 0.0,
    now_ts: float = 0.0,
    require_pe_spot_for_buy_ce: bool = True,
    require_spot_weak_for_buy_pe: bool = True,
    signal_cooldown_sec: int = 600,
    min_open_strike_spacing: int = 100,
) -> Dict[str, Any]:
    """Score one abnormal alert; record all dimensions and blockers for journal review."""
    writer_side = str(alert.get("option_type") or "")
    strike = _as_int(alert.get("strike"))
    direction = str(alert.get("direction") or "")
    if writer_side == "CE":
        decision = "BUY_PE"
        entry_side = "PE"
    elif writer_side == "PE":
        decision = "BUY_CE"
        entry_side = "CE"
    else:
        decision = "UNKNOWN"
        entry_side = ""

    signal_key = f"{strike}:{writer_side}:{decision}"
    spot_delta = _as_float(spot_v5.get("delta"))
    recent = alert.get("recent_1m_deltas") or []
    positive_recent = [row for row in recent if _as_int(row.get("oi_delta")) > 0]
    volume_positive = [row for row in positive_recent if _as_int(row.get("volume_delta")) > 0]
    reason = str(alert.get("reason") or "")
    # Normalized OI-velocity outlier (replaces raw ΔOI "chain outlier" flag).
    oiv_adding = _as_float(alert.get("oiv_adding"))
    oiv_pctl = _as_float(alert.get("velocity_percentile"))
    oiv_pass = oiv_adding >= OIV_Z_DIM or oiv_pctl >= OIV_PCT_DIM

    dimensions: Dict[str, Dict[str, Any]] = {}
    dimensions["key_area"] = _dim(
        CONFLUENCE_WEIGHTS["key_area"],
        CONFLUENCE_WEIGHTS["key_area"],
        bool(alert.get("key_area")),
        ", ".join(alert.get("key_area_reasons") or []) or "Not at flagged key area",
    )
    dimensions["oi_sustained"] = _dim(
        CONFLUENCE_WEIGHTS["oi_sustained"],
        CONFLUENCE_WEIGHTS["oi_sustained"],
        len(positive_recent) >= MIN_POSITIVE_MINUTE_ADDS,
        f"{len(positive_recent)}/{len(recent)} positive OI minutes",
    )
    dimensions["volume_confirm"] = _dim(
        CONFLUENCE_WEIGHTS["volume_confirm"],
        CONFLUENCE_WEIGHTS["volume_confirm"],
        len(volume_positive) >= MIN_VOLUME_CONFIRMED_MINUTES,
        f"{len(volume_positive)} volume-confirmed add minutes",
    )
    dimensions["oi_velocity"] = _dim(
        CONFLUENCE_WEIGHTS["oi_velocity"],
        CONFLUENCE_WEIGHTS["oi_velocity"],
        oiv_pass,
        f"oiv z {oiv_adding:.2f} / pctl {oiv_pctl:.0f} (need z≥{OIV_Z_DIM} or pctl≥{OIV_PCT_DIM:.0f})",
    )

    spot_ok = False
    spot_detail = f"spot 5m {spot_delta:+.1f} pts"
    if decision == "BUY_CE" and require_pe_spot_for_buy_ce:
        spot_ok = pe_behavior == "PE_ADD_SPOT_UP"
        spot_detail = f"PE behavior {pe_behavior or 'NA'} — need PE_ADD_SPOT_UP"
    elif decision == "BUY_PE" and require_spot_weak_for_buy_pe:
        spot_ok = spot_delta <= PLAYBOOK_SPOT_FLAT_PTS
        spot_detail = f"spot 5m {spot_delta:+.1f} — need flat/falling (≤ {PLAYBOOK_SPOT_FLAT_PTS})"
    else:
        spot_ok = True
    dimensions["spot_confirm"] = _dim(
        CONFLUENCE_WEIGHTS["spot_confirm"],
        CONFLUENCE_WEIGHTS["spot_confirm"],
        spot_ok,
        spot_detail,
    )

    writer_confirmed = direction == "WRITERS ADDING"
    dimensions["writer_price"] = _dim(
        CONFLUENCE_WEIGHTS["writer_price"],
        CONFLUENCE_WEIGHTS["writer_price"],
        writer_confirmed,
        direction or "unknown",
    )

    entry_price = _as_float((entry_row or {}).get("last_price"))
    target_price = round(entry_price * 1.50, 2) if entry_price > 0 else 0.0
    commission = (
        commission_conviction_check(entry_price, target_price, commission_cfg.lot_size, commission_cfg)
        if entry_price > 0
        else {"passed": False, "reason": "No entry price"}
    )
    dimensions["commission"] = _dim(
        CONFLUENCE_WEIGHTS["commission"],
        CONFLUENCE_WEIGHTS["commission"],
        bool(commission.get("passed")),
        str(commission.get("reason") or "commission check"),
    )

    dist = abs(spot - strike) if spot > 0 else 999.0
    prox_score = max(0, int(CONFLUENCE_WEIGHTS["atm_proximity"] * (1 - dist / MAX_SIGNAL_STRIKE_DISTANCE_PTS)))
    prox_pass = dist <= MAX_SIGNAL_STRIKE_DISTANCE_PTS
    dimensions["atm_proximity"] = {
        "score": prox_score if prox_pass else 0,
        "max": CONFLUENCE_WEIGHTS["atm_proximity"],
        "pass": prox_pass,
        "detail": f"{dist:.0f} pts from spot (max {MAX_SIGNAL_STRIKE_DISTANCE_PTS:.0f})",
    }

    total_score = sum(_as_int(dim.get("score")) for dim in dimensions.values())
    max_score = sum(CONFLUENCE_WEIGHTS.values())

    # Shadow signed grader: each dimension contributes +weight (pass) or -weight
    # (fail). Range -100..+100. Does NOT affect paper eligibility — comparison only.
    signed_dimensions = {
        name: (dim["max"] if dim.get("pass") else -dim["max"]) for name, dim in dimensions.items()
    }
    signed_score = sum(signed_dimensions.values())
    signed_grade = (
        "A" if signed_score >= SIGNED_GRADE_A
        else "B" if signed_score >= SIGNED_GRADE_B
        else "C" if signed_score >= SIGNED_GRADE_C
        else "WATCH"
    )

    blockers: List[str] = []
    if in_orb_no_trade:
        blockers.append("ORB_NO_TRADE")
    if late_session:
        blockers.append("LATE_SESSION")
    if open_signals_count >= 1:
        blockers.append("MAX_OPEN")
    if block_same_thesis_stack and open_signals_count >= 1:
        blockers.append("THESIS_STACK")
    if not prox_pass:
        blockers.append("STRIKE_TOO_FAR")
    open_strikes = open_strikes or []
    if any(abs(strike - open_strike) <= min_open_strike_spacing for open_strike in open_strikes):
        blockers.append("STRIKE_SPACING")
    open_decisions = open_decisions or set()
    if single_direction_book and open_decisions and decision not in open_decisions:
        blockers.append("DIRECTION_CONFLICT")
    if require_pe_spot_for_buy_ce and decision == "BUY_CE" and not spot_ok:
        blockers.append("PE_SPOT_NOT_CONFIRMED")
    if require_spot_weak_for_buy_pe and decision == "BUY_PE" and not spot_ok:
        blockers.append("SPOT_NOT_WEAK")
    if not commission.get("passed"):
        blockers.append("COMMISSION_TOO_THIN")
    if not writer_confirmed:
        blockers.append("WRITER_NOT_CONFIRMED")
    if now_ts - last_signal_ts < signal_cooldown_sec:
        blockers.append("COOLDOWN")
    if not entry_row or entry_price <= 0:
        blockers.append("NO_ENTRY_CONTRACT")

    paper_blockers = {
        "ORB_NO_TRADE",
        "LATE_SESSION",
        "MAX_OPEN",
        "THESIS_STACK",
        "STRIKE_SPACING",
        "DIRECTION_CONFLICT",
        "COOLDOWN",
        "NO_ENTRY_CONTRACT",
        "FUTURES_MACRO_CONFLICT",
    }
    hard_blocked = any(item in paper_blockers for item in blockers)
    confluence_ready = total_score >= TRADE_MIN_CONFLUENCE
    paper_eligible = confluence_ready and len(blockers) == 0

    bias = str((morning_context or {}).get("combined_bias") or "UNKNOWN")
    playbook_phase = str((playbook or {}).get("phase") or "")

    return {
        "event": "SIGNAL_CANDIDATE",
        "signal_key": signal_key,
        "decision": decision,
        "entry_side": entry_side,
        "writer_side": writer_side,
        "strike": strike,
        "writer_contract": alert.get("contract"),
        "entry_contract": (entry_row or {}).get("tradingsymbol"),
        "entry_price": entry_price or None,
        "target_price": target_price or None,
        "spot": spot,
        "spot_5m_delta": spot_delta,
        "pe_behavior": pe_behavior,
        "total_score": total_score,
        "max_score": max_score,
        "score_pct": round((total_score / max_score) * 100, 1) if max_score else 0,
        "grade": _grade(total_score, max_score),
        "signed_score": signed_score,
        "signed_grade": signed_grade,
        "signed_dimensions": signed_dimensions,
        "dimensions": dimensions,
        "blockers": blockers,
        "confluence_ready": confluence_ready,
        "paper_eligible": paper_eligible,
        "paper_min_score": TRADE_MIN_CONFLUENCE,
        "combined_bias": bias,
        "playbook_phase": playbook_phase,
        "source_alert": {
            "contract": alert.get("contract"),
            "direction": direction,
            "reason": reason,
            "key_area_reasons": alert.get("key_area_reasons"),
            "velocity_5m": alert.get("velocity_5m"),
            "velocity_1m": alert.get("velocity_1m"),
        },
        "commission_check": commission,
    }
