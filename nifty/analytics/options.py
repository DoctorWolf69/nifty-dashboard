#!/usr/bin/env python3
"""
Phases 7–9 analytics for NIFTY weekly options.

Phase 7 — Delta: OI-weighted chain delta proxy, velocity, price–delta divergence
Phase 8 — Greeks: Black–Scholes delta/gamma/theta/vega + net GEX estimate
Phase 9 — IV: implied vol, skew, velocity, rank/percentile from stored history
"""

from __future__ import annotations

import json
import math
import os
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

from scipy.stats import norm

IST = __import__("zoneinfo").ZoneInfo("Asia/Kolkata")
DEFAULT_RISK_FREE = float(os.environ.get("NIFTY_RISK_FREE_RATE", "0.065"))
NIFTY_LOT = int(os.environ.get("NIFTY_LOT_SIZE", "65"))


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def year_fraction_to_expiry(expiry: str, now: Optional[datetime] = None) -> float:
    """Time to expiry in years (expiry day 15:30 IST)."""
    now_ist = (now or datetime.now(IST)).astimezone(IST)
    try:
        exp_day = datetime.strptime(str(expiry)[:10], "%Y-%m-%d").date()
    except ValueError:
        return max(1 / 365, 1 / 252 / 6)
    exp_dt = datetime.combine(exp_day, time(15, 30), tzinfo=IST)
    seconds = (exp_dt - now_ist).total_seconds()
    return max(seconds / (365.25 * 24 * 3600), 1 / (252 * 6.5 * 3600))


def bs_price(
    spot: float,
    strike: float,
    t: float,
    rate: float,
    sigma: float,
    option_type: str,
) -> float:
    if spot <= 0 or strike <= 0 or t <= 0 or sigma <= 0:
        return 0.0
    sqrt_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * t) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    if option_type.upper() == "CE":
        return spot * norm.cdf(d1) - strike * math.exp(-rate * t) * norm.cdf(d2)
    return strike * math.exp(-rate * t) * norm.cdf(-d2) - spot * norm.cdf(-d1)


def bs_greeks(
    spot: float,
    strike: float,
    t: float,
    rate: float,
    sigma: float,
    option_type: str,
) -> Dict[str, float]:
    if spot <= 0 or strike <= 0 or t <= 0 or sigma <= 0:
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}
    sqrt_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * t) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    pdf = norm.pdf(d1)
    if option_type.upper() == "CE":
        delta = norm.cdf(d1)
        theta = (
            -(spot * pdf * sigma) / (2 * sqrt_t)
            - rate * strike * math.exp(-rate * t) * norm.cdf(d2)
        ) / 365.0
        rho = strike * t * math.exp(-rate * t) * norm.cdf(d2) / 100.0
    else:
        delta = norm.cdf(d1) - 1.0
        theta = (
            -(spot * pdf * sigma) / (2 * sqrt_t)
            + rate * strike * math.exp(-rate * t) * norm.cdf(-d2)
        ) / 365.0
        rho = -strike * t * math.exp(-rate * t) * norm.cdf(-d2) / 100.0
    gamma = pdf / (spot * sigma * sqrt_t)
    vega = spot * pdf * sqrt_t / 100.0
    return {
        "delta": round(delta, 4),
        "gamma": round(gamma, 6),
        "theta": round(theta, 4),
        "vega": round(vega, 4),
        "rho": round(rho, 4),
    }


def implied_volatility(
    price: float,
    spot: float,
    strike: float,
    t: float,
    rate: float,
    option_type: str,
    *,
    max_iter: int = 60,
) -> Optional[float]:
    if price <= 0 or spot <= 0 or strike <= 0 or t <= 0:
        return None
    intrinsic = max(0.0, spot - strike) if option_type.upper() == "CE" else max(0.0, strike - spot)
    if price < intrinsic * 0.98:
        return None
    lo, hi = 0.01, 3.0
    for _ in range(max_iter):
        mid = (lo + hi) / 2
        model = bs_price(spot, strike, t, rate, mid, option_type)
        if model > price:
            hi = mid
        else:
            lo = mid
    return round((lo + hi) / 2, 4)


