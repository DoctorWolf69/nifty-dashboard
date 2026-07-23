#!/usr/bin/env python3
"""Intent filter — strike / chain / spot layers to separate OI noise from real intent.

Ported faithfully from quant-desk-engine's nifty_intent_filter.py (mentor-authored).
One deliberate omission: the original compute_chain_bias() could optionally route
through nifty_oi_velocity_engine.compute_chain_bias_normalized() when a velocity_ctx
was supplied. That engine (context-normalized OI z-scores) was tried in this repo
and explicitly reverted (commits 67e60ea..137efd2) — every alert/gamma/ranking path
here uses raw OI deltas by design. So velocity_ctx is accepted for interface
compatibility but always ignored; the raw-delta path (the original's own fallback
when no velocity_ctx was given) is the only behavior. Everything else is unchanged.

Not yet wired into the live pipeline — see MIGRATION_PLAN.md / the porting todo list.
Self-check: python -m nifty.analytics.intent_filter
"""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

CHAIN_DOMINANCE_RATIO = 1.5
CHAIN_MIN_TOTAL_DELTA = 50_000
BUY_PE_MAX_SPOT_5M_PTS = 3.0
SUBSCRIPTION_STRIKES_MIN = 3
SUBSCRIPTION_STRIKES_MAX = 8


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


def trade_and_watch_radius(expected_move_pts: float) -> Tuple[float, float]:
    """Dynamic radii from ATM straddle expected move (mentor §2 backlog)."""
    em = expected_move_pts if expected_move_pts > 0 else 400.0
    trade = min(250.0, max(100.0, 0.25 * em))
    watch = min(500.0, max(150.0, 0.50 * em))
    return round(trade, 1), round(watch, 1)


def hybrid_trade_watch_radius(
    expected_move_pts: float,
    atr14_pts: float = 0.0,
    *,
    event_day: bool = False,
    gap_pts: float = 0.0,
) -> Tuple[float, float, str]:
    """Hybrid IV expected move + ATR14; widen on event / large-gap days."""
    gap_ref = abs(gap_pts) if gap_pts else 0.0
    em_ref = max(expected_move_pts, gap_ref * 0.5) if (expected_move_pts > 0 or gap_ref > 0) else 0.0
    trade_em, watch_em = trade_and_watch_radius(em_ref) if em_ref > 0 else (0.0, 0.0)
    trade_atr, watch_atr = trade_and_watch_radius(atr14_pts) if atr14_pts > 0 else (0.0, 0.0)
    trade = max(trade_em, trade_atr)
    watch = max(watch_em, watch_atr)
    if trade <= 0:
        trade, watch = 150.0, 300.0
        source = "default"
    elif trade_atr >= trade_em and atr14_pts > 0:
        source = "atr14"
    else:
        source = "expected_move"
    if event_day:
        trade = min(250.0, round(trade * 1.15, 1))
        watch = min(500.0, round(watch * 1.15, 1))
        source = f"{source}+event"
    return trade, watch, source


def strikes_each_side_from_watch(watch_radius_pts: float, *, strike_step: int = 100) -> int:
    """Kite subscription half-width from watch radius."""
    if watch_radius_pts <= 0 or strike_step <= 0:
        return SUBSCRIPTION_STRIKES_MIN
    needed = int(math.ceil(watch_radius_pts / strike_step))
    return max(SUBSCRIPTION_STRIKES_MIN, min(SUBSCRIPTION_STRIKES_MAX, needed))


def subscription_window_strikes(
    center_strike: int,
    *,
    strikes_each_side: int,
    strike_step: int,
    extra_strikes: Optional[Iterable[int]] = None,
) -> Tuple[int, int]:
    """Inclusive low/high strike for the live Kite subscription window."""
    low = center_strike - strikes_each_side * strike_step
    high = center_strike + strikes_each_side * strike_step
    for strike in extra_strikes or ():
        try:
            val = int(round(float(strike)))
        except (TypeError, ValueError):
            continue
        if val > 0:
            low = min(low, val)
            high = max(high, val)
    return low, high


def subscription_needs_recenter(
    spot: float,
    center_strike: int,
    *,
    strikes_each_side: int,
    strike_step: int,
    watch_radius_pts: float,
    extra_strikes: Optional[Iterable[int]] = None,
) -> bool:
    """True when spot is within watch radius of the subscribed strike window edge."""
    if spot <= 0 or center_strike <= 0:
        return False
    low, high = subscription_window_strikes(
        center_strike,
        strikes_each_side=strikes_each_side,
        strike_step=strike_step,
        extra_strikes=extra_strikes,
    )
    margin = max(watch_radius_pts, 0.0)
    return (spot - low) < margin or (high - spot) < margin


