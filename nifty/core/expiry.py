#!/usr/bin/env python3
"""Expiry-day and session timing rules for the NIFTY F&O desk.

select_active_option_expiry()/should_roll_option_expiry() ported faithfully
from quant-desk-engine v4/ATLAS's evolved desk_expiry_rules.py
(mentor-authored) — additive only, no existing function's behavior changed.
Needed by nifty_multi_expiry_chain.py / nifty_expiry_calendar.py (not yet
ported) to pick/roll the subscribed weekly series after 15:30 IST on expiry
day.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Normal vs expiry-day windows (IST)
ORB_NO_TRADE_START = (9, 15)
ORB_NO_TRADE_END_NORMAL = (9, 30)
ORB_NO_TRADE_END_EXPIRY = (9, 45)  # Extended observe on weekly expiry
NIFTY_OPTIONS_ALLOWED_FROM = (9, 45)  # Expiry day: Nifty options only after 9:45


def _minutes(clock: Tuple[int, int]) -> int:
    return clock[0] * 60 + clock[1]


def is_nifty_weekly_expiry(trade_date: Optional[date] = None) -> bool:
    """NIFTY weekly expiry is Tuesday."""
    day = trade_date or date.today()
    return day.weekday() == 1


def is_expiry_session(*, trade_date: Optional[date] = None, option_expiry: Optional[str] = None) -> bool:
    """True when today's session is the option expiry day."""
    day = trade_date or date.today()
    if option_expiry:
        try:
            return str(option_expiry)[:10] == day.isoformat()
        except (TypeError, ValueError):
            pass
    return is_nifty_weekly_expiry(day)


def no_trade_window_end(is_expiry: bool) -> Tuple[int, int]:
    return ORB_NO_TRADE_END_EXPIRY if is_expiry else ORB_NO_TRADE_END_NORMAL


def no_trade_window_label(is_expiry: bool) -> str:
    end = no_trade_window_end(is_expiry)
    return f"{ORB_NO_TRADE_START[0]:02d}:{ORB_NO_TRADE_START[1]:02d}-{end[0]:02d}:{end[1]:02d} IST"


def is_no_trade_window(
    now: Optional[datetime] = None,
    *,
    is_expiry: bool = False,
) -> bool:
    current = now or datetime.now()
    if current.weekday() >= 5:
        return False
    start = _minutes(ORB_NO_TRADE_START)
    end = _minutes(no_trade_window_end(is_expiry))
    clock = _minutes((current.hour, current.minute))
    return start <= clock < end


def no_trade_seconds_remaining(
    now: Optional[datetime] = None,
    *,
    is_expiry: bool = False,
) -> int:
    current = now or datetime.now()
    end = _minutes(no_trade_window_end(is_expiry))
    remaining = (end - _minutes((current.hour, current.minute))) * 60 - current.second
    return max(0, remaining)


def is_nifty_options_blocked(
    now: Optional[datetime] = None,
    *,
    is_expiry: bool = False,
) -> bool:
    """On expiry day, Nifty options blocked until 9:45."""
    if not is_expiry:
        return is_no_trade_window(now, is_expiry=False)
    current = now or datetime.now()
    if current.weekday() >= 5:
        return False
    allowed_from = _minutes(NIFTY_OPTIONS_ALLOWED_FROM)
    clock = _minutes((current.hour, current.minute))
    return clock < allowed_from


def select_active_option_expiry(
    available: Iterable[Any],
    now: Optional[datetime] = None,
) -> date:
    """Pick front weekly; after 15:30 on expiry day roll to next series."""
    current = now or datetime.now()
    today = current.date()
    parsed: List[date] = []
    for item in available:
        if isinstance(item, date):
            exp = item
        else:
            try:
                exp = datetime.strptime(str(item)[:10], "%Y-%m-%d").date()
            except ValueError:
                continue
        if exp >= today:
            parsed.append(exp)
    if not parsed:
        raise ValueError("no active option expiries")
    candidates = sorted(set(parsed))
    front = candidates[0]
    if front == today and is_nifty_weekly_expiry(today):
        if _minutes((current.hour, current.minute)) >= _minutes((15, 30)) and len(candidates) > 1:
            return candidates[1]
    return front


def should_roll_option_expiry(current_expiry: str, now: Optional[datetime] = None) -> bool:
    """True when subscribed series should move to next weekly."""
    current = now or datetime.now()
    today = current.date()
    try:
        exp = datetime.strptime(str(current_expiry)[:10], "%Y-%m-%d").date()
    except ValueError:
        return False
    if exp < today:
        return True
    if exp == today and is_nifty_weekly_expiry(today):
        return _minutes((current.hour, current.minute)) >= _minutes((15, 30))
    return False