@dataclass
class IVHistoryStore:
    """Persist ATM IV samples for rank / percentile (Phase 9)."""

    path: Path
    samples: Deque[float] = field(default_factory=lambda: deque(maxlen=500))
    intraday: Deque[Tuple[float, float]] = field(default_factory=lambda: deque(maxlen=240))

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                iv = _as_float(row.get("atm_iv"))
                if iv > 0:
                    self.samples.append(iv)
        except (OSError, json.JSONDecodeError):
            return

    def record(self, atm_iv: float, ts: Optional[float] = None) -> None:
        if atm_iv <= 0:
            return
        stamp = ts or datetime.now().timestamp()
        if self.intraday and abs(self.intraday[-1][1] - atm_iv) < 0.05:
            return
        self.intraday.append((stamp, atm_iv))
        today = datetime.fromtimestamp(stamp).date().isoformat()
        last_daily = getattr(self, "_last_daily_write", "")
        if last_daily != today:
            self.samples.append(atm_iv)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps({"date": today, "atm_iv": atm_iv, "recorded_at": datetime.fromtimestamp(stamp).isoformat()})
                    + "\n"
                )
            self._last_daily_write = today

    def rank_percentile(self, atm_iv: float) -> Dict[str, Optional[float]]:
        if atm_iv <= 0 or len(self.samples) < 5:
            return {"iv_rank": None, "iv_percentile": None, "sample_count": len(self.samples)}
        values = list(self.samples)
        lo, hi = min(values), max(values)
        rank = round(((atm_iv - lo) / (hi - lo)) * 100, 1) if hi > lo else 50.0
        below = sum(1 for v in values if v <= atm_iv)
        pct = round((below / len(values)) * 100, 1)
        return {"iv_rank": rank, "iv_percentile": pct, "sample_count": len(values)}

    def iv_velocity(self, atm_iv: float, seconds: int = 300, now_ts: Optional[float] = None) -> Optional[float]:
        if atm_iv <= 0 or not self.intraday:
            return None
        cutoff = (now_ts or datetime.now().timestamp()) - seconds
        old_iv = None
        for ts, iv in self.intraday:
            if ts <= cutoff:
                old_iv = iv
            else:
                break
        if old_iv is None or old_iv <= 0:
            return None
        return round(atm_iv - old_iv, 4)


def classify_price_delta_divergence(spot_delta: float, net_delta_change: float) -> Dict[str, Any]:
    """Phase 7 — price vs OI-weighted delta proxy."""
    spot_up = spot_delta > 8
    spot_down = spot_delta < -8
    delta_up = net_delta_change > 0
    delta_down = net_delta_change < 0
    label = "NEUTRAL"
    read = "Spot and chain delta moving together"
    if spot_up and delta_up:
        label, read = "AGGRESSIVE_BUYERS", "Price ↑ + net delta ↑ — aggressive buyers"
    elif spot_down and delta_down:
        label, read = "AGGRESSIVE_SELLERS", "Price ↓ + net delta ↓ — aggressive sellers"
    elif spot_up and delta_down:
        label, read = "BUYER_ABSORPTION", "Price ↑ but net delta ↓ — buyer absorption / distribution"
    elif spot_down and delta_up:
        label, read = "SELLER_ABSORPTION", "Price ↓ but net delta ↑ — seller absorption / support"
    return {"label": label, "read": read}


