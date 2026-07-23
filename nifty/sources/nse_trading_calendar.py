#!/usr/bin/env python3
"""
Official NSE trading calendar — weekends + exchange holidays.

Source: NSE holiday-master API (FO segment for NIFTY F&O desk).
Cached locally at config/nse_trading_calendar.json for offline use.

Ported faithfully from quant-desk-engine v4/ATLAS's nse_trading_calendar.py
(mentor-authored). No logic changed. Only adaptation: BASE_DIR/CALENDAR_PATH
resolve via nifty.paths.PROJECT_ROOT.

Genuinely new capability: named-holiday awareness (not just weekday math),
a locally-cached NSE-API-backed calendar, next/previous trading day
helpers, and a combined calendar+intraday session-phase status
(PRE_OPEN/PRE_OPEN_AUCTION/CASH_SESSION/CLOSING/AFTER_HOURS with
entries_allowed/amo_window flags) for execution UI. Deliberately NOT a
replacement for nifty.jobs.is_trading_day (a simpler, already-live-wired
weekday+static-holiday-list check used by the scheduler) - this is a
separate, richer, NSE-API-backed calendar living alongside it. nifty.pte
.backfill's nse_day_status() already wraps nifty.jobs.is_trading_day
rather than this module, by this session's own earlier documented choice
(porting nse_trading_calendar.py wasn't needed for the PTE backfill
orchestrator); nothing currently imports this new module.

Not yet wired into the live pipeline.
Self-check: python -m nifty.sources.nse_trading_calendar
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from zoneinfo import ZoneInfo

import requests

from nifty.paths import PROJECT_ROOT

IST = ZoneInfo("Asia/Kolkata")
CALENDAR_PATH = PROJECT_ROOT / "config" / "nse_trading_calendar.json"
NSE_HOLIDAY_URL = "https://www.nseindia.com/api/holiday-master?type=trading"
DEFAULT_SEGMENT = "FO"
WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

_logger = logging.getLogger(__name__)
_memory_cache: Dict[str, Any] = {"loaded_at": "", "payload": None}


def ist_now() -> datetime:
    return datetime.now(IST)


def _parse_nse_date(raw: str) -> Optional[date]:
    raw = str(raw or "").strip()
    if not raw:
        return None
    for fmt in ("%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[:10] if fmt == "%Y-%m-%d" else raw, fmt).date()
        except ValueError:
            continue
    return None


def _load_file() -> Optional[Dict[str, Any]]:
    if not CALENDAR_PATH.exists():
        return None
    try:
        return json.loads(CALENDAR_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _save_file(payload: Dict[str, Any]) -> None:
    CALENDAR_PATH.parent.mkdir(parents=True, exist_ok=True)
    CALENDAR_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def fetch_official_calendar(segment: str = DEFAULT_SEGMENT) -> Dict[str, Any]:
    """Pull holiday list from NSE and return normalized payload."""
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0",
            "Accept": "application/json",
            "Referer": "https://www.nseindia.com/",
        }
    )
    session.get("https://www.nseindia.com", timeout=15)
    response = session.get(NSE_HOLIDAY_URL, timeout=25)
    response.raise_for_status()
    raw = response.json()

    holidays: List[Dict[str, Any]] = []
    segments_used: List[str] = []
    section = raw.get(segment)
    if isinstance(section, list):
        segments_used.append(segment)
        for row in section:
            d = _parse_nse_date(str(row.get("tradingDate") or ""))
            if d is None:
                continue
            holidays.append(
                {
                    "date": d.isoformat(),
                    "weekday": str(row.get("weekDay") or WEEKDAY_NAMES[d.weekday()]),
                    "description": str(row.get("description") or "Trading holiday"),
                    "segment": segment,
                    "morning_session": row.get("morning_session"),
                    "evening_session": row.get("evening_session"),
                }
            )
    else:
        for seg, rows in raw.items():
            if not isinstance(rows, list):
                continue
            segments_used.append(str(seg))
            for row in rows:
                d = _parse_nse_date(str(row.get("tradingDate") or ""))
                if d is None:
                    continue
                holidays.append(
                    {
                        "date": d.isoformat(),
                        "weekday": str(row.get("weekDay") or WEEKDAY_NAMES[d.weekday()]),
                        "description": str(row.get("description") or "Trading holiday"),
                        "segment": str(seg),
                        "morning_session": row.get("morning_session"),
                        "evening_session": row.get("evening_session"),
                    }
                )

    holidays.sort(key=lambda r: r["date"])
    by_date: Dict[str, Dict[str, Any]] = {}
    for row in holidays:
        by_date.setdefault(row["date"], row)

    return {
        "source": NSE_HOLIDAY_URL,
        "fetched_at": ist_now().strftime("%Y-%m-%d %H:%M:%S IST"),
        "primary_segment": segment,
        "segments_in_source": sorted(set(segments_used)),
        "holiday_count": len(by_date),
        "holidays": list(by_date.values()),
    }


def refresh_calendar(*, segment: str = DEFAULT_SEGMENT, force: bool = False) -> Dict[str, Any]:
    """Fetch from NSE and write config/nse_trading_calendar.json."""
    if not force:
        existing = _load_file()
        if existing and existing.get("fetched_at", "").startswith(ist_now().date().isoformat()):
            _memory_cache["loaded_at"] = ist_now().isoformat()
            _memory_cache["payload"] = existing
            return existing
    try:
        payload = fetch_official_calendar(segment=segment)
        _save_file(payload)
        _memory_cache["loaded_at"] = ist_now().isoformat()
        _memory_cache["payload"] = payload
        return payload
    except Exception as exc:
        _logger.warning("NSE calendar fetch failed: %s", exc)
        fallback = _load_file()
        if fallback:
            fallback["fetch_error"] = str(exc)
            return fallback
        raise


def load_calendar(*, auto_refresh: bool = True) -> Dict[str, Any]:
    """Load cached calendar; refresh from NSE if missing or stale (once per IST day)."""
    if _memory_cache.get("payload") and str(_memory_cache.get("loaded_at", "")).startswith(
        ist_now().date().isoformat()
    ):
        return _memory_cache["payload"]

    file_payload = _load_file()
    if file_payload and not auto_refresh:
        _memory_cache["payload"] = file_payload
        return file_payload

    if file_payload:
        fetched = str(file_payload.get("fetched_at") or "")
        if fetched.startswith(ist_now().date().isoformat()):
            _memory_cache["payload"] = file_payload
            return file_payload

    if auto_refresh:
        try:
            return refresh_calendar(force=False)
        except Exception:
            if file_payload:
                return file_payload

    if file_payload:
        return file_payload

    return {
        "source": NSE_HOLIDAY_URL,
        "fetched_at": "",
        "primary_segment": DEFAULT_SEGMENT,
        "holiday_count": 0,
        "holidays": [],
        "note": "Calendar not loaded — run refresh_nse_calendar.py",
    }


def holiday_dates(*, segment: Optional[str] = None) -> Set[str]:
    cal = load_calendar()
    seg = segment or cal.get("primary_segment") or DEFAULT_SEGMENT
    out: Set[str] = set()
    for row in cal.get("holidays") or []:
        if segment and str(row.get("segment")) != seg:
            continue
        if row.get("date"):
            out.add(str(row["date"]))
    return out


def holiday_on(day: date, *, segment: Optional[str] = None) -> Optional[Dict[str, Any]]:
    label = day.isoformat()
    cal = load_calendar()
    seg = segment or cal.get("primary_segment") or DEFAULT_SEGMENT
    for row in cal.get("holidays") or []:
        if str(row.get("date")) != label:
            continue
        if segment and str(row.get("segment")) != seg:
            continue
        return row
    return None


def is_weekend(day: Optional[date] = None) -> bool:
    current = day or ist_now().date()
    return current.weekday() >= 5


def is_trading_day(day: Optional[date] = None, *, segment: Optional[str] = None) -> bool:
    current = day or ist_now().date()
    if is_weekend(current):
        return False
    return current.isoformat() not in holiday_dates(segment=segment)


def next_trading_day(start: Optional[date] = None, *, segment: Optional[str] = None) -> date:
    day = start or ist_now().date()
    for _ in range(366):
        if is_trading_day(day, segment=segment):
            return day
        day += timedelta(days=1)
    return day


def previous_trading_day(start: Optional[date] = None, *, segment: Optional[str] = None) -> date:
    day = start or ist_now().date()
    day -= timedelta(days=1)
    for _ in range(366):
        if is_trading_day(day, segment=segment):
            return day
        day -= timedelta(days=1)
    return day


def day_status(day: Optional[date] = None, *, segment: Optional[str] = None) -> Dict[str, Any]:
    current = day or ist_now().date()
    hol = holiday_on(current, segment=segment)
    weekend = is_weekend(current)
    trading = is_trading_day(current, segment=segment)
    if weekend:
        label = "WEEKEND"
        reason = f"{WEEKDAY_NAMES[current.weekday()]} — NSE closed"
    elif hol:
        label = "HOLIDAY"
        reason = hol.get("description") or "NSE trading holiday"
    elif trading:
        label = "TRADING DAY"
        reason = "NSE session scheduled"
    else:
        label = "CLOSED"
        reason = "Non-trading day"

    return {
        "date": current.isoformat(),
        "weekday": WEEKDAY_NAMES[current.weekday()],
        "is_trading_day": trading,
        "is_weekend": weekend,
        "is_holiday": hol is not None,
        "holiday_name": (hol or {}).get("description"),
        "status": label,
        "reason": reason,
        "next_trading_day": next_trading_day(current + timedelta(days=1), segment=segment).isoformat(),
        "calendar_file": str(CALENDAR_PATH),
        "calendar_fetched_at": load_calendar().get("fetched_at"),
    }


def session_status(now: Optional[datetime] = None) -> Dict[str, Any]:
    """Combined calendar + intraday session window for execution UI."""
    current = now or ist_now()
    d = current.date()
    base = day_status(d)
    t = current.time()

    if not base["is_trading_day"]:
        base["session_phase"] = "CLOSED"
        base["entries_allowed"] = False
        base["amo_window"] = False
        base["detail"] = base["reason"]
        return base

    if t < time(9, 0):
        phase, detail = "PRE_OPEN", "Pre-open — AMO may be accepted after 05:30 IST"
        entries = False
        amo = t >= time(5, 30)
    elif t < time(9, 15):
        phase, detail = "PRE_OPEN_AUCTION", "Pre-open auction — no regular entries yet"
        entries = False
        amo = True
    elif t < time(15, 15):
        phase, detail = "CASH_SESSION", "Regular session — entries allowed until 15:15 IST"
        entries = True
        amo = False
    elif t < time(15, 30):
        phase, detail = "CLOSING", "Cash closing — no fresh entries"
        entries = False
        amo = False
    else:
        phase, detail = "AFTER_HOURS", "After close — AMO/GTT only until next session"
        entries = False
        amo = t >= time(5, 30) or t < time(9, 0)

    base["session_phase"] = phase
    base["entries_allowed"] = entries
    base["amo_window"] = amo
    base["detail"] = detail
    base["time_ist"] = current.strftime("%H:%M:%S")
    return base


def calendar_summary() -> Dict[str, Any]:
    cal = load_calendar()
    today = day_status()
    upcoming: List[Dict[str, Any]] = []
    start = ist_now().date()
    for row in cal.get("holidays") or []:
        try:
            d = date.fromisoformat(str(row["date"]))
        except ValueError:
            continue
        if d >= start:
            upcoming.append(row)
    return {
        "today": today,
        "session": session_status(),
        "fetched_at": cal.get("fetched_at"),
        "source": cal.get("source"),
        "primary_segment": cal.get("primary_segment", DEFAULT_SEGMENT),
        "holiday_count": cal.get("holiday_count", len(cal.get("holidays") or [])),
        "upcoming_holidays": upcoming[:8],
        "calendar_file": str(CALENDAR_PATH),
    }


def _selftest() -> None:
    global CALENDAR_PATH
    original_path = CALENDAR_PATH
    original_cache = dict(_memory_cache)
    try:
        assert _parse_nse_date("26-Jan-2026") == date(2026, 1, 26)
        assert _parse_nse_date("2026-01-26") == date(2026, 1, 26)
        assert _parse_nse_date("") is None
        assert _parse_nse_date("garbage") is None

        # No cached file -> load_calendar falls back to the empty-note payload
        # when auto_refresh can't reach NSE (no network guaranteed in this env).
        import tempfile

        CALENDAR_PATH = Path(tempfile.mkdtemp(prefix="nse-calendar-selftest-")) / "nse_trading_calendar.json"
        _memory_cache["loaded_at"] = ""
        _memory_cache["payload"] = None
        cal = load_calendar(auto_refresh=False)
        assert cal["holiday_count"] == 0
        assert cal["holidays"] == []

        # Seed a fixture calendar directly (bypassing the live NSE fetch), and
        # pin the memory cache to "loaded today" so every load_calendar() call
        # below hits the cache fast path instead of attempting a live NSE fetch.
        fixture = {
            "source": NSE_HOLIDAY_URL,
            "fetched_at": "2026-07-20 09:00:00 IST",
            "primary_segment": "FO",
            "holiday_count": 1,
            "holidays": [
                {"date": "2026-08-19", "weekday": "Wed", "description": "Ganesh Chaturthi", "segment": "FO"},
            ],
        }
        _save_file(fixture)
        _memory_cache["loaded_at"] = ist_now().isoformat()
        _memory_cache["payload"] = fixture
        loaded = load_calendar(auto_refresh=False)
        assert loaded["holiday_count"] == 1

        assert holiday_dates() == {"2026-08-19"}
        assert holiday_on(date(2026, 8, 19)) is not None
        assert holiday_on(date(2026, 8, 18)) is None

        assert is_weekend(date(2026, 7, 25)) is True  # Saturday
        assert is_weekend(date(2026, 7, 21)) is False  # Tuesday

        assert is_trading_day(date(2026, 7, 21)) is True
        assert is_trading_day(date(2026, 8, 19)) is False  # named weekday holiday

        nxt = next_trading_day(date(2026, 8, 19))
        assert nxt == date(2026, 8, 20)  # 19th holiday (Wed) -> Thursday 20th

        prev = previous_trading_day(date(2026, 8, 19))
        assert prev == date(2026, 8, 18)  # 19th holiday (Wed) -> Tuesday 18th

        status = day_status(date(2026, 8, 19))
        assert status["status"] == "HOLIDAY"
        assert status["holiday_name"] == "Ganesh Chaturthi"

        status_trading = day_status(date(2026, 7, 21))
        assert status_trading["status"] == "TRADING DAY"

        session = session_status(datetime(2026, 7, 21, 10, 0, tzinfo=IST))
        assert session["session_phase"] == "CASH_SESSION"
        assert session["entries_allowed"] is True

        session_closed = session_status(datetime(2026, 8, 15, 10, 0, tzinfo=IST))
        assert session_closed["session_phase"] == "CLOSED"
        assert session_closed["entries_allowed"] is False

        session_preopen = session_status(datetime(2026, 7, 21, 8, 30, tzinfo=IST))
        assert session_preopen["session_phase"] == "PRE_OPEN"
        assert session_preopen["entries_allowed"] is False

        summary = calendar_summary()
        assert summary["holiday_count"] == 1
        assert "session" in summary
    finally:
        CALENDAR_PATH = original_path
        _memory_cache.clear()
        _memory_cache.update(original_cache)

    print("[sources.nse_trading_calendar] selftest OK: date parsing, cache load, trading-day math, session phases")


if __name__ == "__main__":
    _selftest()