def max_pain_pull_context(
    spot: float,
    max_pain: Optional[float],
    prev_close: Optional[float],
) -> Dict[str, Any]:
    if not max_pain or not prev_close or spot <= 0:
        return {}
    gap_to_mp = round(max_pain - prev_close, 2)
    spot_vs_mp = round(spot - max_pain, 2)
    below_mp = prev_close < max_pain
    return {
        "max_pain": max_pain,
        "prev_close": prev_close,
        "spot": round(spot, 2),
        "close_vs_max_pain_pts": gap_to_mp,
        "spot_vs_max_pain_pts": spot_vs_mp,
        "below_max_pain_at_close": below_mp,
        "expiry_pull_bias": "UPWARD" if below_mp else ("DOWNWARD" if prev_close > max_pain else "NEUTRAL"),
        "note": (
            f"Close {prev_close:.0f} is {abs(gap_to_mp):.0f} pts "
            f"{'below' if below_mp else 'above'} max pain {max_pain:.0f} — "
            f"expiry {'upward' if below_mp else 'downward'} pull context"
        ),
    }


def build_expiry_session_rules(
    *,
    is_expiry: bool,
    combined_bias: str = "NEUTRAL",
    max_pain: Optional[float] = None,
    spot: float = 0.0,
    prev_close: Optional[float] = None,
) -> Dict[str, Any]:
    end = no_trade_window_end(is_expiry)
    rules = {
        "is_expiry_day": is_expiry,
        "no_trade_window": no_trade_window_label(is_expiry),
        "no_trade_until": f"{end[0]:02d}:{end[1]:02d} IST",
        "nifty_options_from": "09:45 IST" if is_expiry else "09:30 IST",
        "primary_instrument": "BANKNIFTY" if is_expiry else "NIFTY",
        "secondary_instrument": "NIFTY" if is_expiry else None,
        "avoid_sectors": ["IT", "METAL"] if is_expiry else [],
        "rules": [],
    }
    if is_expiry:
        rules["rules"] = [
            "Observe only 9:15–9:45 — expiry ORB trap + delta hedging",
            "BankNifty primary in first hour; Nifty options only after 9:45",
            "No Nifty options in first 30 min — premium collapse unpredictable",
            "Wider stops — expiry volatility higher",
            "VIX > 18 at open → reduce size to 25%",
        ]
    else:
        rules["rules"] = [
            "Observe only 9:15–9:30 — ORB forming",
            "Fresh entries from 9:30 when participants confirm",
        ]
    if max_pain and prev_close:
        rules["max_pain_context"] = max_pain_pull_context(spot or prev_close, max_pain, prev_close)
    return rules


EXPIRY_SCENARIOS = (
    {
        "id": "A",
        "label": "Max pain pull",
        "weight_pct": 40,
        "path": "Open near 23,100 → holds 23,070 low → drifts toward 23,300 → pull to max pain",
        "trade": "BankNifty CE on morning low confirmation after 9:45",
    },
    {
        "id": "B",
        "label": "Continued selling",
        "weight_pct": 35,
        "path": "Breaks 23,070 → 23,000 support → put writer panic",
        "trade": "Wait — if 23,000 holds 30+ min → buy reclaim",
    },
    {
        "id": "C",
        "label": "Volatile whipsaw",
        "weight_pct": 25,
        "path": "300pt swings both ways — no clear direction",
        "trade": "No trade — sit on hands",
    },
)


def _selftest() -> None:
    tue = date(2026, 7, 21)
    next_tue = date(2026, 7, 28)

    # Existing behavior untouched: weekly expiry is Tuesday, no-trade windows unchanged.
    assert is_nifty_weekly_expiry(tue) is True
    assert is_nifty_weekly_expiry(date(2026, 7, 22)) is False

    # select_active_option_expiry: front weekly picked when not yet at expiry-day rollover.
    available = [tue, next_tue]
    before_close = datetime(2026, 7, 21, 14, 0)
    assert select_active_option_expiry(available, before_close) == tue

    # After 15:30 on expiry day itself, rolls to the next weekly.
    after_close = datetime(2026, 7, 21, 15, 31)
    assert select_active_option_expiry(available, after_close) == next_tue

    # Non-expiry day (Monday, before that week's Tuesday expiry): front weekly
    # stays selected regardless of time of day — only expiry-day itself triggers
    # the 15:30 rollover check.
    mon_late = datetime(2026, 7, 20, 15, 45)
    assert select_active_option_expiry(available, mon_late) == tue

    # Only one candidate left on expiry day past close -> stays on it (nothing to roll to).
    assert select_active_option_expiry([tue], after_close) == tue

    # Past expiries are excluded; empty result raises rather than silently picking a stale date.
    try:
        select_active_option_expiry([date(2020, 1, 1)], before_close)
        raise AssertionError("expected ValueError for no active expiries")
    except ValueError:
        pass

    # should_roll_option_expiry: mirrors the same rollover timing.
    assert should_roll_option_expiry("2026-07-21", before_close) is False
    assert should_roll_option_expiry("2026-07-21", after_close) is True
    assert should_roll_option_expiry("2026-07-20", before_close) is True  # already past -> roll
    same_day_not_tuesday = datetime(2026, 7, 22, 15, 45)  # exp == today, but today isn't a weekly-expiry weekday
    assert should_roll_option_expiry("2026-07-22", same_day_not_tuesday) is False
    assert should_roll_option_expiry("not-a-date", before_close) is False  # malformed -> safe default

    print("[core.expiry] selftest OK: weekly expiry detection, active-expiry selection, rollover timing")


if __name__ == "__main__":
    _selftest()