def analyze_option_chain(
    paired_rows: List[Dict[str, Any]],
    *,
    spot: float,
    expiry: str,
    lot_size: int = NIFTY_LOT,
    risk_free: float = DEFAULT_RISK_FREE,
    spot_delta_5m: float = 0.0,
    prev_net_dealer_delta: Optional[float] = None,
    india_vix: Optional[float] = None,
    iv_store: Optional[IVHistoryStore] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Build Phases 7–9 payload from live paired CE/PE rows.

    `now` is the engine clock (naive IST). Without it, replay computed
    time-to-expiry against the wall clock - already-expired for archived
    days, so IV solve and greeks silently emptied out."""
    if spot <= 0 or not paired_rows:
        return {"error": "no_chain_data"}

    t = year_fraction_to_expiry(expiry, now)
    now_ts = (now or datetime.now()).timestamp()
    strikes = sorted(_as_int(row.get("strike")) for row in paired_rows)
    atm_strike = min(strikes, key=lambda k: abs(k - spot)) if strikes else 0

    chain_rows: List[Dict[str, Any]] = []
    net_dealer_delta = 0.0
    net_gex = 0.0
    total_call_oi = 0
    total_put_oi = 0
    iv_calls: List[float] = []
    iv_puts: List[float] = []
    atm_ce_iv = None
    atm_pe_iv = None
    atm_straddle = 0.0

    for pair in paired_rows:
        strike = _as_int(pair.get("strike"))
        ce = pair.get("ce") or {}
        pe = pair.get("pe") or {}
        ce_ltp = _as_float(ce.get("last_price"))
        pe_ltp = _as_float(pe.get("last_price"))
        ce_oi = _as_int(ce.get("oi"))
        pe_oi = _as_int(pe.get("oi"))
        total_call_oi += ce_oi
        total_put_oi += pe_oi

        ce_iv = implied_volatility(ce_ltp, spot, strike, t, risk_free, "CE") if ce_ltp > 0 else None
        pe_iv = implied_volatility(pe_ltp, spot, strike, t, risk_free, "PE") if pe_ltp > 0 else None
        if ce_iv:
            iv_calls.append(ce_iv)
        if pe_iv:
            iv_puts.append(pe_iv)
        if strike == atm_strike:
            atm_ce_iv, atm_pe_iv = ce_iv, pe_iv
            atm_straddle = ce_ltp + pe_ltp

        ce_sigma = ce_iv or 0.18
        pe_sigma = pe_iv or 0.18
        ce_g = bs_greeks(spot, strike, t, risk_free, ce_sigma, "CE")
        pe_g = bs_greeks(spot, strike, t, risk_free, pe_sigma, "PE")

        # Dealers short customer OI → flip sign for exposure
        ce_dd = -ce_g["delta"] * ce_oi * lot_size
        pe_dd = -pe_g["delta"] * pe_oi * lot_size
        net_dealer_delta += ce_dd + pe_dd

        gex_scale = (spot ** 2) * lot_size * 0.01
        strike_gex = (ce_g["gamma"] * ce_oi - pe_g["gamma"] * pe_oi) * gex_scale
        net_gex += strike_gex

        chain_rows.append(
            {
                "strike": strike,
                "ce_ltp": ce_ltp,
                "pe_ltp": pe_ltp,
                "ce_oi": ce_oi,
                "pe_oi": pe_oi,
                "ce_iv": ce_iv,
                "pe_iv": pe_iv,
                "ce_delta": ce_g["delta"],
                "pe_delta": pe_g["delta"],
                "ce_gamma": ce_g["gamma"],
                "pe_gamma": pe_g["gamma"],
                "ce_theta": ce_g["theta"],
                "pe_theta": pe_g["theta"],
                "ce_vega": ce_g["vega"],
                "pe_vega": pe_g["vega"],
                "dealer_delta": round(ce_dd + pe_dd, 0),
                "gex": round(strike_gex, 0),
                "distance_from_spot": round(spot - strike, 2),
            }
        )

    atm_iv = None
    if atm_ce_iv and atm_pe_iv:
        atm_iv = round((atm_ce_iv + atm_pe_iv) / 2, 4)
    elif atm_ce_iv or atm_pe_iv:
        atm_iv = atm_ce_iv or atm_pe_iv

    iv_skew = None
    if atm_ce_iv and atm_pe_iv:
        iv_skew = round(atm_pe_iv - atm_ce_iv, 4)

    iv_velocity = iv_store.iv_velocity(atm_iv or 0.0, now_ts=now_ts) if iv_store and atm_iv else None
    iv_stats = iv_store.rank_percentile(atm_iv or 0.0) if iv_store and atm_iv else {"iv_rank": None, "iv_percentile": None}

    if iv_store and atm_iv:
        iv_store.record(atm_iv, ts=now_ts)

    expected_move_pts = round(spot * (atm_iv or 0.15) * math.sqrt(max(t, 1 / 252)), 2) if atm_iv else round(atm_straddle, 2)

    delta_change = (
        round(net_dealer_delta - prev_net_dealer_delta, 0)
        if prev_net_dealer_delta is not None
        else 0.0
    )
    divergence = classify_price_delta_divergence(spot_delta_5m, delta_change)

    pcr_oi = round(total_put_oi / total_call_oi, 3) if total_call_oi else None
    premium_cheap = bool(atm_iv and india_vix and atm_iv < india_vix / 100 * 0.85)
    premium_expensive = bool(atm_iv and india_vix and atm_iv > india_vix / 100 * 1.15)

    return {
        "phase": "7-9",
        "expiry": expiry,
        "time_to_expiry_years": round(t, 6),
        "atm_strike": atm_strike,
        "atm_iv": atm_iv,
        "atm_ce_iv": atm_ce_iv,
        "atm_pe_iv": atm_pe_iv,
        "iv_skew_put_minus_call": iv_skew,
        "iv_velocity_5m": iv_velocity,
        "iv_rank": iv_stats.get("iv_rank"),
        "iv_percentile": iv_stats.get("iv_percentile"),
        "iv_history_samples": iv_stats.get("sample_count", 0),
        "india_vix": india_vix,
        "premium_vs_vix": "CHEAP" if premium_cheap else ("EXPENSIVE" if premium_expensive else "FAIR"),
        "expected_move_pts": expected_move_pts,
        "atm_straddle_premium": round(atm_straddle, 2),
        "net_dealer_delta": round(net_dealer_delta, 0),
        "net_dealer_delta_change": delta_change,
        "net_gex": round(net_gex, 0),
        "net_gex_cr": round(net_gex / 1e7, 2),
        "gex_regime": "POSITIVE_GAMMA" if net_gex > 0 else ("NEGATIVE_GAMMA" if net_gex < 0 else "NEUTRAL"),
        "dealer_positioning": (
            "Dealers short gamma — expansion risk"
            if net_gex < 0
            else "Dealers long gamma — pin / mean-revert bias"
        ),
        "pcr_oi_chain": pcr_oi,
        "price_delta_divergence": divergence,
        "chain_rows": chain_rows,
        "risk_free_rate": risk_free,
        "lot_size": lot_size,
    }
