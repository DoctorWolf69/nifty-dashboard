#!/usr/bin/env python3
"""Multi-expiry NIFTY option chain — surface subscription and term structure.

Ported faithfully from quant-desk-engine v4/ATLAS's nifty_multi_expiry_chain.py
(mentor-authored). No logic changed. Only adaptation: imports
select_active_option_expiry from nifty.core.expiry (added there this
session — see that module's docstring) and
DEFAULT_RISK_FREE/bs_greeks/bs_higher_order_greeks/implied_volatility/
year_fraction_to_expiry from nifty.analytics.options (bs_higher_order_greeks
added there this session; the rest already existed as nifty-dashboard's own
port of the same mentor math).

This gives relationships_lab.py's build_multi_expiry_surface_placeholder a
path to a REAL implementation (build_multi_expiry_surface below) instead of
its synthetic placeholder data — not wired to replace it yet, just now
available as real code.

Duck-types option instruments (attributes: expiry, strike, option_type,
last_price, oi, instrument_token, tradingsymbol, series_role) rather than
importing a specific instrument class, matching the mentor's own design —
works with nifty-dashboard's InstrumentRef or any similarly-shaped object.

Not yet wired into the live pipeline.
Self-check: python -m nifty.analytics.multi_expiry_chain
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from nifty.core.expiry import select_active_option_expiry
from nifty.analytics.options import (
    DEFAULT_RISK_FREE,
    bs_greeks,
    bs_higher_order_greeks,
    implied_volatility,
    year_fraction_to_expiry,
)

EXPIRY_LABELS = {
    "W0": "Current Weekly",
    "W1": "Next Weekly",
    "W2": "Week +2",
    "M0": "Monthly",
    "M1": "Far Monthly",
}


def parse_expiry(value: Any) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def is_monthly_expiry(exp: date) -> bool:
    """NIFTY monthly = last Tuesday of the month."""
    return (exp + timedelta(days=7)).month != exp.month


def nearest(spot: float, step: int) -> int:
    return int(round(spot / step) * step)


def list_nifty_option_expiries(
    nfo_instruments: Iterable[Dict[str, Any]],
    *,
    today: Optional[date] = None,
) -> List[date]:
    day = today or date.today()
    expiries: set[date] = set()
    for item in nfo_instruments:
        if item.get("name") != "NIFTY":
            continue
        if item.get("instrument_type") not in {"CE", "PE"}:
            continue
        exp = parse_expiry(item.get("expiry"))
        if exp >= day:
            expiries.add(exp)
    return sorted(expiries)


def classify_expiries(
    available: Sequence[date],
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Optional[date]]:
    if not available:
        return {"W0": None, "W1": None, "W2": None, "M0": None, "M1": None}
    front = select_active_option_expiry(available, now=now)
    weekly_after = [exp for exp in available if exp > front]
    monthlies = [exp for exp in available if is_monthly_expiry(exp) and exp >= front]
    return {
        "W0": front,
        "W1": weekly_after[0] if weekly_after else None,
        "W2": weekly_after[1] if len(weekly_after) > 1 else None,
        "M0": monthlies[0] if monthlies else None,
        "M1": monthlies[1] if len(monthlies) > 1 else None,
    }


def resolve_surface_instruments(
    nfo_instruments: Iterable[Dict[str, Any]],
    *,
    spot: float,
    front_expiry: str,
    strike_step: int = 100,
    strikes_each_side: int = 2,
    roles: Sequence[str] = ("W1", "W2", "M0", "M1"),
    make_instrument: Callable[..., Any],
) -> Tuple[Dict[str, str], List[Any]]:
    """Resolve supplemental expiry series for term structure (excludes W0 primary chain)."""
    available = list_nifty_option_expiries(nfo_instruments)
    classified = classify_expiries(available)
    front = parse_expiry(front_expiry)
    center = nearest(spot, strike_step)
    wanted_strikes = {
        center + (offset * strike_step) for offset in range(-strikes_each_side, strikes_each_side + 1)
    }

    role_by_expiry: Dict[str, str] = {}
    selected: List[Any] = []
    for role in roles:
        exp = classified.get(role)
        if exp is None or exp == front:
            continue
        role_by_expiry[str(exp)] = role
        for item in nfo_instruments:
            if item.get("name") != "NIFTY":
                continue
            if item.get("instrument_type") not in {"CE", "PE"}:
                continue
            if parse_expiry(item.get("expiry")) != exp:
                continue
            strike = int(float(item.get("strike") or 0))
            if strike not in wanted_strikes:
                continue
            selected.append(
                make_instrument(
                    token=int(item["instrument_token"]),
                    tradingsymbol=str(item["tradingsymbol"]),
                    strike=strike,
                    option_type=str(item["instrument_type"]),
                    expiry=str(exp),
                    series_role=role,
                )
            )
    selected.sort(key=lambda row: (row.series_role, row.strike, row.option_type))
    return role_by_expiry, selected


def _days_to_expiry(expiry: str, now: Optional[datetime] = None) -> int:
    now_ist = now or datetime.now()
    try:
        exp_day = parse_expiry(expiry)
    except ValueError:
        return 0
    return max(0, (exp_day - now_ist.date()).days)


def _group_by_expiry_strike(instruments: Iterable[Any]) -> Dict[str, Dict[int, Dict[str, Any]]]:
    grouped: Dict[str, Dict[int, Dict[str, Any]]] = {}
    for item in instruments:
        ex = str(item.expiry)
        grouped.setdefault(ex, {}).setdefault(int(item.strike), {})[str(item.option_type).lower()] = item
    return grouped


def _atm_iv_for_expiry(
    grouped_strikes: Dict[int, Dict[str, Any]],
    spot: float,
    expiry: str,
    *,
    risk_free: float = DEFAULT_RISK_FREE,
) -> Optional[float]:
    if not grouped_strikes or spot <= 0:
        return None
    atm = min(grouped_strikes.keys(), key=lambda strike: abs(strike - spot))
    legs = grouped_strikes[atm]
    ce = legs.get("ce")
    pe = legs.get("pe")
    t = year_fraction_to_expiry(expiry)
    ivs: List[float] = []
    if ce is not None and float(getattr(ce, "last_price", 0) or 0) > 0:
        iv = implied_volatility(float(ce.last_price), spot, atm, t, risk_free, "CE")
        if iv:
            ivs.append(iv)
    if pe is not None and float(getattr(pe, "last_price", 0) or 0) > 0:
        iv = implied_volatility(float(pe.last_price), spot, atm, t, risk_free, "PE")
        if iv:
            ivs.append(iv)
    return round(sum(ivs) / len(ivs), 4) if ivs else None


def _strike_oi(legs: Dict[str, Any]) -> int:
    total = 0
    for leg in legs.values():
        total += int(getattr(leg, "oi", 0) or 0)
    return total


def build_term_structure(
    *,
    primary_expiry: str,
    primary_atm_iv: Optional[float],
    primary_instruments: Iterable[Any],
    surface_instruments: Iterable[Any],
    spot: float,
    role_by_expiry: Dict[str, str],
) -> Dict[str, Any]:
    points: List[Dict[str, Any]] = []
    if primary_atm_iv is not None:
        points.append(
            {
                "expiry_id": "W0",
                "expiry": primary_expiry,
                "atm_iv": round(float(primary_atm_iv), 4),
                "days_to_expiry": _days_to_expiry(primary_expiry),
            }
        )

    grouped = _group_by_expiry_strike(surface_instruments)
    primary_grouped = _group_by_expiry_strike(primary_instruments)
    for exp_str, strikes in grouped.items():
        atm_iv = _atm_iv_for_expiry(strikes, spot, exp_str)
        if atm_iv is None:
            continue
        points.append(
            {
                "expiry_id": role_by_expiry.get(exp_str, exp_str),
                "expiry": exp_str,
                "atm_iv": atm_iv,
                "days_to_expiry": _days_to_expiry(exp_str),
            }
        )

    points.sort(key=lambda row: row["days_to_expiry"])
    if len(points) < 2:
        return {
            "status": "SINGLE_EXPIRY_CHAIN",
            "note": "Live chain tracks one active expiry; W1/W2/M0/M1 surface pending ticks.",
            "points": points,
        }

    near_iv = float(points[0]["atm_iv"])
    far_iv = float(points[-1]["atm_iv"])
    slope = far_iv - near_iv
    if slope > 0.005:
        curve = "CONTANGO"
    elif slope < -0.005:
        curve = "BACKWARDATION"
    else:
        curve = "FLAT"

    roll_pressure: Optional[Dict[str, Any]] = None
    w1_exp = next((p["expiry"] for p in points if p["expiry_id"] == "W1"), None)
    if w1_exp and primary_expiry:
        w0_grouped = primary_grouped.get(str(primary_expiry)[:10], {})
        w1_grouped = grouped.get(w1_exp, {})
        if spot > 0:
            atm = min(
                set(w0_grouped.keys()) | set(w1_grouped.keys()) or {nearest(spot, 100)},
                key=lambda strike: abs(strike - spot),
            )
            w0_oi = _strike_oi(w0_grouped.get(atm, {}))
            w1_oi = _strike_oi(w1_grouped.get(atm, {}))
            total = w0_oi + w1_oi
            if total > 0:
                roll_pressure = {
                    "atm_strike": atm,
                    "w0_oi": w0_oi,
                    "w1_oi": w1_oi,
                    "w1_share_pct": round(100.0 * w1_oi / total, 1),
                    "label": "ROLL_BUILDING" if w1_oi > w0_oi * 0.35 else "FRONT_DOMINANT",
                }

    return {
        "status": "MULTI_EXPIRY",
        "curve": curve,
        "iv_slope": round(slope, 4),
        "points": points,
        "roll_pressure": roll_pressure,
        "note": f"{curve} — ATM IV slope {slope:+.4f} across {len(points)} tenors",
    }


def build_multi_expiry_surface(
    *,
    primary_expiry: str,
    spot: float,
    primary_instruments: Iterable[Any],
    surface_instruments: Iterable[Any],
    role_by_expiry: Dict[str, str],
    lot_size: int = 65,
    risk_free: float = DEFAULT_RISK_FREE,
) -> Dict[str, Any]:
    primary_list = list(primary_instruments)
    surface_list = list(surface_instruments)
    all_instruments = primary_list + surface_list
    if not all_instruments:
        return {
            "status": "unavailable",
            "note": "No option instruments subscribed.",
            "expiries": [],
            "metrics": [],
            "heatmap": [],
            "migration": [],
        }

    classified = classify_expiries(
        [parse_expiry(primary_expiry)]
        + [parse_expiry(exp) for exp in role_by_expiry.keys()]
    )
    expiries_meta: List[Dict[str, Any]] = []
    for role in ("W0", "W1", "W2", "M0", "M1"):
        exp = classified.get(role)
        if role == "W0":
            exp = parse_expiry(primary_expiry)
        elif exp is None:
            exp = next(
                (parse_expiry(exp_str) for exp_str, rid in role_by_expiry.items() if rid == role),
                None,
            )
        if exp is None:
            continue
        subscribed = role == "W0" or str(exp) in role_by_expiry
        expiries_meta.append(
            {
                "id": role,
                "label": EXPIRY_LABELS.get(role, role),
                "expiry": str(exp),
                "status": "live" if subscribed else "unsubscribed",
            }
        )

    heatmap: List[Dict[str, Any]] = []
    grouped = _group_by_expiry_strike(all_instruments)
    for exp_str, strikes in grouped.items():
        role = "W0" if exp_str == str(primary_expiry)[:10] else role_by_expiry.get(exp_str, "?")
        t = year_fraction_to_expiry(exp_str)
        for strike, legs in sorted(strikes.items()):
            ce = legs.get("ce")
            pe = legs.get("pe")
            oi = int(getattr(ce, "oi", 0) or 0) + int(getattr(pe, "oi", 0) or 0)
            gamma = 0.0
            delta = 0.0
            vanna = 0.0
            charm = 0.0
            for leg in (ce, pe):
                if leg is None:
                    continue
                ltp = float(getattr(leg, "last_price", 0) or 0)
                leg_oi = int(getattr(leg, "oi", 0) or 0)
                opt_type = str(getattr(leg, "option_type", "CE"))
                sigma = implied_volatility(ltp, spot, strike, t, risk_free, opt_type) if ltp > 0 else 0.18
                g = bs_greeks(spot, strike, t, risk_free, sigma or 0.18, opt_type)
                h = bs_higher_order_greeks(spot, strike, t, risk_free, sigma or 0.18, opt_type)
                sign = 1 if opt_type.upper() == "CE" else -1
                gamma += sign * g["gamma"] * leg_oi
                delta += sign * g["delta"] * leg_oi
                vanna += sign * h["vanna"] * leg_oi
                charm += sign * h["charm"] * leg_oi
            heatmap.append(
                {
                    "expiry_id": role,
                    "strike": strike,
                    "oi": oi,
                    "gamma": round(gamma / max(oi, 1), 6),
                    "delta": round(delta / max(oi, 1), 4),
                    "vanna": round(vanna / max(oi, 1), 4),
                    "charm": round(charm / max(oi, 1), 4),
                    "rollover_pct": 0.0,
                }
            )

    migration: List[Dict[str, Any]] = []
    w1_exp = next((meta["expiry"] for meta in expiries_meta if meta["id"] == "W1"), None)
    if w1_exp and primary_expiry:
        w0_rows = grouped.get(str(primary_expiry)[:10], {})
        w1_rows = grouped.get(w1_exp, {})
        for strike in sorted(set(w0_rows.keys()) & set(w1_rows.keys())):
            w0_oi = _strike_oi(w0_rows[strike])
            w1_oi = _strike_oi(w1_rows[strike])
            total = w0_oi + w1_oi
            if total <= 0:
                continue
            migration.append(
                {
                    "strike": strike,
                    "from": "W0",
                    "to": "W1",
                    "oi_shift_pct": round(100.0 * w1_oi / total, 1),
                    "note": f"W1 share {round(100.0 * w1_oi / total, 1)}% at {strike}",
                }
            )
        migration.sort(key=lambda row: row["oi_shift_pct"], reverse=True)
        migration = migration[:8]

    supplemental = len(surface_list)
    return {
        "status": "live" if supplemental else "partial",
        "note": (
            f"Multi-expiry surface live — {supplemental} supplemental contracts across "
            f"{', '.join(sorted(set(role_by_expiry.values()))) or 'W0 only'}"
        ),
        "expiries": expiries_meta,
        "metrics": ["oi", "gamma", "delta", "vanna", "charm"],
        "heatmap": heatmap,
        "migration": migration,
        "subscribed_roles": ["W0", *sorted(set(role_by_expiry.values()))],
        "contract_count": len(all_instruments),
    }


def _selftest() -> None:
    from dataclasses import dataclass as _dc

    @_dc
    class _Instrument:
        token: int
        tradingsymbol: str
        strike: int
        option_type: str
        expiry: str
        series_role: str
        last_price: float = 100.0
        oi: int = 10_000

    assert parse_expiry("2026-07-21") == date(2026, 7, 21)
    assert parse_expiry(date(2026, 7, 21)) == date(2026, 7, 21)
    assert is_monthly_expiry(date(2026, 7, 28)) is True  # last Tuesday of July 2026
    assert is_monthly_expiry(date(2026, 7, 21)) is False
    assert nearest(23050.0, 100) == 23000
    assert nearest(23060.0, 100) == 23100

    nfo = [
        {"name": "NIFTY", "instrument_type": "CE", "expiry": "2026-07-21", "strike": 23000, "instrument_token": 1, "tradingsymbol": "NIFTY26072123000CE"},
        {"name": "NIFTY", "instrument_type": "PE", "expiry": "2026-07-21", "strike": 23000, "instrument_token": 2, "tradingsymbol": "NIFTY26072123000PE"},
        {"name": "NIFTY", "instrument_type": "CE", "expiry": "2026-07-28", "strike": 23000, "instrument_token": 3, "tradingsymbol": "NIFTY26072823000CE"},
        {"name": "NIFTY", "instrument_type": "PE", "expiry": "2026-07-28", "strike": 23000, "instrument_token": 4, "tradingsymbol": "NIFTY26072823000PE"},
        {"name": "NIFTY", "instrument_type": "CE", "expiry": "2026-07-28", "strike": 23400, "instrument_token": 5, "tradingsymbol": "NIFTY26072823400CE"},
        {"name": "NIFTY", "instrument_type": "PE", "expiry": "2026-08-25", "strike": 23000, "instrument_token": 6, "tradingsymbol": "NIFTY26082523000PE"},
    ]
    today = date(2026, 7, 20)
    expiries = list_nifty_option_expiries(nfo, today=today)
    assert expiries == [date(2026, 7, 21), date(2026, 7, 28), date(2026, 8, 25)]

    classified = classify_expiries(expiries, now=datetime(2026, 7, 20, 10, 0))
    assert classified["W0"] == date(2026, 7, 21)
    assert classified["W1"] == date(2026, 7, 28)
    # July 28 is itself the last Tuesday of July (a monthly by this rule), so it's
    # correctly classified as BOTH W1 and M0 — classify_expiries doesn't dedupe
    # weekly vs monthly roles, it evaluates each independently. Aug 25 is M1.
    assert classified["M0"] == date(2026, 7, 28)
    assert classified["M1"] == date(2026, 8, 25)

    role_by_expiry, selected = resolve_surface_instruments(
        nfo, spot=23000.0, front_expiry="2026-07-21", strike_step=100, strikes_each_side=2,
        make_instrument=lambda **kw: _Instrument(**kw),
    )
    assert "2026-07-28" in role_by_expiry
    # July 28 satisfies both W1 and M0 (see the classify_expiries note above); the
    # roles loop processes ("W1", "W2", "M0", "M1") in order, so M0 — evaluated
    # last among the roles that resolve to this date — wins the dict slot.
    assert role_by_expiry["2026-07-28"] == "M0"
    assert len(selected) >= 1
    assert all(isinstance(row, _Instrument) for row in selected)

    primary = [
        _Instrument(1, "NIFTY26072123000CE", 23000, "CE", "2026-07-21", "W0", last_price=120.0, oi=50_000),
        _Instrument(2, "NIFTY26072123000PE", 23000, "PE", "2026-07-21", "W0", last_price=110.0, oi=40_000),
    ]
    surface = [
        _Instrument(3, "NIFTY26072823000CE", 23000, "CE", "2026-07-28", "W1", last_price=180.0, oi=20_000),
        _Instrument(4, "NIFTY26072823000PE", 23000, "PE", "2026-07-28", "W1", last_price=170.0, oi=15_000),
    ]
    term = build_term_structure(
        primary_expiry="2026-07-21", primary_atm_iv=0.14,
        primary_instruments=primary, surface_instruments=surface,
        spot=23000.0, role_by_expiry={"2026-07-28": "W1"},
    )
    assert term["status"] == "MULTI_EXPIRY"
    assert term["curve"] in {"CONTANGO", "BACKWARDATION", "FLAT"}
    assert len(term["points"]) == 2

    surface_result = build_multi_expiry_surface(
        primary_expiry="2026-07-21", spot=23000.0,
        primary_instruments=primary, surface_instruments=surface,
        role_by_expiry={"2026-07-28": "W1"},
    )
    assert surface_result["status"] == "live"
    assert len(surface_result["heatmap"]) >= 2
    assert surface_result["metrics"] == ["oi", "gamma", "delta", "vanna", "charm"]

    empty_surface = build_multi_expiry_surface(
        primary_expiry="2026-07-21", spot=23000.0,
        primary_instruments=[], surface_instruments=[], role_by_expiry={},
    )
    assert empty_surface["status"] == "unavailable"

    single_expiry_term = build_term_structure(
        primary_expiry="2026-07-21", primary_atm_iv=0.14,
        primary_instruments=primary, surface_instruments=[],
        spot=23000.0, role_by_expiry={},
    )
    assert single_expiry_term["status"] == "SINGLE_EXPIRY_CHAIN"

    print("[analytics.multi_expiry_chain] selftest OK: expiry classification, term structure, surface heatmap")


if __name__ == "__main__":
    _selftest()