def _journal_strike(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("strike")
    try:
        strike = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return strike if strike > 0 else None


def subscription_strikes_from_journal(
    journal_dir: Path,
    *,
    trade_date: Optional[str] = None,
    default_each_side: int = 3,
) -> Tuple[int, Set[int]]:
    """Build Kite strike window from morning desk / premarket artifacts.

    Reads the SAME journal files and keys nifty-dashboard's morning pipeline
    already writes: desk_brief_{day}.json / premarket_scan_{day}.json /
    morning_desk_{day}.json, with oi_map.ceiling/floor/max_pain, key_range
    (support/resistance), options_analytics.expected_move_pts, cash_open_gap.
    """
    day = trade_date or date.today().isoformat()
    extra: Set[int] = set()
    strikes_each_side = max(1, default_each_side)
    expected_move = 0.0
    for name in (f"desk_brief_{day}.json", f"premarket_scan_{day}.json", f"morning_desk_{day}.json"):
        path = journal_dir / name
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        oi_map = payload.get("oi_map") or {}
        for key in ("ceiling", "floor"):
            strike = _journal_strike(oi_map.get(key))
            if strike:
                extra.add(strike)
        for strike in (_journal_strike(oi_map.get("max_pain")), _journal_strike(payload.get("max_pain"))):
            if strike:
                extra.add(strike)
        key_range = payload.get("key_range") or {}
        for key in ("support", "resistance"):
            strike = _journal_strike(key_range.get(key))
            if strike:
                extra.add(strike)
        for container_key in ("options_analytics", "ph7_9", "ph7"):
            em = _as_float((payload.get(container_key) or {}).get("expected_move_pts"))
            if em > expected_move:
                expected_move = em
        gap = payload.get("cash_open_gap") or {}
        gap_pts = abs(_as_float(gap.get("gap_pts")))
        if gap_pts > expected_move:
            expected_move = gap_pts
    if expected_move > 0:
        _, watch = trade_and_watch_radius(expected_move)
        strikes_each_side = max(strikes_each_side, strikes_each_side_from_watch(watch))
    return strikes_each_side, extra


def compute_chain_bias(
    paired_rows: List[Dict[str, Any]],
    *,
    spot: float,
    trade_radius: float,
    velocity_ctx: Optional[Dict[str, Any]] = None,  # accepted, always ignored — see module docstring
) -> Dict[str, Any]:
    """Sum CE vs PE raw 5m OI velocity within trade radius."""
    ce_sum = 0
    pe_sum = 0
    strikes_in = 0
    for pair in paired_rows:
        strike = _as_int(pair.get("strike"))
        if spot > 0 and abs(strike - spot) > trade_radius:
            continue
        ce = pair.get("ce") or {}
        pe = pair.get("pe") or {}
        ce_delta = _as_int((ce.get("velocity_5m") or {}).get("delta"))
        pe_delta = _as_int((pe.get("velocity_5m") or {}).get("delta"))
        ce_sum += max(0, ce_delta)
        pe_sum += max(0, pe_delta)
        strikes_in += 1

    total = ce_sum + pe_sum
    label = "NEUTRAL"
    detail = "Mixed or quiet chain OI within trade radius"
    if total < CHAIN_MIN_TOTAL_DELTA:
        label = "QUIET"
        detail = f"Chain OI adds quiet ({total:,} total 5m delta within {trade_radius:.0f} pts)"
    elif pe_sum > ce_sum * CHAIN_DOMINANCE_RATIO:
        label = "PE_DOMINANT_CHAIN"
        detail = f"PE OI adds dominate chain ({pe_sum:,} vs CE {ce_sum:,}) — CE long thesis favored"
    elif ce_sum > pe_sum * CHAIN_DOMINANCE_RATIO:
        label = "CE_DOMINANT_CHAIN"
        detail = f"CE OI adds dominate chain ({ce_sum:,} vs PE {pe_sum:,}) — PE long thesis favored"
    elif ce_sum > 0 and pe_sum > 0:
        label = "CHOP"
        detail = f"Both sides adding across chain ({ce_sum:,} CE / {pe_sum:,} PE) — chop zone"

    return {
        "label": label,
        "detail": detail,
        "ce_sum_5m": ce_sum,
        "pe_sum_5m": pe_sum,
        "strikes_in_radius": strikes_in,
        "trade_radius_pts": trade_radius,
    }


def evaluate_intent_filter(
    *,
    decision: str,
    pair_read: str,
    pe_behavior: str,
    chain_bias: Dict[str, Any],
    spot_v5_delta: float = 0.0,
    options_analytics: Optional[Dict[str, Any]] = None,
    market_profile: Optional[Dict[str, Any]] = None,
    liquidity_engine: Optional[Dict[str, Any]] = None,
    volatility_engine: Optional[Dict[str, Any]] = None,
    spot_flat_pts: float = BUY_PE_MAX_SPOT_5M_PTS,
    spot: float = 0.0,
) -> Dict[str, Any]:
    """
    Layer 1 strike + Layer 2 chain intent gates.
    Returns intent label, blockers, and detail for scoreboard journal.
    """
    blockers: List[str] = []
    pair_read = str(pair_read or "")
    pe_behavior = str(pe_behavior or "")
    chain_label = str((chain_bias or {}).get("label") or "NEUTRAL")

    # Layer 1 — strike
    if pair_read == "Both sides adding":
        blockers.append("BOTH_SIDES_ADDING")
    if decision == "BUY_CE" and pe_behavior == "CE_DOMINANT":
        blockers.append("STRIKE_INTENT_CONFLICT")
    if decision == "BUY_PE" and pe_behavior == "PE_ADD_SPOT_UP":
        blockers.append("STRIKE_INTENT_CONFLICT")

    # Layer 2 — chain dominant side
    if chain_label == "CHOP":
        blockers.append("CHAIN_DIRECTION_CONFLICT")
    elif chain_label == "CE_DOMINANT_CHAIN" and decision == "BUY_CE":
        blockers.append("CHAIN_DIRECTION_CONFLICT")
    elif chain_label == "PE_DOMINANT_CHAIN" and decision == "BUY_PE":
        blockers.append("CHAIN_DIRECTION_CONFLICT")

    # Layer 3 — spot must agree with bearish PE-long thesis (vol-adaptive flat band)
    if decision == "BUY_PE" and spot_v5_delta > spot_flat_pts:
        blockers.append("SPOT_NOT_WEAK_FOR_PE")

    # Layer 4 — market profile / liquidity (hard blocks only on clear conflicts)
    mp = market_profile or {}
    if mp.get("status") == "READY" and spot > 0:
        vah = _as_float(mp.get("vah"))
        val = _as_float(mp.get("val"))
        acceptance = str(mp.get("acceptance_rejection") or "")
        # Match confluence: block CE only when extended above VAH, not merely above POC
        if decision == "BUY_CE" and vah > 0 and spot > vah and acceptance == "ACCEPTED_ABOVE_POC":
            blockers.append("MARKET_PROFILE_CONFLICT")
        if decision == "BUY_PE" and val > 0 and spot < val and acceptance == "ACCEPTED_BELOW_POC":
            blockers.append("MARKET_PROFILE_CONFLICT")

    liq = liquidity_engine or {}
    ve = volatility_engine or {}
    if liq.get("status") == "READY":
        grab = str(liq.get("liquidity_grab") or "NONE")
        if grab == "UPSIDE_LIQUIDITY_GRAB" and decision == "BUY_CE":
            blockers.append("LIQUIDITY_GRAB_CONFLICT")
        if grab == "DOWNSIDE_LIQUIDITY_GRAB" and decision == "BUY_PE":
            blockers.append("LIQUIDITY_GRAB_CONFLICT")

    # Vol regime gates live in confluence dimensions (score + selective blockers) — not duplicated here.

    # Ph 7–9 GEX / price–delta divergence
    if options_analytics:
        div_label = str((options_analytics.get("price_delta_divergence") or {}).get("label") or "")
        gex_regime = str(options_analytics.get("gex_regime") or "")
        if decision == "BUY_CE" and div_label == "BUYER_ABSORPTION":
            blockers.append("GEX_DELTA_CONFLICT")
        if decision == "BUY_PE" and div_label == "SELLER_ABSORPTION":
            blockers.append("GEX_DELTA_CONFLICT")
        if gex_regime == "NEGATIVE_GAMMA" and div_label in {"AGGRESSIVE_BUYERS", "AGGRESSIVE_SELLERS"}:
            blockers.append("GEX_VOL_EXPANSION")

    intent_blockers = {
        "BOTH_SIDES_ADDING",
        "STRIKE_INTENT_CONFLICT",
        "CHAIN_DIRECTION_CONFLICT",
        "SPOT_NOT_WEAK_FOR_PE",
        "GEX_DELTA_CONFLICT",
        "GEX_VOL_EXPANSION",
        "MARKET_PROFILE_CONFLICT",
        "LIQUIDITY_GRAB_CONFLICT",
    }
    active_intent = [b for b in blockers if b in intent_blockers]

    if active_intent:
        if pair_read == "Both sides adding" or chain_label == "CHOP":
            intent = "NOISE"
        else:
            intent = "CONFLICT"
    elif pair_read in {"Stable / mixed", "Both sides unwinding"}:
        intent = "NOISE"
    elif chain_label in {"PE_DOMINANT_CHAIN", "CE_DOMINANT_CHAIN"}:
        if (chain_label == "PE_DOMINANT_CHAIN" and decision == "BUY_CE") or (
            chain_label == "CE_DOMINANT_CHAIN" and decision == "BUY_PE"
        ):
            intent = "QUALIFIED"
        else:
            intent = "NEUTRAL"
    else:
        intent = "QUALIFIED"

    details = [pair_read or "—", chain_bias.get("detail") or ""]
    if pe_behavior:
        details.append(f"PE behavior {pe_behavior}")
    if options_analytics:
        div = options_analytics.get("price_delta_divergence") or {}
        if div.get("label"):
            details.append(f"Δ div {div.get('label')}")
        if options_analytics.get("gex_regime"):
            details.append(str(options_analytics.get("gex_regime")))
    if mp.get("status") == "READY" and mp.get("balance_state"):
        details.append(f"profile {mp.get('balance_state')}")
    if liq.get("liquidity_grab") and liq.get("liquidity_grab") != "NONE":
        details.append(f"liq {liq.get('liquidity_grab')}")
    if ve.get("volatility_regime"):
        details.append(f"vol {ve.get('volatility_regime')}")

    return {
        "intent": intent,
        "blockers": active_intent,
        "pair_read": pair_read,
        "chain_bias": chain_label,
        "pe_behavior": pe_behavior,
        "detail": " · ".join(d for d in details if d),
    }


def _selftest() -> None:
    # trade_and_watch_radius: mid-range expected move
    trade, watch = trade_and_watch_radius(200.0)
    assert trade == 100.0 and watch == 150.0, (trade, watch)  # both clamp to their floors
    trade, watch = trade_and_watch_radius(800.0)
    assert trade == 200.0 and watch == 400.0, (trade, watch)

    assert strikes_each_side_from_watch(0) == SUBSCRIPTION_STRIKES_MIN
    assert strikes_each_side_from_watch(250, strike_step=100) == 3
    assert strikes_each_side_from_watch(900, strike_step=100) == SUBSCRIPTION_STRIKES_MAX

    low, high = subscription_window_strikes(23000, strikes_each_side=3, strike_step=100, extra_strikes=[23450])
    assert (low, high) == (22700, 23450), (low, high)

    assert subscription_needs_recenter(
        23440, 23000, strikes_each_side=3, strike_step=100, watch_radius_pts=100
    ) is True
    assert subscription_needs_recenter(
        23000, 23000, strikes_each_side=3, strike_step=100, watch_radius_pts=100
    ) is False

    # velocity_ctx must be accepted but never change the (raw) result
    rows = [
        {"strike": 23000, "ce": {"velocity_5m": {"delta": 10000}}, "pe": {"velocity_5m": {"delta": 80000}}},
        {"strike": 23100, "ce": {"velocity_5m": {"delta": 5000}}, "pe": {"velocity_5m": {"delta": 20000}}},
    ]
    a = compute_chain_bias(rows, spot=23000, trade_radius=150)
    b = compute_chain_bias(rows, spot=23000, trade_radius=150, velocity_ctx={"anything": 1})
    assert a == b == {
        "label": "PE_DOMINANT_CHAIN", "detail": a["detail"],
        "ce_sum_5m": 15000, "pe_sum_5m": 100000, "strikes_in_radius": 2, "trade_radius_pts": 150,
    }

    result = evaluate_intent_filter(
        decision="BUY_CE", pair_read="PE writers adding", pe_behavior="PE_ADD_SPOT_UP",
        chain_bias=a, spot_v5_delta=2.0, spot=23000,
    )
    assert result["intent"] == "QUALIFIED", result

    print("[analytics.intent_filter] selftest OK: radii, subscription window, chain bias, intent gates")


if __name__ == "__main__":
    _selftest()
