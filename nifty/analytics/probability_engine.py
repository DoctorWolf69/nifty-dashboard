#!/usr/bin/env python3
"""
Probability + Expected Value engine (shadow -> production path).

Ported faithfully from quant-desk-engine's nifty_probability_engine.py
(mentor-authored). No formula, threshold, or branch changed.

Replaces additive confluence scoring over time with:
  Opportunity x Direction x Confidence x EV x Risk filter

Philosophy: maximize expected return - do not imitate participant types.
Futures may hedge; they are not automatically directional.

One dependency resolved rather than revived: the original imported
evaluate_alert_velocity + opportunity_velocity_factors from
nifty_oi_velocity_engine (the context-normalized OI engine explicitly
reverted in this repo, commits 67e60ea..137efd2). evaluate_alert_velocity is
only ever invoked when a velocity_ctx is explicitly built and passed in -
nothing here does that, so that call path is genuinely dead in this repo and
is omitted. opportunity_velocity_factors is different: it is null-safe by
its own design (`p = profile or {}`) and returns (0.0, []) - a real,
designed no-op, the same degradation pattern oi_conviction.py already uses
for market_profile/liquidity_engine/volatility_engine. That one function is
inlined verbatim below rather than reviving the whole normalization module.

Added this session (additive only, faithful to the same v4 source):
CALIBRATION_PATH/SHADOW_CALIBRATION/load_shadow_calibration_doc/
reload_shadow_calibration/apply_shadow_calibration - the isotonic
probability-calibration cache that nifty.analytics.ev_shadow_trainer (also
ported this session) writes to config/ev_calibration_shadow.json.
apply_shadow_calibration is NOT called anywhere in this module (v4's own
evaluate_trade_opportunity-equivalent calls it at one line; wiring that in
here would be a live scoring-behavior change requiring the same sign-off
as the earlier confluence.py/futures.py updates, so it's deliberately left
unwired - a pure pass-through until both that wiring AND a trained
calibration file exist).

Not yet wired into the live pipeline.
Self-check: python -m nifty.analytics.probability_engine
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from nifty.paths import DATA_DIR, PROJECT_ROOT

# ---------------------------------------------------------------------------
# Conflict taxonomy (mentor's Conflict Classification architecture)
# ---------------------------------------------------------------------------

FATAL_CONFLICTS = frozenset(
    {
        "BOTH_SIDES_ADDING",
        "NEWS",
        "DATA_ERROR",
        "ORB_NO_TRADE",
        "ORB_RESTRICTION",
        "EXTREME_LIQUIDITY",
        "LATE_SESSION",
    }
)

THESIS_BREAKER_CONFLICTS = frozenset(
    {
        "WRITER_NOT_CONFIRMED",
        "PE_SPOT_NOT_CONFIRMED",
        "SPOT_NOT_WEAK",
        "SPOT_NOT_WEAK_FOR_PE",
        "MARKET_PROFILE_CONFLICT",
        "GEX_DELTA_CONFLICT",
        "GEX_VOL_EXPANSION",
        "CROSS_EXPIRY_CONFLICT",
        "DEALER_CONFLICT",
        "FUTURES_MACRO_CONFLICT",
        "STRIKE_INTENT_CONFLICT",
        "CHAIN_DIRECTION_CONFLICT",
        "LIQUIDITY_GRAB_CONFLICT",
        "VOL_REGIME_CONFLICT",
    }
)

RISK_MODIFIER_CONFLICTS = frozenset(
    {
        "WATCH_ZONE",
        "STRIKE_TOO_FAR",
        "STRIKE_SPACING",
        "COOLDOWN",
        "MAX_OPEN",
        "THESIS_STACK",
        "DIRECTION_CONFLICT",
        "COMMISSION_TOO_THIN",
    }
)

# Log-odds evidence weights for Bayesian direction updates (tunable from journal)
DEFAULT_EVIDENCE_LOG_ODDS: Dict[str, float] = {
    "oi_velocity_strong": 0.35,
    "oi_sustained": 0.25,
    "volume_expansion": 0.20,
    "spot_confirms": 0.45,
    "writer_confirms": 0.40,
    "chain_aligns": 0.30,
    "cross_expiry_aligns": 0.25,
    "dealer_aligns": 0.20,
    "market_profile_aligns": 0.15,
    "both_sides_adding": -0.55,
    "writer_not_confirmed": -0.50,
    "spot_not_confirmed": -0.45,
    "market_profile_conflict": -0.35,
    "gex_conflict": -0.30,
    "futures_hedge_not_directional": -0.15,
    "futures_directional_align": 0.20,
}

MIN_EV_RUPEES = 150.0
MIN_CONFIDENCE_PCT = 55.0
MIN_OPPORTUNITY_PCT = 40.0
MIN_DIRECTION_PROB_PCT = 58.0

USE_EV_MODEL_FOR_PAPER = False  # shadow until calibrated on journal history

WEIGHTS_PATH = DATA_DIR / "evidence_weights.json"
CALIBRATION_PATH = PROJECT_ROOT / "config" / "ev_calibration_shadow.json"
EVIDENCE_LOG_ODDS: Dict[str, float] = dict(DEFAULT_EVIDENCE_LOG_ODDS)
SHADOW_CALIBRATION: Dict[str, Any] = {}


def load_learned_weights_doc(path: Path = WEIGHTS_PATH) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def get_evidence_log_odds(market_state: Optional[str] = None) -> Dict[str, float]:
    """Merge hand-tuned defaults with journal-calibrated global + regime weights."""
    weights = dict(DEFAULT_EVIDENCE_LOG_ODDS)
    learned = load_learned_weights_doc()
    global_w = learned.get("global_weights") or {}
    for key, value in global_w.items():
        if key in weights:
            weights[key] = _as_float(value, weights[key])
    if market_state:
        regime_w = (learned.get("regime_weights") or {}).get(market_state) or {}
        for key, value in regime_w.items():
            if key in weights:
                weights[key] = _as_float(value, weights[key])
    return weights


def refresh_evidence_weights(market_state: Optional[str] = None) -> Dict[str, float]:
    """Reload learned weights into module cache (call after EOD calibration)."""
    global EVIDENCE_LOG_ODDS
    EVIDENCE_LOG_ODDS = get_evidence_log_odds(market_state)
    return EVIDENCE_LOG_ODDS


def load_shadow_calibration_doc(path: Path = CALIBRATION_PATH) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def reload_shadow_calibration(path: Path = CALIBRATION_PATH) -> Dict[str, Any]:
    """Reload isotonic calibration anchors for shadow probability/EV."""
    global SHADOW_CALIBRATION
    SHADOW_CALIBRATION = load_shadow_calibration_doc(path)
    return SHADOW_CALIBRATION


def apply_shadow_calibration(raw_probability_pct: float) -> float:
    """Map raw thesis probability (0-100) to calibrated probability (0-100).

    Not called anywhere in this module yet - evaluate_trade_opportunity's
    own thesis-probability computation is unchanged from before this port.
    Wiring this in would be a live scoring-behavior change requiring the
    same explicit sign-off as the earlier confluence.py/futures.py updates;
    additionally SHADOW_CALIBRATION stays {} (this function is a pass-through)
    until ev_shadow_trainer.train_ev_shadow_calibration is actually run, since
    no config/ev_calibration_shadow.json exists yet.
    """
    anchors = (SHADOW_CALIBRATION.get("fit") or {}).get("anchors") or []
    if not anchors:
        return raw_probability_pct
    from nifty.analytics.ev_shadow_trainer import apply_isotonic_calibration

    raw = _clamp(_as_float(raw_probability_pct) / 100.0, 0.01, 0.99)
    calibrated = apply_isotonic_calibration(raw, anchors)
    return round(calibrated * 100.0, 1)


reload_shadow_calibration()


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


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _prob_to_log_odds(p: float) -> float:
    p = _clamp(p, 0.01, 0.99)
    return math.log(p / (1.0 - p))


def _log_odds_to_prob(lo: float) -> float:
    return 1.0 / (1.0 + math.exp(-lo))


def opportunity_velocity_factors(profile: Optional[Dict[str, Any]]) -> Tuple[float, List[str]]:
    """Bonus points for the opportunity engine from a normalized OI profile.

    Inlined verbatim from nifty_oi_velocity_engine.py — see module docstring.
    profile is always None in this repo (no velocity_ctx is ever built), so
    this is always the documented no-op: bonus=0.0, factors=[].
    """
    p = profile or {}
    bonus = 0.0
    factors: List[str] = []
    score = _as_float(p.get("velocity_score"))
    if p.get("is_outlier"):
        bonus += 18
        factors.append(f"Norm OI velocity outlier (score {score:.0f})")
    sustained = _as_int(p.get("sustained_norm_minutes"))
    if p.get("is_sustained"):
        bonus += 15
        factors.append(f"Norm OI sustained {sustained}m")
    if _as_float(p.get("acceleration")) > 0.5:
        bonus += 6
        factors.append("OI acceleration")
    return bonus, factors


def classify_conflicts(blockers: List[str]) -> Dict[str, Any]:
    """Map raw blockers -> fatal / thesis_breaker / risk_modifier."""
    fatal: List[str] = []
    thesis: List[str] = []
    modifiers: List[str] = []
    other: List[str] = []
    for b in blockers:
        key = str(b or "").upper()
        if key in FATAL_CONFLICTS:
            fatal.append(key)
        elif key in THESIS_BREAKER_CONFLICTS:
            thesis.append(key)
        elif key in RISK_MODIFIER_CONFLICTS:
            modifiers.append(key)
        elif key:
            other.append(key)
    risk_level = "LOW"
    if fatal:
        risk_level = "REJECT"
    elif len(thesis) >= 2:
        risk_level = "HIGH"
    elif thesis:
        risk_level = "MEDIUM"
    elif modifiers:
        risk_level = "MEDIUM" if len(modifiers) >= 2 else "LOW"
    return {
        "fatal": fatal,
        "thesis_breakers": thesis,
        "risk_modifiers": modifiers,
        "other": other,
        "risk_level": risk_level,
    }


def classify_market_state(
    *,
    market_profile: Optional[Dict[str, Any]] = None,
    volatility_engine: Optional[Dict[str, Any]] = None,
    options_analytics: Optional[Dict[str, Any]] = None,
    spot_v5_delta: float = 0.0,
    chain_bias_label: str = "NEUTRAL",
) -> str:
    """Coarse regime for dynamic feature weighting."""
    mp = market_profile or {}
    ve = volatility_engine or {}
    oa = options_analytics or {}
    balance = str(mp.get("balance_state") or "")
    vol_reg = str(ve.get("volatility_regime") or "NORMAL")
    gex = str(oa.get("gex_regime") or "")
    expansion = bool(ve.get("vol_expansion"))
    compression = bool(ve.get("vol_compression"))

    if gex == "POSITIVE_GAMMA" and balance in {"BALANCED", "BALANCED_ROTATION"}:
        return "GAMMA_PINNING"
    if expansion:
        return "VOLATILITY_EXPANSION"
    if compression:
        return "VOLATILITY_COMPRESSION"
    if balance == "IMBALANCED_EXPANSION":
        return "TREND_UP" if spot_v5_delta > 0 else "TREND_DOWN"
    if abs(spot_v5_delta) >= 25:
        return "BREAKOUT"
    if chain_bias_label == "CHOP":
        return "RANGE"
    if spot_v5_delta > 8:
        return "TREND_UP"
    if spot_v5_delta < -8:
        return "TREND_DOWN"
    return "RANGE"


def estimate_futures_context(
    decision: str,
    *,
    futures_alignment: Optional[Dict[str, Any]] = None,
    futures_layer: Optional[Dict[str, Any]] = None,
    spot_v5_delta: float = 0.0,
    chain_bias_label: str = "NEUTRAL",
) -> Dict[str, Any]:
    """Futures are not simply bullish/bearish - estimate hedging vs directional intent."""
    fa = futures_alignment or {}
    fl = futures_layer or {}
    live = str(fa.get("live_fut_behavior") or fl.get("front_behavior") or "FLAT")
    macro = str(fa.get("macro_bias") or "NEUTRAL")

    hedging_signals = 0
    directional_signals = 0

    if live in {"OI_ADD_MIXED", "FLAT"}:
        hedging_signals += 2
    if macro in {"NEUTRAL", ""}:
        hedging_signals += 1
    if abs(spot_v5_delta) < 5 and live not in {"LONG_BUILD", "SHORT_BUILD"}:
        hedging_signals += 1

    if decision == "BUY_CE":
        if live in {"LONG_BUILD", "SHORT_COVER"} and spot_v5_delta > 0:
            directional_signals += 2
        if chain_bias_label == "PE_DOMINANT_CHAIN":
            directional_signals += 1
    elif decision == "BUY_PE":
        if live in {"SHORT_BUILD", "LONG_UNWIND"} and spot_v5_delta <= 0:
            directional_signals += 2
        if chain_bias_label == "CE_DOMINANT_CHAIN":
            directional_signals += 1

    total = max(1, hedging_signals + directional_signals)
    hedging_prob = hedging_signals / total
    directional_prob = directional_signals / total
    weight = 0.05 if hedging_prob >= 0.55 else (0.35 if directional_prob >= 0.5 else 0.15)

    return {
        "hedging_probability": round(hedging_prob, 3),
        "directional_probability": round(directional_prob, 3),
        "futures_weight": round(weight, 3),
        "live_behavior": live,
        "macro_bias": macro,
        "note": "Hedging dominant — futures weight reduced" if hedging_prob >= 0.55 else "Directional component usable",
    }


def estimate_opportunity_score(
    *,
    alert: Dict[str, Any],
    options_analytics: Optional[Dict[str, Any]] = None,
    volatility_engine: Optional[Dict[str, Any]] = None,
    liquidity_engine: Optional[Dict[str, Any]] = None,
    market_state: str = "RANGE",
    velocity_ctx: Optional[Dict[str, Any]] = None,  # accepted, unused — see module docstring
) -> Dict[str, Any]:
    """Engine 1 - is there enough movement for option premium expansion?"""
    oa = options_analytics or {}
    ve = volatility_engine or {}
    liq = liquidity_engine or {}
    oi_velocity = alert.get("oi_velocity")  # always None here — no normalized profile is ever attached

    score = 35.0
    factors: List[str] = []
    vel_bonus, vel_factors = opportunity_velocity_factors(oi_velocity)
    score += vel_bonus
    factors.extend(vel_factors)
    recent = alert.get("recent_1m_deltas") or []
    vol_conf = sum(1 for r in recent if _as_int(r.get("volume_delta")) > 0)
    if vol_conf >= 3:
        score += 12
        factors.append(f"Volume expansion {vol_conf}m")
    em = _as_float(oa.get("expected_move_pts"))
    if em >= 200:
        score += 10
        factors.append(f"EM {em:.0f}pts")
    if ve.get("vol_expansion"):
        score += 8
        factors.append("Vol expansion")
    if str(liq.get("liquidity_grab") or "NONE") != "NONE":
        score += 5
        factors.append("Liquidity imbalance")
    if market_state == "GAMMA_PINNING":
        score -= 15
        factors.append("Gamma pin — opportunity capped")
    elif market_state in {"VOLATILITY_EXPANSION", "BREAKOUT"}:
        score += 8
        factors.append(f"Regime {market_state}")

    return {
        "score": round(_clamp(score, 0, 100), 1),
        "factors": factors,
    }


def bayesian_direction_update(
    decision: str,
    evidence_keys: List[str],
    *,
    prior: float = 0.50,
    market_state: Optional[str] = None,
) -> Dict[str, Any]:
    """Engine 2 core - log-odds updates from independent evidence."""
    odds_table = get_evidence_log_odds(market_state)
    # For BUY_CE we track P(bullish move); for BUY_PE track P(bearish move)
    lo = _prob_to_log_odds(prior)
    trail: List[Dict[str, Any]] = [{"step": "prior", "probability": round(prior * 100, 1)}]
    for key in evidence_keys:
        delta = odds_table.get(key, 0.0)
        if abs(delta) < 1e-6:
            continue
        lo += delta
        p = _log_odds_to_prob(lo)
        trail.append({"evidence": key, "delta_log_odds": delta, "probability": round(p * 100, 1)})
    final_p = _log_odds_to_prob(lo)
    bull = final_p if decision == "BUY_CE" else 1.0 - final_p
    bear = 1.0 - bull
    return {
        "bull_probability": round(bull * 100, 1),
        "bear_probability": round(bear * 100, 1),
        "thesis_probability": round(final_p * 100, 1),
        "trail": trail,
    }


def build_direction_evidence(
    *,
    candidate: Dict[str, Any],
    intent: Dict[str, Any],
    chain_bias: Dict[str, Any],
    futures_ctx: Dict[str, Any],
) -> List[str]:
    """Translate scored dimensions + conflicts into Bayesian evidence keys."""
    keys: List[str] = []
    dims = candidate.get("dimensions") or {}
    blockers = [str(b).upper() for b in (candidate.get("blockers") or [])]
    intent_blockers = [str(b).upper() for b in (intent.get("blockers") or [])]
    all_blockers = set(blockers) | set(intent_blockers)

    if dims.get("oi_velocity", {}).get("pass"):
        keys.append("oi_velocity_strong")
    if dims.get("oi_sustained", {}).get("pass"):
        keys.append("oi_sustained")
    if dims.get("volume_confirm", {}).get("pass"):
        keys.append("volume_expansion")
    if dims.get("spot_confirm", {}).get("pass"):
        keys.append("spot_confirms")
    if dims.get("writer_price", {}).get("pass"):
        keys.append("writer_confirms")
    label = str(chain_bias.get("label") or "")
    decision = str(candidate.get("decision") or "")
    if (decision == "BUY_CE" and label == "PE_DOMINANT_CHAIN") or (
        decision == "BUY_PE" and label == "CE_DOMINANT_CHAIN"
    ):
        keys.append("chain_aligns")
    if dims.get("market_profile", {}).get("pass"):
        keys.append("market_profile_aligns")
    if dims.get("options_surface", {}).get("pass"):
        keys.append("dealer_aligns")

    if "BOTH_SIDES_ADDING" in all_blockers:
        keys.append("both_sides_adding")
    if "WRITER_NOT_CONFIRMED" in all_blockers:
        keys.append("writer_not_confirmed")
    if "PE_SPOT_NOT_CONFIRMED" in all_blockers or "SPOT_NOT_WEAK" in all_blockers:
        keys.append("spot_not_confirmed")
    if "MARKET_PROFILE_CONFLICT" in all_blockers:
        keys.append("market_profile_conflict")
    if "GEX_DELTA_CONFLICT" in all_blockers or "GEX_VOL_EXPANSION" in all_blockers:
        keys.append("gex_conflict")

    if futures_ctx.get("hedging_probability", 0) >= 0.55:
        keys.append("futures_hedge_not_directional")
    elif futures_ctx.get("directional_probability", 0) >= 0.5:
        keys.append("futures_directional_align")

    return keys


def estimate_confidence_score(
    *,
    intent: Dict[str, Any],
    direction_trail: List[Dict[str, Any]],
    data_quality_ok: bool = True,
) -> Dict[str, Any]:
    """Engine 3 - reliability of the directional estimate (not magnitude)."""
    score = 40.0
    factors: List[str] = []
    intent_label = str(intent.get("intent") or "NEUTRAL")
    if intent_label == "QUALIFIED":
        score += 25
        factors.append("Intent QUALIFIED")
    elif intent_label == "NEUTRAL":
        score += 10
    else:
        score -= 15
        factors.append(f"Intent {intent_label}")

    blocker_count = len(intent.get("blockers") or [])
    score -= blocker_count * 8

    if len(direction_trail) >= 4:
        score += 10
        factors.append("Multi-evidence path")
    if not data_quality_ok:
        score -= 20
        factors.append("Data quality degraded")

    return {"score": round(_clamp(score, 0, 100), 1), "factors": factors}


def estimate_expected_value(
    *,
    decision: str,
    entry_price: float,
    target_price: float,
    stop_price: float,
    lot_size: int,
    commission_rupees: float,
    thesis_probability: float,
    opportunity_score: float,
) -> Dict[str, Any]:
    """Engine 5 - EV after costs (simplified option premium model)."""
    if entry_price <= 0:
        return {"expected_value_rupees": 0.0, "reward_risk": 0.0, "positive": False}

    gross_win = max(0.0, (target_price - entry_price) * lot_size)
    gross_loss = max(0.0, (entry_price - stop_price) * lot_size)
    p = _clamp(thesis_probability / 100.0, 0.05, 0.95)
    ev = p * gross_win - (1.0 - p) * gross_loss - commission_rupees
    rr = gross_win / gross_loss if gross_loss > 0 else 0.0

    # Scale by opportunity — low opportunity reduces effective EV
    opp_factor = _clamp(opportunity_score / 100.0, 0.3, 1.0)
    adjusted_ev = ev * opp_factor

    return {
        "expected_move_premium": round(target_price - entry_price, 2),
        "expected_drawdown_premium": round(entry_price - stop_price, 2),
        "gross_win_rupees": round(gross_win, 2),
        "gross_loss_rupees": round(gross_loss, 2),
        "commission_rupees": round(commission_rupees, 2),
        "reward_risk": round(rr, 2),
        "expected_value_rupees": round(adjusted_ev, 2),
        "positive": adjusted_ev >= MIN_EV_RUPEES,
    }


def evaluate_trade_opportunity(
    candidate: Dict[str, Any],
    *,
    intent: Optional[Dict[str, Any]] = None,
    chain_bias: Optional[Dict[str, Any]] = None,
    futures_alignment: Optional[Dict[str, Any]] = None,
    futures_layer: Optional[Dict[str, Any]] = None,
    market_profile: Optional[Dict[str, Any]] = None,
    volatility_engine: Optional[Dict[str, Any]] = None,
    liquidity_engine: Optional[Dict[str, Any]] = None,
    options_analytics: Optional[Dict[str, Any]] = None,
    spot_v5_delta: float = 0.0,
    lot_size: int = 65,
    alert: Optional[Dict[str, Any]] = None,
    velocity_ctx: Optional[Dict[str, Any]] = None,  # accepted, unused — see module docstring
) -> Dict[str, Any]:
    """
    Full five-engine evaluation for one signal candidate.
    Runs in shadow mode alongside legacy confluence score.
    """
    intent = intent or candidate.get("intent_filter") or {}
    chain_bias = chain_bias or candidate.get("chain_bias") or {}
    alert = alert or candidate.get("source_alert") or {}
    decision = str(candidate.get("decision") or "")
    blockers = list(candidate.get("blockers") or [])

    market_state = classify_market_state(
        market_profile=market_profile,
        volatility_engine=volatility_engine,
        options_analytics=options_analytics,
        spot_v5_delta=spot_v5_delta,
        chain_bias_label=str(chain_bias.get("label") or ""),
    )
    conflict = classify_conflicts(blockers)
    futures_ctx = estimate_futures_context(
        decision,
        futures_alignment=futures_alignment or candidate.get("futures_alignment"),
        futures_layer=futures_layer,
        spot_v5_delta=spot_v5_delta,
        chain_bias_label=str(chain_bias.get("label") or ""),
    )
    opportunity = estimate_opportunity_score(
        alert=alert,
        options_analytics=options_analytics,
        volatility_engine=volatility_engine,
        liquidity_engine=liquidity_engine,
        market_state=market_state,
        velocity_ctx=velocity_ctx,
    )
    evidence = build_direction_evidence(
        candidate=candidate,
        intent=intent,
        chain_bias=chain_bias,
        futures_ctx=futures_ctx,
    )
    direction = bayesian_direction_update(decision, evidence, prior=0.50, market_state=market_state)
    confidence = estimate_confidence_score(
        intent=intent,
        direction_trail=direction.get("trail") or [],
    )

    entry = _as_float(candidate.get("entry_price"))
    target = _as_float(candidate.get("target_price"))
    stop = round(entry * 0.70, 2) if entry > 0 else 0.0
    comm = _as_float((candidate.get("commission_check") or {}).get("round_trip_rupees"))
    ev = estimate_expected_value(
        decision=decision,
        entry_price=entry,
        target_price=target,
        stop_price=stop,
        lot_size=lot_size,
        commission_rupees=comm,
        thesis_probability=_as_float(direction.get("thesis_probability"), 50.0),
        opportunity_score=_as_float(opportunity.get("score"), 50.0),
    )

    thesis_prob = _as_float(direction.get("thesis_probability"), 50.0)
    opp_score = _as_float(opportunity.get("score"), 0.0)
    conf_score = _as_float(confidence.get("score"), 0.0)
    risk_level = conflict.get("risk_level") or "LOW"

    questions = {
        "enough_opportunity": opp_score >= MIN_OPPORTUNITY_PCT,
        "direction_probability": thesis_prob >= MIN_DIRECTION_PROB_PCT,
        "confidence_adequate": conf_score >= MIN_CONFIDENCE_PCT,
        "positive_ev": bool(ev.get("positive")),
        "risk_acceptable": risk_level in {"LOW", "MEDIUM"},
    }
    trade_eligible = all(questions.values()) and risk_level != "REJECT"

    legacy_score = _as_int(candidate.get("total_score"))
    legacy_grade = str(candidate.get("grade") or "")

    return {
        "model": "probability_ev_v1",
        "market_state": market_state,
        "opportunity": opportunity,
        "direction": direction,
        "confidence": confidence,
        "risk": {
            "level": risk_level,
            "conflicts": conflict,
            "futures_context": futures_ctx,
        },
        "expected_value": ev,
        "five_questions": questions,
        "trade_eligible": trade_eligible,
        "use_for_paper": USE_EV_MODEL_FOR_PAPER,
        "legacy_comparison": {
            "total_score": legacy_score,
            "grade": legacy_grade,
            "paper_eligible_legacy": bool(candidate.get("paper_eligible")),
            "divergence": trade_eligible != bool(candidate.get("paper_eligible")),
        },
        "summary": (
            f"P(thesis)={thesis_prob:.0f}% · Conf={conf_score:.0f}% · "
            f"Opp={opp_score:.0f}% · EV=₹{ev.get('expected_value_rupees', 0):.0f} · Risk={risk_level}"
        ),
    }


def _selftest() -> None:
    assert opportunity_velocity_factors(None) == (0.0, [])  # the designed no-op

    odds = get_evidence_log_odds()
    assert odds["oi_velocity_strong"] == 0.35 and odds["both_sides_adding"] == -0.55

    conflict = classify_conflicts(["ORB_NO_TRADE", "STRIKE_SPACING"])
    assert conflict["risk_level"] == "REJECT" and conflict["fatal"] == ["ORB_NO_TRADE"]
    conflict2 = classify_conflicts(["COOLDOWN"])
    assert conflict2["risk_level"] == "LOW"

    state = classify_market_state(spot_v5_delta=30.0)
    assert state == "BREAKOUT"

    direction = bayesian_direction_update("BUY_CE", ["spot_confirms", "writer_confirms"], prior=0.50)
    assert direction["thesis_probability"] > 50.0  # both evidence keys are positive for CE

    candidate = {
        "decision": "BUY_CE", "entry_price": 100.0, "target_price": 150.0,
        "total_score": 78, "grade": "B", "paper_eligible": True,
        "dimensions": {"spot_confirm": {"pass": True}, "writer_price": {"pass": True}},
        "blockers": [], "commission_check": {"round_trip_rupees": 40.0},
    }
    result = evaluate_trade_opportunity(candidate, spot_v5_delta=10.0)
    assert result["model"] == "probability_ev_v1"
    assert "trade_eligible" in result and isinstance(result["trade_eligible"], bool)
    assert result["opportunity"]["score"] >= 0  # velocity bonus is 0 (no profile) but score still computed

    # No config/ev_calibration_shadow.json trained yet -> pass-through, never raises.
    assert load_shadow_calibration_doc(Path("/nonexistent/path.json")) == {}
    global SHADOW_CALIBRATION
    original_calibration = SHADOW_CALIBRATION
    try:
        SHADOW_CALIBRATION = {}
        assert apply_shadow_calibration(62.5) == 62.5
    finally:
        SHADOW_CALIBRATION = original_calibration

    print("[analytics.probability_engine] selftest OK: no-op velocity factors, conflicts, market state, five-engine eval, shadow calibration pass-through")


if __name__ == "__main__":
    _selftest()
