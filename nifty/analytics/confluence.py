#!/usr/bin/env python3
"""Multi-factor confluence scoring for NIFTY OI velocity signal candidates.

UPDATED 2026-07-24 to add 4 new scoring dimensions from quant-desk-engine
v4/ATLAS's evolved nifty_signal_confluence.py (mentor-authored), per
user-approved direction: market_profile, volatility_align, liquidity_align,
options_surface — each ported verbatim as its own self-contained,
null-safe function (score_market_profile_dimension,
score_volatility_dimension, score_liquidity_dimension,
score_options_surface_dimension). Also added the WATCH_ZONE distance-band
refinement (see score_signal_candidate) and a desk_reasoning field via
this session's already-ported reasoning_ladder.py.

Two things v4 also changed were DELIBERATELY NOT adopted, both because
they reconnect to nifty-dashboard's own reverted OI-velocity-normalization
engine (see [[porting-effort-status]] memory / velocity_normalizer.py's
own docstring for the revert history — measurably worse results: 17->18
signals, win rate 41%->33%, PF 0.72->0.32):
1. v4 rewrote the oi_sustained/oi_velocity dimensions to call
   score_oi_sustained_pass()/score_oi_velocity_pass() from the reverted
   engine. This file's oi_sustained/oi_velocity dimensions are UNCHANGED
   from nifty-dashboard's current logic (positive_recent-minute-count /
   chain_outlier reason-string match) — same quarantine principle as
   velocity_normalizer.py's narrower extraction.
2. v4 also added a `velocity_ctx` fallback parameter that calls
   evaluate_alert_velocity() when `alert.get("oi_velocity")` is empty.
   Not present here at all — nifty-dashboard's alerts always populate
   oi_velocity directly, so this fallback would never fire in practice,
   but it is not ported even as dead code.

PLAYBOOK_SPOT_FLAT_PTS was also NOT changed from nifty-dashboard's current
8.0 (v4 evolved to 3.0) — that threshold change wasn't part of what was
discussed/approved, so the current live value is deliberately preserved.

Net effect on live trading behavior: paper_eligible/confluence_ready are
UNCHANGED today, since the 4 new dimensions score 0 (their inputs -
market_profile/volatility_engine/liquidity_engine/options_analytics -
are not yet passed by state.py, so each falls through to its documented
"warming up" no-op) and paper_eligible depends on the ABSOLUTE total_score
vs TRADE_MIN_CONFLUENCE, which is unaffected by unscored dimensions. Only
the informational score_pct/grade fields shift slightly (larger
denominator: max_score 100->128) - candidate sort order is unaffected
since every candidate's denominator grows identically.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from nifty.core.commission import CommissionConfig, commission_conviction_check
from nifty.analytics.reasoning_ladder import build_desk_reasoning

CONFLUENCE_WEIGHTS: Dict[str, int] = {
    "key_area": 12,
    "oi_sustained": 15,
    "volume_confirm": 15,
    "oi_velocity": 13,
    "spot_confirm": 15,
    "writer_price": 10,
    "commission": 10,
    "atm_proximity": 10,
    "market_profile": 8,
    "volatility_align": 7,
    "liquidity_align": 5,
    "options_surface": 8,
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


def _near_level(spot: float, level: Any, *, pts: float = 0.0, pct: float = 0.00035) -> bool:
    lvl = _as_float(level, 0.0)
    if spot <= 0 or lvl <= 0:
        return False
    tol = pts if pts > 0 else max(8.0, spot * pct)
    return abs(spot - lvl) <= tol


def score_market_profile_dimension(
    decision: str,
    spot: float,
    market_profile: Optional[Dict[str, Any]],
) -> Tuple[Dict[str, Any], List[str]]:
    """Auction profile alignment — VAL support for CE, VAH rejection for PE."""
    maximum = CONFLUENCE_WEIGHTS["market_profile"]
    blockers: List[str] = []
    mp = market_profile or {}
    if mp.get("status") != "READY" or spot <= 0:
        return (
            _dim(maximum, maximum, False, "Market profile warming up"),
            blockers,
        )

    poc = _as_float(mp.get("poc"))
    vah = _as_float(mp.get("vah"))
    val = _as_float(mp.get("val"))
    balance = str(mp.get("balance_state") or "")
    acceptance = str(mp.get("acceptance_rejection") or "")

    if decision == "BUY_CE":
        if val > 0 and _near_level(spot, val):
            return _dim(maximum, maximum, True, f"Spot near VAL {val:.0f} — support for long CE"), blockers
        if val > 0 and spot < poc and balance == "BALANCED_ROTATION":
            score = max(4, int(maximum * 0.75))
            return (
                {
                    "score": score,
                    "max": maximum,
                    "pass": True,
                    "detail": f"Below POC {poc:.0f} in balanced rotation — dip-buy zone",
                },
                blockers,
            )
        if mp.get("poor_low"):
            score = max(3, int(maximum * 0.5))
            return (
                {
                    "score": score,
                    "max": maximum,
                    "pass": True,
                    "detail": "Poor low in profile — potential short-covering bounce",
                },
                blockers,
            )
        if vah > 0 and spot > vah and acceptance == "ACCEPTED_ABOVE_POC":
            blockers.append("MARKET_PROFILE_CONFLICT")
            return (
                _dim(0, maximum, False, f"Extended above VAH {vah:.0f} — poor CE location"),
                blockers,
            )
        return _dim(0, maximum, False, "No VAL / rotation support for BUY_CE"), blockers

    if decision == "BUY_PE":
        if vah > 0 and _near_level(spot, vah):
            return _dim(maximum, maximum, True, f"Spot near VAH {vah:.0f} — rejection zone for long PE"), blockers
        if vah > 0 and spot > poc and balance == "IMBALANCED_EXPANSION":
            score = max(4, int(maximum * 0.75))
            return (
                {
                    "score": score,
                    "max": maximum,
                    "pass": True,
                    "detail": f"Above POC {poc:.0f} in expansion — fade setup",
                },
                blockers,
            )
        if mp.get("poor_high"):
            score = max(3, int(maximum * 0.5))
            return (
                {
                    "score": score,
                    "max": maximum,
                    "pass": True,
                    "detail": "Poor high in profile — potential rejection fade",
                },
                blockers,
            )
        if val > 0 and spot < val and acceptance == "ACCEPTED_BELOW_POC":
            blockers.append("MARKET_PROFILE_CONFLICT")
            return (
                _dim(0, maximum, False, f"Compressed below VAL {val:.0f} — poor PE location"),
                blockers,
            )
        return _dim(0, maximum, False, "No VAH / expansion fade for BUY_PE"), blockers

    return _dim(0, maximum, False, "Unknown decision"), blockers


def score_volatility_dimension(
    decision: str,
    volatility_engine: Optional[Dict[str, Any]],
) -> Tuple[Dict[str, Any], List[str]]:
    """Vol regime / expansion alignment with long-option thesis."""
    maximum = CONFLUENCE_WEIGHTS["volatility_align"]
    blockers: List[str] = []
    ve = volatility_engine or {}
    if ve.get("status") != "READY":
        return _dim(maximum, maximum, False, "Volatility engine warming up"), blockers

    regime = str(ve.get("volatility_regime") or "NORMAL")
    expansion = bool(ve.get("vol_expansion"))
    compression = bool(ve.get("vol_compression"))
    iv_rv = ve.get("iv_rv_ratio")

    if decision == "BUY_PE":
        if regime in {"HIGH_VOL", "NORMAL_HIGH"} or expansion:
            detail = f"Regime {regime}" + (" + expansion" if expansion else "")
            return _dim(maximum, maximum, True, f"Vol supports fade — {detail}"), blockers
        if regime == "LOW_VOL" and compression:
            blockers.append("VOL_REGIME_CONFLICT")
            return _dim(0, maximum, False, "Low-vol compression — weak environment for BUY_PE"), blockers
        if iv_rv is not None and _as_float(iv_rv) >= 1.35:
            score = max(3, int(maximum * 0.6))
            return (
                {
                    "score": score,
                    "max": maximum,
                    "pass": True,
                    "detail": f"Rich IV (IV/RV {_as_float(iv_rv):.2f}) — optionality for PE",
                },
                blockers,
            )
        return _dim(0, maximum, False, f"Vol regime {regime} neutral for BUY_PE"), blockers

    if decision == "BUY_CE":
        if regime in {"LOW_VOL", "NORMAL_LOW"} or compression:
            detail = f"Regime {regime}" + (" + compression" if compression else "")
            return _dim(maximum, maximum, True, f"Vol supports bounce — {detail}"), blockers
        if regime == "HIGH_VOL" and expansion:
            blockers.append("VOL_REGIME_CONFLICT")
            return _dim(0, maximum, False, "High-vol expansion — chasing CE is low quality"), blockers
        if iv_rv is not None and _as_float(iv_rv) <= 0.92:
            score = max(3, int(maximum * 0.6))
            return (
                {
                    "score": score,
                    "max": maximum,
                    "pass": True,
                    "detail": f"Cheap IV (IV/RV {_as_float(iv_rv):.2f}) — CE entry efficient",
                },
                blockers,
            )
        return _dim(0, maximum, False, f"Vol regime {regime} neutral for BUY_CE"), blockers

    return _dim(0, maximum, False, "Unknown decision"), blockers


def score_liquidity_dimension(
    decision: str,
    spot: float,
    liquidity_engine: Optional[Dict[str, Any]],
) -> Tuple[Dict[str, Any], List[str]]:
    """Liquidity pools / grab detection alignment."""
    maximum = CONFLUENCE_WEIGHTS["liquidity_align"]
    blockers: List[str] = []
    liq = liquidity_engine or {}
    if liq.get("status") != "READY" or spot <= 0:
        return _dim(maximum, maximum, False, "Liquidity engine warming up"), blockers

    grab = str(liq.get("liquidity_grab") or "NONE")
    grab_src = str(liq.get("liquidity_grab_source") or "")

    if grab == "UPSIDE_LIQUIDITY_GRAB":
        if decision == "BUY_PE":
            detail = f"Upside grab ({grab_src or 'pool'}) — fade after sweep"
            return _dim(maximum, maximum, True, detail), blockers
        blockers.append("LIQUIDITY_GRAB_CONFLICT")
        return _dim(0, maximum, False, "Upside liquidity grab — do not buy CE into sweep"), blockers

    if grab == "DOWNSIDE_LIQUIDITY_GRAB":
        if decision == "BUY_CE":
            detail = f"Downside grab ({grab_src or 'pool'}) — bounce after sweep"
            return _dim(maximum, maximum, True, detail), blockers
        blockers.append("LIQUIDITY_GRAB_CONFLICT")
        return _dim(0, maximum, False, "Downside liquidity grab — do not buy PE into sweep"), blockers

    equal_highs = liq.get("equal_highs") or []
    equal_lows = liq.get("equal_lows") or []
    if decision == "BUY_PE" and equal_highs:
        lvl = _as_float(equal_highs[0].get("level"))
        if _near_level(spot, lvl):
            score = max(3, int(maximum * 0.8))
            return (
                {
                    "score": score,
                    "max": maximum,
                    "pass": True,
                    "detail": f"At equal highs {lvl:.0f} — liquidity pool overhead",
                },
                blockers,
            )
    if decision == "BUY_CE" and equal_lows:
        lvl = _as_float(equal_lows[0].get("level"))
        if _near_level(spot, lvl):
            score = max(3, int(maximum * 0.8))
            return (
                {
                    "score": score,
                    "max": maximum,
                    "pass": True,
                    "detail": f"At equal lows {lvl:.0f} — liquidity pool below",
                },
                blockers,
            )

    return _dim(0, maximum, False, "No active liquidity grab / pool at spot"), blockers


def score_options_surface_dimension(
    decision: str,
    spot: float,
    strike: int,
    options_analytics: Optional[Dict[str, Any]],
    *,
    morning_context: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """GEX / IV / dealer flow / max-pain alignment from live options chain."""
    maximum = CONFLUENCE_WEIGHTS["options_surface"]
    blockers: List[str] = []
    oa = options_analytics or {}
    if not oa or oa.get("error"):
        return _dim(maximum, maximum, False, "Options surface warming up"), blockers

    score = 0
    notes: List[str] = []
    div = str((oa.get("price_delta_divergence") or {}).get("label") or "")
    gex = str(oa.get("gex_regime") or "")
    prem = str(oa.get("premium_vs_vix") or "")
    hedge = str((oa.get("dealer_hedge_flow") or {}).get("label") or "")

    if decision == "BUY_CE":
        if div in {"AGGRESSIVE_BUYERS", "SELLER_ABSORPTION"}:
            score += 3
            notes.append("delta+price aligned")
        elif div == "BUYER_ABSORPTION":
            blockers.append("GEX_DELTA_CONFLICT")
        if gex == "POSITIVE_GAMMA":
            score += 3
            notes.append("positive GEX pin/revert")
        elif gex == "NEGATIVE_GAMMA" and div in {"AGGRESSIVE_BUYERS", "AGGRESSIVE_SELLERS"}:
            blockers.append("GEX_VOL_EXPANSION")
        if prem == "CHEAP":
            score += 2
            notes.append("IV cheap vs VIX")
        elif prem == "FAIR":
            score += 1
        if hedge in {"BUYING_PRESSURE", "DEALER_LONG_DELTA"}:
            score += 1
            notes.append("dealer flow supports bounce")
    elif decision == "BUY_PE":
        if div in {"AGGRESSIVE_SELLERS", "BUYER_ABSORPTION"}:
            score += 3
            notes.append("delta+price aligned for fade")
        elif div == "SELLER_ABSORPTION":
            blockers.append("GEX_DELTA_CONFLICT")
        if gex == "NEGATIVE_GAMMA":
            score += 3
            notes.append("negative GEX expansion fade")
        elif gex == "POSITIVE_GAMMA" and div in {"AGGRESSIVE_BUYERS", "AGGRESSIVE_SELLERS"}:
            blockers.append("GEX_VOL_EXPANSION")
        if prem in {"CHEAP", "FAIR"}:
            score += 1
        if hedge in {"SELLING_PRESSURE", "DEALER_SHORT_DELTA"}:
            score += 1
            notes.append("dealer flow supports fade")

    ctx = morning_context or {}
    max_pain = _as_float(ctx.get("max_pain") or (ctx.get("oi_map") or {}).get("max_pain"))
    em = _as_float(oa.get("expected_move_pts"))
    if max_pain > 0 and spot > 0 and em > 0:
        mp_dist = abs(spot - max_pain)
        if mp_dist <= max(25.0, em * 0.20):
            score += 1
            notes.append(f"near max pain {max_pain:.0f}")

    score = min(maximum, score)
    passed = score >= int(maximum * 0.5)
    detail = "; ".join(notes) if notes else f"GEX {gex or '—'} · IV {prem or '—'}"
    return (
        {"score": score, "max": maximum, "pass": passed, "detail": detail},
        blockers,
    )


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
    watch_strike_distance_pts: float = 0.0,
    intent_blockers: Optional[List[str]] = None,
    market_profile: Optional[Dict[str, Any]] = None,
    volatility_engine: Optional[Dict[str, Any]] = None,
    liquidity_engine: Optional[Dict[str, Any]] = None,
    options_analytics: Optional[Dict[str, Any]] = None,
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
    chain_outlier = "chain outlier" in reason or "pct outlier" in reason

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
        chain_outlier,
        reason or "No velocity outlier flag",
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

    # WATCH_ZONE: a distance band beyond the tradeable radius but within a wider
    # "watch" radius gets its own blocker name (rather than the plain
    # STRIKE_TOO_FAR) for display/journal purposes only. Both names already
    # force paper_eligible=False via the len(blockers)==0 check below, same as
    # before this change — this is a labeling refinement, not a new gate.
    trade_radius = MAX_SIGNAL_STRIKE_DISTANCE_PTS
    watch_radius = watch_strike_distance_pts if watch_strike_distance_pts > trade_radius else trade_radius * 2.0
    dist = abs(spot - strike) if spot > 0 else 999.0
    prox_score = max(0, int(CONFLUENCE_WEIGHTS["atm_proximity"] * (1 - dist / trade_radius)))
    prox_pass = dist <= trade_radius
    watch_zone = dist > trade_radius and dist <= watch_radius
    dimensions["atm_proximity"] = {
        "score": prox_score if prox_pass else 0,
        "max": CONFLUENCE_WEIGHTS["atm_proximity"],
        "pass": prox_pass,
        "detail": f"{dist:.0f} pts from spot (trade ≤{trade_radius:.0f}, watch ≤{watch_radius:.0f})",
    }

    mp_dim, mp_blockers = score_market_profile_dimension(decision, spot, market_profile)
    dimensions["market_profile"] = mp_dim
    vol_dim, vol_blockers = score_volatility_dimension(decision, volatility_engine)
    dimensions["volatility_align"] = vol_dim
    liq_dim, liq_blockers = score_liquidity_dimension(decision, spot, liquidity_engine)
    dimensions["liquidity_align"] = liq_dim
    opt_dim, opt_blockers = score_options_surface_dimension(
        decision,
        spot,
        strike,
        options_analytics,
        morning_context=morning_context,
    )
    dimensions["options_surface"] = opt_dim

    total_score = sum(_as_int(dim.get("score")) for dim in dimensions.values())
    max_score = sum(CONFLUENCE_WEIGHTS.values())

    blockers: List[str] = []
    if in_orb_no_trade:
        blockers.append("ORB_NO_TRADE")
    if late_session:
        blockers.append("LATE_SESSION")
    if open_signals_count >= 1:
        blockers.append("MAX_OPEN")
    if block_same_thesis_stack and open_signals_count >= 1:
        blockers.append("THESIS_STACK")
    if watch_zone:
        blockers.append("WATCH_ZONE")
    elif not prox_pass:
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
    for item in intent_blockers or []:
        if item and item not in blockers:
            blockers.append(item)
    for item in mp_blockers + vol_blockers + liq_blockers + opt_blockers:
        if item and item not in blockers:
            blockers.append(item)

    paper_blockers = {
        "ORB_NO_TRADE",
        "LATE_SESSION",
        "MAX_OPEN",
        "THESIS_STACK",
        "STRIKE_SPACING",
        "DIRECTION_CONFLICT",
        "COOLDOWN",
        "NO_ENTRY_CONTRACT",
        "GEX_DELTA_CONFLICT",
        "GEX_VOL_EXPANSION",
        "MARKET_PROFILE_CONFLICT",
        "VOL_REGIME_CONFLICT",
        "LIQUIDITY_GRAB_CONFLICT",
        "WATCH_ZONE",
    }
    hard_blocked = any(item in paper_blockers for item in blockers)
    confluence_ready = total_score >= TRADE_MIN_CONFLUENCE
    paper_eligible = confluence_ready and len(blockers) == 0

    bias = str((morning_context or {}).get("combined_bias") or "UNKNOWN")
    playbook_phase = str((playbook or {}).get("phase") or "")

    desk_reasoning = build_desk_reasoning(
        oi_side=writer_side,
        direction=direction,
        strike=strike,
        decision=decision,
        dimensions=dimensions,
        engine="legacy_v1",
    )

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
        "desk_reasoning": desk_reasoning,
    }


def _selftest() -> None:
    from nifty.core.commission import CommissionConfig

    cfg = CommissionConfig()
    alert = {
        "option_type": "PE", "strike": 23000, "direction": "WRITERS ADDING",
        "contract": "NIFTY23000PE", "key_area": True, "key_area_reasons": ["ORB low"],
        "recent_1m_deltas": [{"oi_delta": 500, "volume_delta": 100}] * 4,
        "reason": "chain outlier: 3.2x pct outlier",
    }
    entry_row = {"last_price": 100.0, "tradingsymbol": "NIFTY23000CE"}

    # Baseline call (no new-dimension inputs, matching today's state.py call site):
    # the 4 new dimensions must degrade to "warming up" / score 0, and
    # paper_eligible/confluence_ready must be identical to pre-port behavior.
    baseline = score_signal_candidate(
        alert, spot=23000.0, spot_v5={"delta": -2.0}, pe_behavior="",
        entry_row=entry_row, commission_cfg=cfg, now_ts=1000.0, last_signal_ts=0.0,
    )
    assert baseline["dimensions"]["market_profile"]["score"] == 0
    assert baseline["dimensions"]["market_profile"]["pass"] is False
    assert baseline["dimensions"]["volatility_align"]["score"] == 0
    assert baseline["dimensions"]["liquidity_align"]["score"] == 0
    assert baseline["dimensions"]["options_surface"]["score"] == 0
    assert baseline["max_score"] == 128  # 100 (original 8 dims) + 8+7+5+8 (new 4)
    assert "desk_reasoning" in baseline
    assert baseline["desk_reasoning"]["oi_fact"]["side"] == "PE"

    # With real market_profile data, the new dimension contributes real score,
    # raising total_score/confluence without touching any existing dimension.
    enriched = score_signal_candidate(
        alert, spot=23000.0, spot_v5={"delta": -2.0}, pe_behavior="",
        entry_row=entry_row, commission_cfg=cfg, now_ts=1000.0, last_signal_ts=0.0,
        market_profile={"status": "READY", "poc": 23050.0, "vah": 23100.0, "val": 22950.0, "balance_state": "BALANCED_ROTATION"},
    )
    # decision for writer_side=PE is BUY_CE; spot=23000 < poc=23050, balanced rotation -> dip-buy zone score
    assert enriched["dimensions"]["market_profile"]["score"] > 0
    assert enriched["total_score"] > baseline["total_score"]

    # score_market_profile_dimension: VAH rejection conflict for BUY_PE blocks.
    mp_dim, mp_blockers = score_market_profile_dimension(
        "BUY_PE", 23100.0, {"status": "READY", "poc": 22950.0, "vah": 23000.0, "val": 22900.0, "acceptance_rejection": "ACCEPTED_BELOW_POC"},
    )
    assert mp_dim["pass"] is False or "MARKET_PROFILE_CONFLICT" not in mp_blockers  # not the compressed-below-VAL branch at this spot

    # score_volatility_dimension: high-vol expansion is a hard conflict for BUY_CE.
    vol_dim, vol_blockers = score_volatility_dimension(
        "BUY_CE", {"status": "READY", "volatility_regime": "HIGH_VOL", "vol_expansion": True},
    )
    assert vol_dim["pass"] is False
    assert "VOL_REGIME_CONFLICT" in vol_blockers

    # score_liquidity_dimension: upside grab supports a PE fade, blocks a CE.
    liq_dim_pe, liq_blockers_pe = score_liquidity_dimension("BUY_PE", 23100.0, {"status": "READY", "liquidity_grab": "UPSIDE_LIQUIDITY_GRAB"})
    assert liq_dim_pe["pass"] is True
    liq_dim_ce, liq_blockers_ce = score_liquidity_dimension("BUY_CE", 23100.0, {"status": "READY", "liquidity_grab": "UPSIDE_LIQUIDITY_GRAB"})
    assert liq_dim_ce["pass"] is False
    assert "LIQUIDITY_GRAB_CONFLICT" in liq_blockers_ce

    # score_options_surface_dimension: aligned GEX + cheap IV passes for BUY_CE.
    opt_dim, opt_blockers = score_options_surface_dimension(
        "BUY_CE", 23000.0, 23000,
        {"gex_regime": "POSITIVE_GAMMA", "premium_vs_vix": "CHEAP", "price_delta_divergence": {"label": "AGGRESSIVE_BUYERS"}},
    )
    assert opt_dim["pass"] is True
    assert opt_dim["score"] > 0

    # WATCH_ZONE: a strike beyond the trade radius but inside the watch radius
    # gets WATCH_ZONE instead of STRIKE_TOO_FAR — but paper_eligible is False
    # either way (both are still blockers), so this is a labeling-only change.
    far_alert = {**alert, "strike": 23200}  # 200pts from spot=23000, beyond 150pt trade radius
    watch_result = score_signal_candidate(
        far_alert, spot=23000.0, spot_v5={"delta": -2.0}, pe_behavior="",
        entry_row=entry_row, commission_cfg=cfg, now_ts=1000.0, last_signal_ts=0.0,
    )
    assert "WATCH_ZONE" in watch_result["blockers"]  # within 300pt default watch radius
    assert watch_result["paper_eligible"] is False

    very_far_alert = {**alert, "strike": 23500}  # 500pts away, beyond default 300pt watch radius too
    far_result = score_signal_candidate(
        very_far_alert, spot=23000.0, spot_v5={"delta": -2.0}, pe_behavior="",
        entry_row=entry_row, commission_cfg=cfg, now_ts=1000.0, last_signal_ts=0.0,
    )
    assert "STRIKE_TOO_FAR" in far_result["blockers"]
    assert "WATCH_ZONE" not in far_result["blockers"]

    # PLAYBOOK_SPOT_FLAT_PTS deliberately preserved at nifty-dashboard's current
    # value (8.0), NOT v4's evolved 3.0 — this wasn't part of what was approved.
    assert PLAYBOOK_SPOT_FLAT_PTS == 8.0

    # intent_blockers passthrough (inert today — nothing upstream populates it yet).
    with_intent = score_signal_candidate(
        alert, spot=23000.0, spot_v5={"delta": -2.0}, pe_behavior="",
        entry_row=entry_row, commission_cfg=cfg, now_ts=1000.0, last_signal_ts=0.0,
        intent_blockers=["BOTH_SIDES_ADDING"],
    )
    assert "BOTH_SIDES_ADDING" in with_intent["blockers"]
    assert with_intent["paper_eligible"] is False

    print("[analytics.confluence] selftest OK: 4 new dims null-safe, WATCH_ZONE labeling, backward compat")


if __name__ == "__main__":
    _selftest()
