#!/usr/bin/env python3
"""Live portfolio payoff curves for the paper trade book (open legs only).

Ported faithfully from quant-desk-engine v4/ATLAS's payoff_engine.py
(mentor-authored). No logic changed. Only adaptation: imports
CommissionConfig/estimate_leg_cost/gross_pnl_rupees/net_pnl_rupees from
nifty.core.commission (already updated this session with the decision-aware
gross_pnl_rupees/net_pnl_rupees this file needs) instead of the standalone
nifty_commission module.

Builds an expiry-payoff curve (paper premium and, where known, actual Kite
fill premium) plus a live mark-to-market curve using a per-strike delta
(from the options chain when available, a rough distance-based fallback
otherwise) to project how the open book's P&L would move with spot,
without waiting for expiry.

Not yet wired into the live pipeline.
Self-check: python -m nifty.analytics.payoff_engine
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from nifty.core.commission import CommissionConfig, estimate_leg_cost, gross_pnl_rupees, net_pnl_rupees

DEFAULT_FIXED_RANGE_PTS = 500.0
CURVE_POINTS = 121


def _leg_net_pnl(
    entry_premium: float,
    mark_or_intrinsic: float,
    qty: int,
    decision: str,
    cfg: CommissionConfig,
) -> float:
    """Net P&L for one long leg; handles zero exit premium without round-trip edge cases."""
    if mark_or_intrinsic <= 0:
        gross = gross_pnl_rupees(entry_premium, 0.0, qty, decision)
        entry_cost = estimate_leg_cost(entry_premium, qty, "BUY", cfg)["total"]
        return round(gross - entry_cost, 2)
    pnl = net_pnl_rupees(entry_premium, mark_or_intrinsic, qty, cfg, decision=decision)
    return float(pnl["net_rupees"])


def _leg_side(row: Dict[str, Any]) -> str:
    decision = str(row.get("decision") or "").upper()
    if "PE" in decision:
        return "PE"
    if "CE" in decision:
        return "CE"
    side = str(row.get("entry_side") or "").upper()
    return side if side in {"CE", "PE"} else "CE"


def _intrinsic(side: str, strike: float, spot: float) -> float:
    if side == "CE":
        return max(0.0, spot - strike)
    return max(0.0, strike - spot)


def build_greek_lookup(options_analytics: Optional[Dict[str, Any]]) -> Dict[Tuple[int, str], float]:
    lookup: Dict[Tuple[int, str], float] = {}
    for row in (options_analytics or {}).get("chain_rows") or []:
        try:
            strike = int(row.get("strike") or 0)
        except (TypeError, ValueError):
            continue
        if strike <= 0:
            continue
        ce_delta = row.get("ce_delta")
        pe_delta = row.get("pe_delta")
        if ce_delta is not None:
            lookup[(strike, "CE")] = float(ce_delta)
        if pe_delta is not None:
            lookup[(strike, "PE")] = float(pe_delta)
    return lookup


def _leg_delta(side: str, strike: int, spot: float, greek_lookup: Dict[Tuple[int, str], float]) -> float:
    key = (strike, side)
    if key in greek_lookup:
        return float(greek_lookup[key])
    # Rough fallback when chain greeks are unavailable
    if spot <= 0:
        return 0.45 if side == "CE" else -0.45
    dist = abs(spot - strike)
    mag = max(0.08, 0.55 - dist / max(spot * 0.02, 80.0))
    return mag if side == "CE" else -mag


def classify_structure(legs: Sequence[Dict[str, Any]]) -> str:
    if not legs:
        return "Flat"
    if len(legs) == 1:
        leg = legs[0]
        side = leg["side"]
        stacks = int(leg.get("lots") or 1)
        suffix = f" ×{stacks}" if stacks > 1 else ""
        return f"Long {side}{suffix}"
    ce = [l for l in legs if l["side"] == "CE"]
    pe = [l for l in legs if l["side"] == "PE"]
    if len(legs) == 2 and len(ce) == 1 and len(pe) == 1:
        if int(ce[0]["strike"]) == int(pe[0]["strike"]):
            return "Long straddle"
        return "Long strangle"
    return f"Custom {len(legs)}-leg"


def _find_breakevens(spots: Sequence[float], pnls: Sequence[float]) -> List[float]:
    out: List[float] = []
    for idx in range(1, len(spots)):
        y0 = float(pnls[idx - 1])
        y1 = float(pnls[idx])
        if y0 == 0:
            out.append(round(float(spots[idx - 1]), 1))
        if y0 * y1 < 0:
            x0, x1 = float(spots[idx - 1]), float(spots[idx])
            # Linear interpolation for zero crossing
            be = x0 + (0 - y0) * (x1 - x0) / (y1 - y0)
            out.append(round(be, 1))
    if pnls and float(pnls[-1]) == 0:
        out.append(round(float(spots[-1]), 1))
    # Dedupe near-identical
    deduped: List[float] = []
    for val in out:
        if not deduped or abs(val - deduped[-1]) > 0.5:
            deduped.append(val)
    return deduped[:4]


def _spot_range(
    spot: float,
    legs: Sequence[Dict[str, Any]],
    *,
    expected_move_pts: float,
    fixed_range_pts: float,
    use_expected_move: bool,
) -> Tuple[float, float]:
    if spot <= 0:
        return (0.0, 0.0)
    if use_expected_move and expected_move_pts > 0:
        pad = float(expected_move_pts)
    else:
        pad = float(fixed_range_pts)
        strikes = [float(l.get("strike") or 0) for l in legs if l.get("strike")]
        if strikes:
            wing = max(abs(spot - min(strikes)), abs(max(strikes) - spot))
            pad = max(pad, wing + 50.0)
    return round(spot - pad, 2), round(spot + pad, 2)


def build_portfolio_payoff(
    open_rows: Sequence[Dict[str, Any]],
    *,
    spot: float,
    expected_move_pts: float = 0.0,
    options_analytics: Optional[Dict[str, Any]] = None,
    commission_cfg: Optional[CommissionConfig] = None,
    fixed_range_pts: float = DEFAULT_FIXED_RANGE_PTS,
    use_expected_move: bool = True,
) -> Dict[str, Any]:
    """Build payoff payload for `/api/paper-trade-book` (OPEN legs only)."""
    cfg = commission_cfg or CommissionConfig.from_env()
    greek_lookup = build_greek_lookup(options_analytics)
    em = float(expected_move_pts or (options_analytics or {}).get("expected_move_pts") or 0.0)

    legs: List[Dict[str, Any]] = []
    portfolio_mtm = 0.0
    for row in open_rows:
        if str(row.get("status") or "") != "OPEN":
            continue
        strike = int(row.get("strike") or 0)
        side = _leg_side(row)
        qty = int(row.get("quantity") or row.get("lot_size") or cfg.lot_size)
        paper_premium = float(row.get("paper_entry") or row.get("entry_price") or 0)
        kite_premium = float(row.get("kite_fill") or 0) or None
        mark = float(row.get("mark_price") or row.get("current_price") or paper_premium)
        decision = str(row.get("decision") or f"BUY_{side}")
        delta = _leg_delta(side, strike, spot, greek_lookup)
        mtm = float(row.get("pnl_net_rupees") or row.get("net_pnl") or 0)
        portfolio_mtm += mtm
        gross_at_mark = (mark - paper_premium) * qty if paper_premium > 0 else 0.0
        charge_rupees = round(gross_at_mark - mtm, 2)
        legs.append(
            {
                "id": row.get("id"),
                "engine": str(row.get("engine") or "L1").upper(),
                "side": side,
                "strike": strike,
                "qty": qty,
                "lots": int(row.get("lots") or max(1, qty // cfg.lot_size)),
                "paper_premium": round(paper_premium, 2) if paper_premium > 0 else None,
                "kite_premium": round(kite_premium, 2) if kite_premium else None,
                "mark": round(mark, 2) if mark > 0 else None,
                "delta": round(delta, 4),
                "decision": decision,
                "mtm_rupees": round(mtm, 2),
                "charge_rupees": charge_rupees,
                "entry_contract": row.get("entry_contract"),
            }
        )

    x_min, x_max = _spot_range(
        spot,
        legs,
        expected_move_pts=em,
        fixed_range_pts=fixed_range_pts,
        use_expected_move=use_expected_move,
    )
    if x_max <= x_min:
        x_min, x_max = spot - fixed_range_pts, spot + fixed_range_pts

    spots = [
        round(x_min + (x_max - x_min) * i / (CURVE_POINTS - 1), 2)
        for i in range(CURVE_POINTS)
    ]

    def _curve_for_premium(*, use_kite: bool) -> List[float]:
        totals: List[float] = []
        for trial_spot in spots:
            net_sum = 0.0
            for leg in legs:
                paper = float(leg.get("paper_premium") or 0)
                kite = float(leg.get("kite_premium") or 0)
                premium = kite if (use_kite and kite > 0) else paper
                if premium <= 0:
                    continue
                intrinsic = _intrinsic(leg["side"], float(leg["strike"]), trial_spot)
                net_sum += _leg_net_pnl(
                    premium,
                    intrinsic,
                    int(leg["qty"]),
                    leg["decision"],
                    cfg,
                )
            totals.append(round(net_sum, 2))
        return totals

    def _mtm_curve() -> List[float]:
        totals: List[float] = []
        for trial_spot in spots:
            net_sum = 0.0
            for leg in legs:
                paper = float(leg.get("paper_premium") or 0)
                kite = float(leg.get("kite_premium") or 0)
                premium = kite if kite > 0 else paper
                mark = float(leg.get("mark") or premium)
                if premium <= 0:
                    continue
                shifted_mark = mark + float(leg.get("delta") or 0) * (trial_spot - spot)
                shifted_mark = max(0.0, shifted_mark)
                net_sum += _leg_net_pnl(
                    premium,
                    shifted_mark,
                    int(leg["qty"]),
                    leg["decision"],
                    cfg,
                )
            totals.append(round(net_sum, 2))
        return totals

    expiry_paper = _curve_for_premium(use_kite=False)
    expiry_kite = _curve_for_premium(use_kite=True)
    mtm = _mtm_curve()
    breakevens = _find_breakevens(spots, expiry_paper)

    max_loss = min(expiry_paper) if expiry_paper else 0.0
    max_profit = max(expiry_paper) if expiry_paper else 0.0

    return {
        "spot": round(spot, 2) if spot > 0 else None,
        "expected_move_pts": round(em, 2) if em > 0 else None,
        "fixed_range_pts": fixed_range_pts,
        "x_min": x_min,
        "x_max": x_max,
        "portfolio_mtm_rupees": round(portfolio_mtm, 2),
        "structure_label": classify_structure(legs),
        "open_legs": len(legs),
        "legs": legs,
        "spots": spots,
        "expiry_paper": expiry_paper,
        "expiry_kite": expiry_kite,
        "mtm_curve": mtm,
        "breakevens": breakevens,
        "max_loss_expiry_rupees": round(max_loss, 2),
        "max_profit_expiry_rupees": round(max_profit, 2),
        "current": {
            "spot": round(spot, 2) if spot > 0 else None,
            "mtm_rupees": round(portfolio_mtm, 2),
        },
    }


def _selftest() -> None:
    cfg = CommissionConfig()

    lookup = build_greek_lookup({"chain_rows": [{"strike": 23000, "ce_delta": 0.55, "pe_delta": -0.45}]})
    assert lookup[(23000, "CE")] == 0.55
    assert lookup[(23000, "PE")] == -0.45

    assert _intrinsic("CE", 23000, 23100) == 100.0
    assert _intrinsic("PE", 23000, 22900) == 100.0
    assert _intrinsic("CE", 23000, 22900) == 0.0

    single_leg = [{"side": "CE", "strike": 23000, "lots": 1}]
    assert classify_structure(single_leg) == "Long CE"
    assert classify_structure([]) == "Flat"
    straddle = [{"side": "CE", "strike": 23000}, {"side": "PE", "strike": 23000}]
    assert classify_structure(straddle) == "Long straddle"
    strangle = [{"side": "CE", "strike": 23100}, {"side": "PE", "strike": 22900}]
    assert classify_structure(strangle) == "Long strangle"

    bes = _find_breakevens([100, 110, 120], [-50, 0, 50])
    assert 110.0 in bes

    open_rows = [
        {
            "id": 1, "status": "OPEN", "decision": "BUY_CE", "strike": 23000,
            "quantity": 65, "entry_price": 100.0, "current_price": 130.0,
            "pnl_net_rupees": 1500.0, "entry_contract": "NIFTY23000CE",
        },
    ]
    payoff = build_portfolio_payoff(open_rows, spot=23050.0, options_analytics={"expected_move_pts": 150.0}, commission_cfg=cfg)
    assert payoff["open_legs"] == 1
    assert payoff["structure_label"] == "Long CE"
    assert len(payoff["spots"]) == CURVE_POINTS
    assert len(payoff["expiry_paper"]) == CURVE_POINTS
    assert payoff["portfolio_mtm_rupees"] == 1500.0
    # Deep ITM at the top of the range should show a large expiry profit.
    assert payoff["expiry_paper"][-1] > 0

    empty_payoff = build_portfolio_payoff([], spot=23000.0, commission_cfg=cfg)
    assert empty_payoff["open_legs"] == 0
    assert empty_payoff["structure_label"] == "Flat"
    assert empty_payoff["spot"] == 23000.0

    print("[analytics.payoff_engine] selftest OK: greek lookup, structure label, breakevens, payoff curve")


if __name__ == "__main__":
    _selftest()
