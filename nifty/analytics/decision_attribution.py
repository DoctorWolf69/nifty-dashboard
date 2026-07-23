#!/usr/bin/env python3
"""Decision attribution — ranked explanation for every accept/reject.

Ported faithfully from quant-desk-engine's nifty_decision_attribution.py
(mentor-authored). No logic changed. Only adaptation: imports
get_evidence_log_odds from nifty.analytics.probability_engine (this repo's
layout) instead of the standalone nifty_probability_engine module.

Not yet wired into the live pipeline.
Self-check: python -m nifty.analytics.decision_attribution
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from nifty.analytics.probability_engine import get_evidence_log_odds

ATTRIBUTION_LABELS: Dict[str, str] = {
    "oi_velocity_strong": "OI Velocity",
    "oi_sustained": "OI Sustained",
    "volume_expansion": "Volume Expansion",
    "spot_confirms": "Spot Acceptance",
    "writer_confirms": "Writer Confirmation",
    "chain_aligns": "Chain Alignment",
    "cross_expiry_aligns": "Cross Expiry",
    "dealer_aligns": "Dealer Position",
    "market_profile_aligns": "Market Profile",
    "both_sides_adding": "Both Sides Adding",
    "writer_not_confirmed": "Writer Missing",
    "spot_not_confirmed": "Spot Not Confirming",
    "market_profile_conflict": "Market Profile Conflict",
    "gex_conflict": "Gamma Conflict",
    "futures_hedge_not_directional": "Futures Hedge",
    "futures_directional_align": "Futures Directional",
}

DIMENSION_LABELS: Dict[str, str] = {
    "key_area": "Key Area",
    "spot_confirm": "Spot",
    "writer_price": "Writer Price",
    "volume_confirm": "Volume",
    "market_profile": "Market Profile",
    "volatility_align": "Volatility Regime",
    "liquidity_align": "Liquidity",
    "options_surface": "Options Surface",
    "commission": "Commission",
}


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _log_odds_to_prob(lo: float) -> float:
    return 1.0 / (1.0 + math.exp(-lo))


def _prob_to_log_odds(p: float) -> float:
    p = max(0.01, min(0.99, p))
    return math.log(p / (1.0 - p))


def _impact_pp(delta_log_odds: float) -> float:
    """Approximate probability-point impact of one log-odds delta."""
    return round((_log_odds_to_prob(delta_log_odds) - 0.5) * 100 * 2, 1)


def build_bayesian_attribution(
    ev_model: Dict[str, Any],
    *,
    market_state: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Ranked factors from Bayesian evidence trail."""
    trail = (ev_model.get("direction") or {}).get("trail") or []
    odds_table = get_evidence_log_odds(market_state)
    reasons: List[Dict[str, Any]] = []
    for step in trail[1:]:
        evidence = str(step.get("evidence") or "")
        if not evidence:
            continue
        delta = _as_float(step.get("delta_log_odds"), odds_table.get(evidence, 0.0))
        reasons.append(
            {
                "factor": ATTRIBUTION_LABELS.get(evidence, evidence.replace("_", " ").title()),
                "evidence_key": evidence,
                "impact_pp": _impact_pp(delta) if step.get("delta_log_odds") is None else round(delta * 18, 1),
                "direction": "for" if delta > 0 else "against",
                "probability_after": step.get("probability"),
            }
        )
    reasons.sort(key=lambda r: abs(r["impact_pp"]), reverse=True)
    return reasons


