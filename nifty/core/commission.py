#!/usr/bin/env python3
"""
NIFTY F&O commission and net-P&L estimates for Zerodha-style flat brokerage.

Used to block paper signals that cannot realistically cover round-trip costs
and to journal net P&L after charges on close.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Literal, Optional

from dotenv import load_dotenv

Side = Literal["BUY", "SELL"]

# NSE index options — approximate statutory rates (Apr 2024+ regime)
STT_OPTIONS_SELL = 0.000625  # 0.0625% on sell-side premium turnover
EXCHANGE_TXN_RATE = 0.0003503  # NSE F&O transaction charge (premium)
SEBI_RATE = 0.000001  # ₹10 / crore
GST_RATE = 0.18
STAMP_DUTY_BUY = 0.00003  # varies by state; conservative default


@dataclass(frozen=True)
class CommissionConfig:
    lot_size: int = 65
    brokerage_per_order: float = 20.0
    brokerage_pct_cap: float = 0.0003
    min_net_profit_multiple: float = 3.0
    min_gross_rupees: float = 500.0

    @classmethod
    def from_env(cls, env_path: Optional[str] = None) -> "CommissionConfig":
        load_dotenv(env_path)
        return cls(
            lot_size=int(os.getenv("NIFTY_LOT_SIZE", "65")),
            brokerage_per_order=float(os.getenv("NIFTY_BROKERAGE_PER_ORDER", "20")),
            brokerage_pct_cap=float(os.getenv("NIFTY_BROKERAGE_PCT_CAP", "0.0003")),
            min_net_profit_multiple=float(os.getenv("NIFTY_MIN_NET_PROFIT_MULTIPLE", "3")),
            min_gross_rupees=float(os.getenv("NIFTY_MIN_GROSS_RUPEES", "500")),
        )


def _brokerage(turnover: float, cfg: CommissionConfig) -> float:
    pct_fee = turnover * cfg.brokerage_pct_cap
    # Zerodha F&O: flat fee or % — whichever is lower
    if cfg.brokerage_per_order:
        return min(cfg.brokerage_per_order, pct_fee) if pct_fee > 0 else cfg.brokerage_per_order
    return pct_fee


def estimate_leg_cost(
    premium: float,
    lot_size: int,
    side: Side,
    cfg: Optional[CommissionConfig] = None,
) -> Dict[str, float]:
    """Estimate charges for one option leg (buy or sell)."""
    config = cfg or CommissionConfig.from_env()
    if premium <= 0 or lot_size <= 0:
        return {"total": 0.0, "turnover": 0.0}
    turnover = premium * lot_size
    brokerage = _brokerage(turnover, config)
    exchange = turnover * EXCHANGE_TXN_RATE
    sebi = turnover * SEBI_RATE
    stt = turnover * STT_OPTIONS_SELL if side == "SELL" else 0.0
    stamp = turnover * STAMP_DUTY_BUY if side == "BUY" else 0.0
    gst = (brokerage + exchange + sebi) * GST_RATE
    total = brokerage + exchange + sebi + stt + stamp + gst
    return {
        "turnover": round(turnover, 2),
        "brokerage": round(brokerage, 2),
        "exchange": round(exchange, 2),
        "sebi": round(sebi, 2),
        "stt": round(stt, 2),
        "stamp_duty": round(stamp, 2),
        "gst": round(gst, 2),
        "total": round(total, 2),
    }


def estimate_round_trip(
    entry_premium: float,
    exit_premium: float,
    lot_size: Optional[int] = None,
    cfg: Optional[CommissionConfig] = None,
) -> Dict[str, Any]:
    """Buy to open, sell to close — full round-trip cost."""
    config = cfg or CommissionConfig.from_env()
    qty = lot_size or config.lot_size
    entry = estimate_leg_cost(entry_premium, qty, "BUY", config)
    exit_leg = estimate_leg_cost(exit_premium, qty, "SELL", config)
    total = entry["total"] + exit_leg["total"]
    points_breakeven = total / qty if qty else 0.0
    return {
        "lot_size": qty,
        "entry_leg": entry,
        "exit_leg": exit_leg,
        "round_trip_rupees": round(total, 2),
        "breakeven_points": round(points_breakeven, 2),
        "brokerage_total": round(entry["brokerage"] + exit_leg["brokerage"], 2),
    }


def gross_pnl_rupees(entry_premium: float, exit_premium: float, lot_size: int) -> float:
    return round((exit_premium - entry_premium) * lot_size, 2)


def net_pnl_rupees(
    entry_premium: float,
    exit_premium: float,
    lot_size: Optional[int] = None,
    cfg: Optional[CommissionConfig] = None,
) -> Dict[str, Any]:
    config = cfg or CommissionConfig.from_env()
    qty = lot_size or config.lot_size
    costs = estimate_round_trip(entry_premium, exit_premium, qty, config)
    gross = gross_pnl_rupees(entry_premium, exit_premium, qty)
    net = round(gross - costs["round_trip_rupees"], 2)
    return {
        "gross_rupees": gross,
        "commission_rupees": costs["round_trip_rupees"],
        "net_rupees": net,
        "breakeven_points": costs["breakeven_points"],
        "commission_detail": costs,
    }


def commission_conviction_check(
    entry_premium: float,
    target_premium: float,
    lot_size: Optional[int] = None,
    cfg: Optional[CommissionConfig] = None,
) -> Dict[str, Any]:
    """
    Decide if target move is large enough to pay round-trip costs with conviction.

    Pass when:
      gross_target >= max(min_gross_rupees, round_trip * min_net_profit_multiple)
    """
    config = cfg or CommissionConfig.from_env()
    qty = lot_size or config.lot_size
    costs = estimate_round_trip(entry_premium, target_premium, qty, config)
    gross_target = gross_pnl_rupees(entry_premium, target_premium, qty)
    min_required = max(
        config.min_gross_rupees,
        costs["round_trip_rupees"] * config.min_net_profit_multiple,
    )
    net_at_target = round(gross_target - costs["round_trip_rupees"], 2)
    passed = gross_target >= min_required
    min_target_premium = entry_premium + (min_required / qty)
    return {
        "passed": passed,
        "lot_size": qty,
        "entry_premium": entry_premium,
        "target_premium": target_premium,
        "gross_target_rupees": gross_target,
        "round_trip_rupees": costs["round_trip_rupees"],
        "breakeven_points": costs["breakeven_points"],
        "min_required_gross_rupees": round(min_required, 2),
        "min_target_premium": round(min_target_premium, 2),
        "net_at_target_rupees": net_at_target,
        "min_net_profit_multiple": config.min_net_profit_multiple,
        "commission_detail": costs,
        "reason": (
            "ok"
            if passed
            else (
                f"gross target ₹{gross_target:.0f} < required ₹{min_required:.0f} "
                f"(round-trip ₹{costs['round_trip_rupees']:.0f} × {config.min_net_profit_multiple}x)"
            )
        ),
    }


def enrich_signal_with_commission(signal: Dict[str, Any], cfg: Optional[CommissionConfig] = None) -> Dict[str, Any]:
    """Attach commission fields to a signal dict (mutates copy)."""
    config = cfg or CommissionConfig.from_env()
    entry = float(signal.get("entry_price") or 0)
    target = float(signal.get("target_price") or 0)
    current = float(signal.get("current_price") or entry)
    exit_price = float(signal.get("exit_price") or 0)
    qty = int(signal.get("lot_size") or config.lot_size)

    out = dict(signal)
    out["lot_size"] = qty
    conviction = commission_conviction_check(entry, target, qty, config)
    out["commission"] = {
        "round_trip_at_entry_rupees": conviction["round_trip_rupees"],
        "breakeven_points": conviction["breakeven_points"],
        "min_required_gross_rupees": conviction["min_required_gross_rupees"],
        "min_target_premium": conviction["min_target_premium"],
        "conviction_pass": conviction["passed"],
        "conviction_reason": conviction["reason"],
        "min_net_profit_multiple": conviction["min_net_profit_multiple"],
    }
    if exit_price > 0:
        closed = net_pnl_rupees(entry, exit_price, qty, config)
        out["pnl_gross_rupees"] = closed["gross_rupees"]
        out["pnl_commission_rupees"] = closed["commission_rupees"]
        out["pnl_net_rupees"] = closed["net_rupees"]
        if entry > 0:
            out["pnl_net_pct"] = round((closed["net_rupees"] / (entry * qty)) * 100, 2)
    else:
        mark = net_pnl_rupees(entry, current, qty, config)
        out["pnl_gross_rupees"] = mark["gross_rupees"]
        out["pnl_commission_rupees"] = mark["commission_rupees"]
        out["pnl_net_rupees"] = mark["net_rupees"]
    return out
