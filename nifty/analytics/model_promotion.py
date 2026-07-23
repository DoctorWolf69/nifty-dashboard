#!/usr/bin/env python3
"""Model promotion rules — shadow → production gate.

Ported verbatim from quant-desk-engine's nifty_model_promotion.py
(mentor-authored). No logic changed. Fully self-contained.

Not yet wired into the live pipeline.
Self-check: python -m nifty.analytics.model_promotion
"""

from __future__ import annotations

from typing import Any, Dict, List

PROMOTION_RULES = {
    "min_sessions": 20,
    "min_signals": 500,
    "max_brier_vs_production": -0.02,  # candidate must be lower (better)
    "min_precision_delta_pp": 2.0,
    "min_ev_delta_rupees": 50.0,
    "max_false_accept_delta_pp": -1.0,  # must not increase false accepts
    "max_drawdown_worsening_pct": 0.0,
}


def evaluate_promotion_candidate(
    *,
    governance_history: List[Dict[str, Any]],
    production_model: str = "legacy",
    candidate_model: str = "ev_v1",
) -> Dict[str, Any]:
    """Check if shadow model qualifies for promotion."""
    if len(governance_history) < PROMOTION_RULES["min_sessions"]:
        return {
            "eligible": False,
            "reason": f"Need {PROMOTION_RULES['min_sessions']} sessions, have {len(governance_history)}",
            "rules": PROMOTION_RULES,
        }

    total_signals = 0
    prod_brier: List[float] = []
    cand_brier: List[float] = []
    prod_precision: List[float] = []
    cand_precision: List[float] = []
    prod_false_accept: List[float] = []
    cand_false_accept: List[float] = []
    ev_improvements: List[float] = []

    for row in governance_history:
        metrics = row.get("metrics") or row.get("governance") or {}
        total_signals += int(metrics.get("sample_size") or 0)
        if metrics.get("brier_score") is not None:
            cand_brier.append(float(metrics["brier_score"]))
        legacy = metrics.get("legacy_gate") or {}
        ev_gate = metrics.get("ev_gate") or {}
        if legacy.get("precision") is not None:
            prod_precision.append(float(legacy["precision"]))
        if ev_gate.get("precision") is not None:
            cand_precision.append(float(ev_gate["precision"]))
        if legacy.get("false_positive") is not None and metrics.get("sample_size"):
            prod_false_accept.append(legacy["false_positive"] / max(metrics["sample_size"], 1) * 100)
        if ev_gate.get("false_positive") is not None and metrics.get("sample_size"):
            cand_false_accept.append(ev_gate["false_positive"] / max(metrics["sample_size"], 1) * 100)
        shadow = metrics.get("shadow_comparison") or {}
        if shadow.get("ev_improvement_rupees") is not None:
            ev_improvements.append(float(shadow["ev_improvement_rupees"]))

    checks: Dict[str, Any] = {
        "sessions": len(governance_history) >= PROMOTION_RULES["min_sessions"],
        "signals": total_signals >= PROMOTION_RULES["min_signals"],
        "brier": (
            (sum(cand_brier) / len(cand_brier) if cand_brier else 1.0)
            <= (sum(prod_brier) / len(prod_brier) if prod_brier else 1.0) + PROMOTION_RULES["max_brier_vs_production"]
        ),
        "precision": (
            (sum(cand_precision) / len(cand_precision) if cand_precision else 0)
            >= (sum(prod_precision) / len(prod_precision) if prod_precision else 0)
            + PROMOTION_RULES["min_precision_delta_pp"]
        ),
        "ev": (sum(ev_improvements) / len(ev_improvements) if ev_improvements else 0) >= PROMOTION_RULES["min_ev_delta_rupees"],
        "false_accept": (
            (sum(cand_false_accept) / len(cand_false_accept) if cand_false_accept else 100)
            <= (sum(prod_false_accept) / len(prod_false_accept) if prod_false_accept else 100)
            + PROMOTION_RULES["max_false_accept_delta_pp"]
        ),
    }
    eligible = all(checks.values())
    return {
        "eligible": eligible,
        "production_model": production_model,
        "candidate_model": candidate_model,
        "checks": checks,
        "rules": PROMOTION_RULES,
        "summary": {
            "sessions": len(governance_history),
            "total_signals": total_signals,
            "avg_ev_improvement_rupees": round(sum(ev_improvements) / len(ev_improvements), 2) if ev_improvements else 0,
        },
        "action": "PROMOTE" if eligible else "HOLD_SHADOW",
    }


def _selftest() -> None:
    too_few = evaluate_promotion_candidate(governance_history=[{"metrics": {"sample_size": 10}}])
    assert too_few["eligible"] is False and "Need 20 sessions" in too_few["reason"]

    good_history = [
        {
            "metrics": {
                "sample_size": 30,
                "brier_score": 0.18,
                "legacy_gate": {"precision": 60.0, "false_positive": 5},
                "ev_gate": {"precision": 68.0, "false_positive": 2},
                "shadow_comparison": {"ev_improvement_rupees": 200.0},
            }
        }
        for _ in range(20)
    ]
    result = evaluate_promotion_candidate(governance_history=good_history)
    assert result["eligible"] is True and result["action"] == "PROMOTE"
    assert result["summary"]["total_signals"] == 600

    print("[analytics.model_promotion] selftest OK: session floor, all-checks-pass promotion")


if __name__ == "__main__":
    _selftest()
