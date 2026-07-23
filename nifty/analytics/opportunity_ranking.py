#!/usr/bin/env python3
"""Opportunity ranking — sort every candidate by risk-adjusted expected value.

Ported verbatim from quant-desk-engine's nifty_opportunity_ranking.py
(mentor-authored). No logic changed.

Expects each candidate to carry an ev_model dict shaped like
nifty_probability_engine.py's output (expected_value/direction/confidence/
opportunity/risk) — that engine is not yet ported (see the porting todo
list), so this has no live producer of real input yet. Self-contained
otherwise; no cross-module dependency issues.

Not yet wired into the live pipeline.
Self-check: python -m nifty.analytics.opportunity_ranking
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _risk_penalty(risk_level: str) -> float:
    return {"LOW": 1.0, "MEDIUM": 0.75, "HIGH": 0.45, "REJECT": 0.0}.get(str(risk_level or "").upper(), 0.5)


def compute_rank_score(candidate: Dict[str, Any], ev_model: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Composite ranking score for capital allocation."""
    ev_model = ev_model or candidate.get("ev_model") or {}
    ev = ev_model.get("expected_value") or {}
    direction = ev_model.get("direction") or {}
    confidence = ev_model.get("confidence") or {}
    opportunity = ev_model.get("opportunity") or {}
    risk_level = (ev_model.get("risk") or {}).get("level") or "MEDIUM"

    ev_rupees = _as_float(ev.get("expected_value_rupees"))
    thesis_p = _as_float(direction.get("thesis_probability"), 50.0) / 100.0
    conf = _as_float(confidence.get("score"), 50.0) / 100.0
    opp = _as_float(opportunity.get("score"), 50.0) / 100.0
    drawdown = max(_as_float(ev.get("expected_drawdown_premium"), 1.0), 0.01)
    reward_risk = _as_float(ev.get("reward_risk"), 1.0)

    capital_efficiency = ev_rupees / drawdown if drawdown else ev_rupees
    risk_adjusted = ev_rupees * thesis_p * conf * _risk_penalty(risk_level)
    composite = risk_adjusted * (0.5 + 0.5 * opp) * min(reward_risk, 3.0) / 3.0

    return {
        "composite_score": round(composite, 2),
        "expected_value_rupees": round(ev_rupees, 2),
        "thesis_probability_pct": round(thesis_p * 100, 1),
        "confidence_pct": round(conf * 100, 1),
        "opportunity_pct": round(opp * 100, 1),
        "capital_efficiency": round(capital_efficiency, 2),
        "risk_adjusted_return": round(risk_adjusted, 2),
        "reward_risk": round(reward_risk, 2),
        "expected_drawdown_premium": round(drawdown, 2),
        "risk_level": risk_level,
        "trade_eligible": bool(ev_model.get("trade_eligible")),
    }


def rank_opportunities(
    candidates: List[Dict[str, Any]],
    *,
    top_n: int = 10,
) -> Dict[str, Any]:
    """Rank all candidates descending by composite score."""
    ranked: List[Dict[str, Any]] = []
    for cand in candidates:
        ranking = compute_rank_score(cand)
        ranked.append(
            {
                "signal_key": cand.get("signal_key"),
                "evaluated_at": cand.get("evaluated_at"),
                "decision": cand.get("decision"),
                "strike": cand.get("strike"),
                "grade": cand.get("grade"),
                "legacy_score": cand.get("total_score"),
                "ranking": ranking,
                "attribution_summary": (cand.get("decision_attribution") or {}).get("summary"),
            }
        )
    ranked.sort(key=lambda r: r["ranking"]["composite_score"], reverse=True)
    top = ranked[:top_n]
    worst = ranked[-3:] if len(ranked) >= 3 else []
    best_trade = top[0] if top else None
    return {
        "total_candidates": len(ranked),
        "top_opportunities": top,
        "best_trade": best_trade,
        "second_best": top[1] if len(top) > 1 else None,
        "third_best": top[2] if len(top) > 2 else None,
        "worst_trades": list(reversed(worst)),
        "ranked_all": ranked,
    }


def _selftest() -> None:
    assert _risk_penalty("LOW") == 1.0
    assert _risk_penalty("REJECT") == 0.0
    assert _risk_penalty("unknown") == 0.5

    strong_ev = {
        "expected_value": {"expected_value_rupees": 3000.0, "expected_drawdown_premium": 20.0, "reward_risk": 2.0},
        "direction": {"thesis_probability": 70.0},
        "confidence": {"score": 80.0},
        "opportunity": {"score": 60.0},
        "risk": {"level": "LOW"},
        "trade_eligible": True,
    }
    weak_ev = {
        "expected_value": {"expected_value_rupees": 500.0, "expected_drawdown_premium": 40.0, "reward_risk": 1.0},
        "direction": {"thesis_probability": 40.0},
        "confidence": {"score": 30.0},
        "opportunity": {"score": 20.0},
        "risk": {"level": "HIGH"},
        "trade_eligible": False,
    }
    r_strong = compute_rank_score({}, strong_ev)
    r_weak = compute_rank_score({}, weak_ev)
    assert r_strong["composite_score"] > r_weak["composite_score"]
    assert r_strong["trade_eligible"] is True and r_weak["trade_eligible"] is False

    candidates = [
        {"signal_key": "A", "decision": "BUY_CE", "ev_model": strong_ev},
        {"signal_key": "B", "decision": "BUY_PE", "ev_model": weak_ev},
    ]
    result = rank_opportunities(candidates)
    assert result["total_candidates"] == 2
    assert result["best_trade"]["signal_key"] == "A"
    assert result["ranked_all"][0]["ranking"]["composite_score"] >= result["ranked_all"][1]["ranking"]["composite_score"]

    print("[analytics.opportunity_ranking] selftest OK: risk penalty, composite score ordering, ranking")


if __name__ == "__main__":
    _selftest()