def build_dimension_attribution(candidate: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Confluence dimension pass/fail as secondary attribution."""
    dims = candidate.get("dimensions") or {}
    out: List[Dict[str, Any]] = []
    for key, label in DIMENSION_LABELS.items():
        dim = dims.get(key) or {}
        if not dim:
            continue
        score = _as_float(dim.get("score"))
        max_score = _as_float(dim.get("max"), 1.0)
        pct = round((score / max_score) * 100, 1) if max_score else 0.0
        out.append(
            {
                "factor": label,
                "impact_pp": pct if dim.get("pass") else -pct,
                "direction": "for" if dim.get("pass") else "against",
                "detail": dim.get("detail"),
            }
        )
    out.sort(key=lambda r: abs(r["impact_pp"]), reverse=True)
    return out


def build_blocker_attribution(candidate: Dict[str, Any], ev_model: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Fatal / thesis breaker attribution."""
    conflict = (ev_model.get("risk") or {}).get("conflicts") or {}
    out: List[Dict[str, Any]] = []
    for blocker in conflict.get("fatal") or []:
        out.append({"factor": blocker.replace("_", " "), "impact_pp": -25.0, "direction": "against", "tier": "fatal"})
    for blocker in conflict.get("thesis_breakers") or []:
        out.append({"factor": blocker.replace("_", " "), "impact_pp": -12.0, "direction": "against", "tier": "thesis"})
    for blocker in conflict.get("risk_modifiers") or []:
        out.append({"factor": blocker.replace("_", " "), "impact_pp": -4.0, "direction": "against", "tier": "modifier"})
    return out


def build_decision_attribution(
    candidate: Dict[str, Any],
    ev_model: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Full ranked attribution for dashboard and research DB.
    """
    market_state = ev_model.get("market_state")
    bayesian = build_bayesian_attribution(ev_model, market_state=market_state)
    blockers = build_blocker_attribution(candidate, ev_model)
    dimensions = build_dimension_attribution(candidate)[:5]

    reasons = bayesian[:6] + blockers[:4]
    reasons.sort(key=lambda r: abs(r["impact_pp"]), reverse=True)

    thesis_prob = _as_float((ev_model.get("direction") or {}).get("thesis_probability"), 50.0)
    ev_rupees = _as_float((ev_model.get("expected_value") or {}).get("expected_value_rupees"))
    trade_eligible = bool(ev_model.get("trade_eligible"))
    legacy_eligible = bool(candidate.get("paper_eligible"))

    return {
        "decision": "ACCEPTED" if trade_eligible else "REJECTED",
        "legacy_decision": "ACCEPTED" if legacy_eligible else "REJECTED",
        "reasons": reasons[:10],
        "bayesian_factors": bayesian,
        "blocker_factors": blockers,
        "dimension_factors": dimensions,
        "final_probability_pct": round(thesis_prob, 1),
        "confidence_pct": _as_float((ev_model.get("confidence") or {}).get("score")),
        "opportunity_pct": _as_float((ev_model.get("opportunity") or {}).get("score")),
        "expected_value_rupees": round(ev_rupees, 2),
        "risk_level": (ev_model.get("risk") or {}).get("level"),
        "summary": (
            f"{'ACCEPT' if trade_eligible else 'REJECT'} · P={thesis_prob:.0f}% · "
            f"EV=₹{ev_rupees:.0f} · top: "
            + (reasons[0]["factor"] if reasons else "no dominant factor")
        ),
    }


def _selftest() -> None:
    ev_model = {
        "market_state": "TREND_UP",
        "direction": {
            "thesis_probability": 68.0,
            "trail": [
                {"step": "prior", "probability": 50.0},
                {"evidence": "spot_confirms", "delta_log_odds": 0.45, "probability": 61.0},
                {"evidence": "writer_confirms", "delta_log_odds": 0.40, "probability": 68.0},
            ],
        },
        "confidence": {"score": 70.0},
        "opportunity": {"score": 55.0},
        "expected_value": {"expected_value_rupees": 2200.0},
        "risk": {"level": "LOW", "conflicts": {"fatal": [], "thesis_breakers": [], "risk_modifiers": ["COOLDOWN"]}},
        "trade_eligible": True,
    }
    candidate = {
        "paper_eligible": True,
        "dimensions": {
            "key_area": {"score": 12, "max": 12, "pass": True, "detail": "top OI wall"},
            "spot_confirm": {"score": 0, "max": 15, "pass": False, "detail": "not confirmed"},
        },
    }

    bayesian = build_bayesian_attribution(ev_model, market_state="TREND_UP")
    assert len(bayesian) == 2
    assert bayesian[0]["direction"] == "for"

    blockers = build_blocker_attribution(candidate, ev_model)
    assert len(blockers) == 1 and blockers[0]["tier"] == "modifier"

    dims = build_dimension_attribution(candidate)
    assert dims[0]["factor"] == "Key Area" and dims[0]["direction"] == "for"

    full = build_decision_attribution(candidate, ev_model)
    assert full["decision"] == "ACCEPTED" and full["legacy_decision"] == "ACCEPTED"
    assert full["final_probability_pct"] == 68.0
    assert "top:" in full["summary"]

    print("[analytics.decision_attribution] selftest OK: bayesian/blocker/dimension attribution, full build")


if __name__ == "__main__":
    _selftest()
