#!/usr/bin/env python3
"""Position sizing — Kelly-capped capital allocation.

Ported verbatim from quant-desk-engine's nifty_position_sizing.py (mentor-authored).
No logic changed; no external dependencies to adapt.

Research output only — nifty-dashboard is read-only against the broker and trades
paper positions at a fixed lot size; this does not place or size any real order.
Not yet wired into the live pipeline — see MIGRATION_PLAN.md / the porting todo list.
Self-check: python -m nifty.analytics.position_sizing
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def compute_position_size(
    *,
    probability_pct: float,
    expected_value_rupees: float,
    expected_drawdown_premium: float,
    entry_price: float,
    lot_size: int = 65,
    account_capital: float = 500_000.0,
    max_daily_risk_pct: float = 2.0,
    max_position_risk_pct: float = 1.0,
    current_exposure_rupees: float = 0.0,
    kelly_fraction_cap: float = 0.25,
) -> Dict[str, Any]:
    """Suggest lots from probability, EV, and risk budget."""
    if entry_price <= 0:
        return {"lots": 0, "capital_rupees": 0.0, "risk_pct": 0.0}

    p = max(0.05, min(0.95, probability_pct / 100.0))
    win_r = max(expected_value_rupees, 1.0)
    loss_r = max(expected_drawdown_premium * lot_size, 1.0)
    kelly = max(0.0, p - (1 - p) * (loss_r / win_r))
    kelly_capped = min(kelly, kelly_fraction_cap)

    risk_budget = account_capital * (max_daily_risk_pct / 100.0)
    remaining_budget = max(0.0, risk_budget - current_exposure_rupees)
    position_risk_budget = account_capital * (max_position_risk_pct / 100.0)

    notional_per_lot = entry_price * lot_size
    kelly_capital = account_capital * kelly_capped
    alloc_capital = min(kelly_capital, position_risk_budget, remaining_budget)
    lots = max(0, int(alloc_capital / notional_per_lot)) if notional_per_lot else 0
    capital = round(lots * notional_per_lot, 2)
    risk_pct = round((loss_r * lots / account_capital) * 100, 3) if account_capital else 0.0

    return {
        "suggested_lots": lots,
        "suggested_capital_rupees": capital,
        "risk_pct_of_account": risk_pct,
        "kelly_raw": round(kelly, 4),
        "kelly_capped": round(kelly_capped, 4),
        "max_daily_risk_pct": max_daily_risk_pct,
        "remaining_daily_budget_rupees": round(remaining_budget, 2),
        "note": "Research output — not wired to live execution.",
    }


def _selftest() -> None:
    r = compute_position_size(
        probability_pct=65.0, expected_value_rupees=3000.0, expected_drawdown_premium=30.0,
        entry_price=100.0, lot_size=65, account_capital=500_000.0,
    )
    assert r["suggested_lots"] >= 0
    assert 0.0 <= r["kelly_capped"] <= 0.25
    assert r["kelly_capped"] <= r["kelly_raw"] or r["kelly_raw"] < 0  # cap never exceeds raw
    assert r["note"] == "Research output — not wired to live execution."

    zero = compute_position_size(
        probability_pct=50.0, expected_value_rupees=1.0, expected_drawdown_premium=1.0, entry_price=0.0,
    )
    assert zero == {"lots": 0, "capital_rupees": 0.0, "risk_pct": 0.0}

    # a losing-edge trade (p low, loss >> win) should cap kelly at/near zero
    bad = compute_position_size(
        probability_pct=20.0, expected_value_rupees=100.0, expected_drawdown_premium=500.0, entry_price=100.0,
    )
    assert bad["kelly_raw"] == 0.0

    print("[analytics.position_sizing] selftest OK: kelly cap, zero-entry guard, losing-edge floor")


if __name__ == "__main__":
    _selftest()
