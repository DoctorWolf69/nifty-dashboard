#!/usr/bin/env python3
"""Premium expansion model — predict option return from move × IV × theta × greeks.

Ported verbatim from quant-desk-engine's nifty_premium_model.py (mentor-authored).
No logic changed; no external dependencies to adapt.

Not yet wired into the live pipeline — see MIGRATION_PLAN.md / the porting todo list.
Self-check: python -m nifty.analytics.premium_model
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def estimate_premium_expansion(
    *,
    decision: str,
    entry_price: float,
    spot: float,
    expected_move_pts: float,
    iv_rank: Optional[float] = None,
    iv_percentile: Optional[float] = None,
    gex_regime: str = "",
    days_to_expiry: float = 5.0,
    lot_size: int = 65,
) -> Dict[str, Any]:
    """
    Premium model (v0):
    Expected Premium Change ≈ underlying_delta_component × IV_factor × theta_decay × gamma_boost
    """
    if entry_price <= 0 or spot <= 0:
        return {"expected_premium_change": 0.0, "expected_return_pct": 0.0}

    move_pct = (expected_move_pts / spot) * 100.0 if spot else 0.0
    delta_proxy = 0.42
    direction_sign = 1.0 if decision == "BUY_CE" else -1.0
    underlying_component = move_pct * delta_proxy * direction_sign

    iv = _as_float(iv_rank, 50.0)
    iv_factor = 1.0 + (iv - 50.0) / 200.0  # high IV → larger premium swings

    theta_decay = max(0.55, 1.0 - (days_to_expiry / 30.0) * 0.35)
    gamma_boost = 1.12 if "NEGATIVE" in str(gex_regime).upper() else 1.0

    expected_return_pct = underlying_component * iv_factor * theta_decay * gamma_boost
    expected_premium_change = entry_price * (expected_return_pct / 100.0)

    return {
        "model": "premium_expansion_v0",
        "expected_move_pts": round(expected_move_pts, 1),
        "underlying_component_pct": round(underlying_component, 2),
        "iv_factor": round(iv_factor, 3),
        "theta_decay": round(theta_decay, 3),
        "gamma_boost": round(gamma_boost, 3),
        "expected_premium_change": round(expected_premium_change, 2),
        "expected_return_pct": round(expected_return_pct, 2),
        "expected_ce_return_pct": round(expected_return_pct if decision == "BUY_CE" else -expected_return_pct * 0.6, 2),
        "expected_pe_return_pct": round(expected_return_pct if decision == "BUY_PE" else -expected_return_pct * 0.6, 2),
        "expected_premium_rupees": round(expected_premium_change * lot_size, 2),
    }


def _selftest() -> None:
    r = estimate_premium_expansion(
        decision="BUY_CE", entry_price=100.0, spot=23000.0, expected_move_pts=150.0,
        iv_rank=70.0, days_to_expiry=3.0,
    )
    assert r["model"] == "premium_expansion_v0"
    assert r["expected_move_pts"] == 150.0
    # move_pct = 150/23000*100 = 0.652%; underlying = 0.652*0.42*1 = 0.274
    assert abs(r["underlying_component_pct"] - 0.27) < 0.01
    assert r["iv_factor"] == round(1.0 + (70.0 - 50.0) / 200.0, 3)  # 1.1
    assert r["expected_ce_return_pct"] == r["expected_return_pct"]
    assert r["expected_pe_return_pct"] == round(-r["expected_return_pct"] * 0.6, 2)

    zero = estimate_premium_expansion(decision="BUY_CE", entry_price=0.0, spot=23000.0, expected_move_pts=100.0)
    assert zero == {"expected_premium_change": 0.0, "expected_return_pct": 0.0}

    print("[analytics.premium_model] selftest OK: expected-move scaling, IV factor, zero-entry guard")


if __name__ == "__main__":
    _selftest()
