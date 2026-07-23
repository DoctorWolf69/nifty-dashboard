#!/usr/bin/env python3
"""NIFTY F&O expiry schedule — options weekly/monthly roles and futures series.

Ported faithfully from quant-desk-engine v4/ATLAS's nifty_expiry_calendar.py
(mentor-authored). No logic changed. Only adaptation: imports from
nifty.core.expiry (is_expiry_session, is_nifty_weekly_expiry,
select_active_option_expiry, should_roll_option_expiry) and
nifty.analytics.multi_expiry_chain (EXPIRY_LABELS, classify_expiries,
is_monthly_expiry, list_nifty_option_expiries, parse_expiry) instead of the
standalone desk_expiry_rules / nifty_multi_expiry_chain modules.

Not yet wired into the live pipeline.
Self-check: python -m nifty.analytics.expiry_calendar
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from nifty.core.expiry import (
    is_expiry_session,
    is_nifty_weekly_expiry,
    select_active_option_expiry,
    should_roll_option_expiry,
)
from nifty.analytics.multi_expiry_chain import (
    EXPIRY_LABELS,
    classify_expiries,
    is_monthly_expiry,
    list_nifty_option_expiries,
    parse_expiry,
)

WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _weekday_label(day: date) -> str:
    return WEEKDAY_NAMES[day.weekday()]


def _days_to_expiry(exp: date, *, today: Optional[date] = None) -> int:
    ref = today or date.today()
    return max(0, (exp - ref).days)


def _sample_tradingsymbol(
    nfo_instruments: Iterable[Dict[str, Any]],
    *,
    expiry: date,
    instrument_type: str = "CE",
) -> Optional[str]:
    for item in nfo_instruments:
        if item.get("name") != "NIFTY":
            continue
        if str(item.get("instrument_type")) != instrument_type:
            continue
        try:
            if parse_expiry(item.get("expiry")) != expiry:
                continue
        except ValueError:
            continue
        sym = str(item.get("tradingsymbol") or "").strip()
        if sym:
            return sym
    return None


def list_nifty_futures_expiries(
    nfo_instruments: Iterable[Dict[str, Any]],
    *,
    today: Optional[date] = None,
) -> List[Tuple[date, str]]:
    """Return sorted (expiry, tradingsymbol) for live NIFTY index futures."""
    ref = today or date.today()
    rows: List[Tuple[date, str]] = []
    for item in nfo_instruments:
        if item.get("name") != "NIFTY":
            continue
        if str(item.get("instrument_type")) != "FUT":
            continue
        try:
            exp = parse_expiry(item.get("expiry"))
        except ValueError:
            continue
        if exp < ref:
            continue
        sym = str(item.get("tradingsymbol") or "").strip()
        rows.append((exp, sym))
    rows.sort(key=lambda pair: pair[0])
    deduped: List[Tuple[date, str]] = []
    seen: set[date] = set()
    for exp, sym in rows:
        if exp in seen:
            continue
        seen.add(exp)
        deduped.append((exp, sym))
    return deduped


def build_option_expiry_rows(
    nfo_instruments: Iterable[Dict[str, Any]],
    *,
    now: Optional[datetime] = None,
    limit: int = 12,
) -> List[Dict[str, Any]]:
    """Upcoming NIFTY option expiries with role, type, DTE, sample symbol."""
    current = now or datetime.now()
    today = current.date()
    available = list_nifty_option_expiries(nfo_instruments, today=today)
    if not available:
        return []
    roles = classify_expiries(available, now=current)
    role_by_date = {exp: role for role, exp in roles.items() if exp is not None}
    active = select_active_option_expiry(available, now=current)
    rows: List[Dict[str, Any]] = []
    for exp in available[:limit]:
        exp_type = "monthly" if is_monthly_expiry(exp) else "weekly"
        role = role_by_date.get(exp)
        rows.append(
            {
                "expiry": exp.isoformat(),
                "weekday": _weekday_label(exp),
                "days_to_expiry": _days_to_expiry(exp, today=today),
                "type": exp_type,
                "role": role,
                "role_label": EXPIRY_LABELS.get(role or "", role or ""),
                "is_active_execution": exp == active,
                "is_today": exp == today,
                "sample_symbol": _sample_tradingsymbol(nfo_instruments, expiry=exp),
            }
        )
    return rows


def build_futures_expiry_rows(
    nfo_instruments: Iterable[Dict[str, Any]],
    *,
    today: Optional[date] = None,
    subscribed: Optional[Sequence[Any]] = None,
) -> List[Dict[str, Any]]:
    """Front/next NIFTY futures with tradingsymbol and DTE."""
    ref = today or date.today()
    fut_expiries = list_nifty_futures_expiries(nfo_instruments, today=ref)
    sub_by_expiry: Dict[str, Dict[str, Any]] = {}
    for item in subscribed or ():
        exp = str(getattr(item, "expiry", "") or "")[:10]
        if not exp:
            continue
        sub_by_expiry[exp] = {
            "tradingsymbol": getattr(item, "tradingsymbol", ""),
            "series_role": getattr(item, "series_role", ""),
            "token": getattr(item, "token", None),
        }
    rows: List[Dict[str, Any]] = []
    for idx, (exp, sym) in enumerate(fut_expiries[:4]):
        exp_s = exp.isoformat()
        sub = sub_by_expiry.get(exp_s, {})
        role = sub.get("series_role") or ("front" if idx == 0 else "next" if idx == 1 else f"far_{idx}")
        rows.append(
            {
                "expiry": exp_s,
                "weekday": _weekday_label(exp),
                "days_to_expiry": _days_to_expiry(exp, today=ref),
                "tradingsymbol": sub.get("tradingsymbol") or sym,
                "series_role": role,
                "subscribed": exp_s in sub_by_expiry,
            }
        )
    return rows


def expiry_calendar_summary(
    nfo_instruments: Optional[Iterable[Dict[str, Any]]] = None,
    *,
    subscribed_option_expiry: Optional[str] = None,
    subscribed_futures: Optional[Sequence[Any]] = None,
    subscribed_option_count: int = 0,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Full expiry context for dashboard API and execution gates."""
    current = now or datetime.now()
    today = current.date()
    nfo = list(nfo_instruments or [])
    has_instruments = bool(nfo)

    option_rows: List[Dict[str, Any]] = []
    futures_rows: List[Dict[str, Any]] = []
    roles_payload: Dict[str, Any] = {}
    active_exp: Optional[date] = None

    if has_instruments:
        available = list_nifty_option_expiries(nfo, today=today)
        if available:
            active_exp = select_active_option_expiry(available, now=current)
            classified = classify_expiries(available, now=current)
            for role, exp in classified.items():
                if exp is None:
                    roles_payload[role] = None
                    continue
                roles_payload[role] = {
                    "expiry": exp.isoformat(),
                    "weekday": _weekday_label(exp),
                    "days_to_expiry": _days_to_expiry(exp, today=today),
                    "type": "monthly" if is_monthly_expiry(exp) else "weekly",
                    "label": EXPIRY_LABELS.get(role, role),
                }
        option_rows = build_option_expiry_rows(nfo, now=current)
        futures_rows = build_futures_expiry_rows(nfo, today=today, subscribed=subscribed_futures)

    sub_exp = str(subscribed_option_expiry or "")[:10] or None
    if sub_exp and active_exp is None:
        try:
            active_exp = parse_expiry(sub_exp)
        except ValueError:
            pass
    exec_exp = sub_exp or (active_exp.isoformat() if active_exp else None)
    exec_date: Optional[date] = None
    if exec_exp:
        try:
            exec_date = parse_expiry(exec_exp)
        except ValueError:
            exec_date = None

    weekly_today = is_nifty_weekly_expiry(today)
    expiry_today = is_expiry_session(trade_date=today, option_expiry=exec_exp)
    roll_pending = bool(exec_exp and should_roll_option_expiry(exec_exp, now=current))

    next_weekly = next((r for r in option_rows if r["type"] == "weekly" and r["days_to_expiry"] > 0), None)
    next_monthly = next((r for r in option_rows if r["type"] == "monthly"), None)
    if next_monthly and next_monthly.get("days_to_expiry", 0) == 0 and len(option_rows) > 1:
        next_monthly = next((r for r in option_rows if r["type"] == "monthly" and r["days_to_expiry"] > 0), next_monthly)

    sample_symbol = None
    if exec_date and has_instruments:
        sample_symbol = _sample_tradingsymbol(nfo, expiry=exec_date)

    return {
        "generated_at": current.isoformat(timespec="seconds"),
        "source": "kite_nfo" if has_instruments else "state_only",
        "weekly_expiry_weekday": "Tuesday",
        "active_execution_expiry": exec_exp,
        "days_to_active_expiry": _days_to_expiry(exec_date, today=today) if exec_date else None,
        "is_expiry_day_today": expiry_today,
        "is_weekly_expiry_today": weekly_today,
        "roll_pending": roll_pending,
        "roll_rule": "After 15:30 IST on weekly expiry day → next series",
        "subscribed": {
            "options_expiry": sub_exp,
            "option_contracts": subscribed_option_count,
            "sample_symbol": sample_symbol,
        },
        "roles": roles_payload,
        "option_expiries": option_rows,
        "futures": futures_rows,
        "next_weekly_expiry": next_weekly,
        "next_monthly_expiry": next_monthly,
    }


