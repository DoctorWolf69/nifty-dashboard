#!/usr/bin/env python3
"""Ensemble specialist models — meta-combination (shadow research).

Ported faithfully from quant-desk-engine's nifty_ensemble_models.py
(mentor-authored). No logic changed. Only adaptation: imports
estimate_premium_expansion from nifty.analytics.premium_model (ported)
instead of the standalone nifty_premium_model module.

Eight synthetic "specialist" views blended into one meta-model score —
explicitly research/shadow only (see run_ensemble_models's own
"research only" label; nothing here feeds a live decision).

Not yet wired into the live pipeline.
Self-check: python -m nifty.analytics.ensemble_models
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from nifty.analytics.premium_model import estimate_premium_expansion


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _specialist(
    name: str,
    probability: float,
    confidence: float,
    ev_rupees: float,
    *,
    factors: Optional[List[str]] = None,
) -> Dict[str, Any]:
    return {
        "model": name,
        "probability": round(probability, 1),
        "confidence": round(confidence, 1),
        "expected_value_rupees": round(ev_rupees, 2),
        "trade_decision": ev_rupees > 0 and probability >= 55,
        "factors": factors or [],
    }


def run_ensemble_models(
    candidate: Dict[str, Any],
    ev_model: Dict[str, Any],
    *,
    options_analytics: Optional[Dict[str, Any]] = None,
    lot_size: int = 65,
) -> Dict[str, Any]:
    """Specialist outputs + meta-model blend (research only)."""
    oa = options_analytics or (candidate.get("engine_layers") or {}).get("options_analytics") or {}
    dims = candidate.get("dimensions") or {}
    thesis = _as_float((ev_model.get("direction") or {}).get("thesis_probability"), 50.0)
    base_ev = _as_float((ev_model.get("expected_value") or {}).get("expected_value_rupees"))

    oi_p = thesis + (8 if dims.get("oi_velocity", {}).get("pass") else -5)
    greeks_p = thesis + (6 if dims.get("options_surface", {}).get("pass") else -4)
    liq_p = thesis + (5 if dims.get("liquidity_align", {}).get("pass") else -3)
    mom_p = thesis + (7 if dims.get("spot_confirm", {}).get("pass") else -6)
    dealer_p = thesis + (5 if oa.get("dealer_positioning") else 0)
    struct_p = thesis + (4 if dims.get("market_profile", {}).get("pass") else -4)
    vol_p = thesis + (3 if dims.get("volatility_align", {}).get("pass") else -2)
    cross_p = thesis

    specialists = [
        _specialist("oi_model", oi_p, 62, base_ev * 0.9, factors=["oi_velocity", "oi_sustained"]),
        _specialist("greeks_model", greeks_p, 58, base_ev * 0.85, factors=["gex", "dealer_delta"]),
        _specialist("liquidity_model", liq_p, 55, base_ev * 0.7, factors=["spread", "grab"]),
        _specialist("momentum_model", mom_p, 60, base_ev * 1.0, factors=["spot", "volume"]),
        _specialist("dealer_model", dealer_p, 57, base_ev * 0.8, factors=["dealer_positioning"]),
        _specialist("market_structure_model", struct_p, 54, base_ev * 0.75, factors=["market_profile"]),
        _specialist("volatility_model", vol_p, 56, base_ev * 0.65, factors=["iv_rank", "vol_regime"]),
        _specialist("cross_expiry_model", cross_p, 50, base_ev * 0.6, factors=["cross_expiry"]),
    ]

    weights = [0.16, 0.14, 0.10, 0.16, 0.12, 0.10, 0.12, 0.10]
    meta_prob = sum(s["probability"] * w for s, w in zip(specialists, weights))
    meta_conf = sum(s["confidence"] * w for s, w in zip(specialists, weights))
    meta_ev = sum(s["expected_value_rupees"] * w for s, w in zip(specialists, weights))

    premium = estimate_premium_expansion(
        decision=str(candidate.get("decision") or ""),
        entry_price=_as_float(candidate.get("entry_price")),
        spot=_as_float(candidate.get("spot")),
        expected_move_pts=_as_float(oa.get("expected_move_pts"), 200),
        iv_rank=_as_float(oa.get("iv_rank"), None) if oa.get("iv_rank") is not None else None,
        gex_regime=str(oa.get("gex_regime") or ""),
        lot_size=lot_size,
    )

    return {
        "ensemble_version": "specialists_v0",
        "specialists": specialists,
        "meta_model": _specialist("ensemble_meta", meta_prob, meta_conf, meta_ev),
        "premium_model": premium,
        "trade_decision": meta_ev > 0 and meta_prob >= 58 and meta_conf >= 52,
    }


def _selftest() -> None:
    candidate = {
        "decision": "BUY_CE", "entry_price": 100.0, "spot": 23000.0,
        "dimensions": {
            "oi_velocity": {"pass": True}, "options_surface": {"pass": True},
            "liquidity_align": {"pass": False}, "spot_confirm": {"pass": True},
            "market_profile": {"pass": False}, "volatility_align": {"pass": True},
        },
    }
    ev_model = {
        "direction": {"thesis_probability": 65.0},
        "expected_value": {"expected_value_rupees": 2000.0},
    }
    oa = {"expected_move_pts": 180.0, "iv_rank": 55.0, "gex_regime": "NEGATIVE_GAMMA", "dealer_positioning": "SHORT_GAMMA"}

    result = run_ensemble_models(candidate, ev_model, options_analytics=oa)
    assert len(result["specialists"]) == 8
    assert result["ensemble_version"] == "specialists_v0"
    assert "meta_model" in result and result["meta_model"]["model"] == "ensemble_meta"
    assert "premium_model" in result and result["premium_model"]["model"] == "premium_expansion_v0"
    assert isinstance(result["trade_decision"], bool)

    # oi_model should score above thesis (dim passed, +8); liquidity_model below (dim failed, -3).
    oi_row = next(s for s in result["specialists"] if s["model"] == "oi_model")
    liq_row = next(s for s in result["specialists"] if s["model"] == "liquidity_model")
    assert oi_row["probability"] > 65.0
    assert liq_row["probability"] < 65.0

    print("[analytics.ensemble_models] selftest OK: 8 specialists, meta blend, premium model integration")


if __name__ == "__main__":
    _selftest()