def _selftest() -> None:
    nfo = [
        {"name": "NIFTY", "instrument_type": "CE", "expiry": "2026-07-21", "strike": 23000, "tradingsymbol": "NIFTY26072123000CE"},
        {"name": "NIFTY", "instrument_type": "PE", "expiry": "2026-07-21", "strike": 23000, "tradingsymbol": "NIFTY26072123000PE"},
        {"name": "NIFTY", "instrument_type": "CE", "expiry": "2026-07-28", "strike": 23000, "tradingsymbol": "NIFTY26072823000CE"},
        {"name": "NIFTY", "instrument_type": "FUT", "expiry": "2026-07-30", "tradingsymbol": "NIFTY26JULFUT"},
        {"name": "NIFTY", "instrument_type": "FUT", "expiry": "2026-08-27", "tradingsymbol": "NIFTY26AUGFUT"},
    ]
    today = date(2026, 7, 20)
    now = datetime(2026, 7, 20, 10, 0)

    fut_expiries = list_nifty_futures_expiries(nfo, today=today)
    assert fut_expiries == [(date(2026, 7, 30), "NIFTY26JULFUT"), (date(2026, 8, 27), "NIFTY26AUGFUT")]

    option_rows = build_option_expiry_rows(nfo, now=now)
    assert len(option_rows) == 2
    front_row = next(r for r in option_rows if r["expiry"] == "2026-07-21")
    assert front_row["is_active_execution"] is True
    assert front_row["sample_symbol"] == "NIFTY26072123000CE"
    assert front_row["role"] == "W0"

    futures_rows = build_futures_expiry_rows(nfo, today=today)
    assert len(futures_rows) == 2
    assert futures_rows[0]["series_role"] == "front"
    assert futures_rows[1]["series_role"] == "next"

    summary = expiry_calendar_summary(nfo, now=now)
    assert summary["active_execution_expiry"] == "2026-07-21"
    assert summary["is_weekly_expiry_today"] is False  # 2026-07-20 is a Monday
    assert summary["roll_pending"] is False
    assert summary["source"] == "kite_nfo"
    assert summary["roles"]["W0"]["expiry"] == "2026-07-21"

    # No instruments at all -> falls back gracefully, no crash, source flips to state_only.
    empty_summary = expiry_calendar_summary(None, subscribed_option_expiry="2026-07-21", now=now)
    assert empty_summary["source"] == "state_only"
    assert empty_summary["active_execution_expiry"] == "2026-07-21"
    assert empty_summary["option_expiries"] == []

    # On the expiry day itself, past 15:30, roll_pending flips True.
    after_close = datetime(2026, 7, 21, 15, 45)
    rolled_summary = expiry_calendar_summary(nfo, subscribed_option_expiry="2026-07-21", now=after_close)
    assert rolled_summary["roll_pending"] is True
    assert rolled_summary["is_weekly_expiry_today"] is True

    print("[analytics.expiry_calendar] selftest OK: option/futures rows, full summary, rollover flag")


if __name__ == "__main__":
    _selftest()
