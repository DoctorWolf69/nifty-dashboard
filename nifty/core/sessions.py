#!/usr/bin/env python3
"""
Global session timeline and NIFTY technical levels for retail-aware desk context.

Sessions are converted to IST automatically (London/NY/Japan local opens).
EMA levels use NIFTY 50 daily closes from Kite when credentials are available.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from nifty.sources.gift import build_gift_nifty_snapshot

IST = ZoneInfo("Asia/Kolkata")
LONDON = ZoneInfo("Europe/London")
NY = ZoneInfo("America/New_York")
TOKYO = ZoneInfo("Asia/Tokyo")
BERLIN = ZoneInfo("Europe/Berlin")

NIFTY_SPOT_TOKEN = 256265
EMA_PERIODS = (20, 50, 100, 200)
TECH_LEVEL_TOLERANCE = 60.0


def ema_last(closes: List[float], period: int) -> Optional[float]:
    if len(closes) < period:
        return None
    multiplier = 2 / (period + 1)
    value = sum(closes[:period]) / period
    for price in closes[period:]:
        value = (price * multiplier) + (value * (1 - multiplier))
    return round(value, 2)


def _combine_local(day: datetime, local_time: time, tz: ZoneInfo) -> datetime:
    return datetime.combine(day.date(), local_time, tzinfo=tz).astimezone(IST)


def _session_status(
    now_ist: datetime,
    start_ist: datetime,
    window_minutes: int,
) -> Tuple[str, int]:
    lead = timedelta(minutes=max(15, window_minutes // 2))
    active_end = start_ist + timedelta(minutes=window_minutes)
    active_start = start_ist - lead
    if now_ist < active_start:
        return "UPCOMING", int((active_start - now_ist).total_seconds() // 60)
    if now_ist <= active_end:
        return "ACTIVE", 0
    return "PASSED", int((now_ist - active_end).total_seconds() // 60)


SESSION_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "id": "tokyo_open",
        "label": "Tokyo cash open",
        "tz": TOKYO,
        "local_time": time(9, 0),
        "window_minutes": 30,
        "note": "Asia lead — affects early GIFT sentiment",
    },
    {
        "id": "gift_pre",
        "label": "GIFT / pre-market",
        "tz": IST,
        "local_time": time(6, 30),
        "window_minutes": 45,
        "note": "Overnight gap context before India open",
    },
    {
        "id": "india_orb",
        "label": "India ORB window",
        "tz": IST,
        "local_time": time(9, 15),
        "window_minutes": 15,
        "note": "Opening range — retail + algo battleground",
    },
    {
        "id": "india_first_hour",
        "label": "India first hour",
        "tz": IST,
        "local_time": time(9, 15),
        "window_minutes": 60,
        "note": "Highest cash volume — ORB + participant footprint",
    },
    {
        "id": "london_open",
        "label": "London cash open",
        "tz": LONDON,
        "local_time": time(8, 0),
        "window_minutes": 30,
        "note": "European flow — retail watches FTSE/DAX reaction",
    },
    {
        "id": "eu_open",
        "label": "Frankfurt / EU cash open",
        "tz": BERLIN,
        "local_time": time(9, 0),
        "window_minutes": 30,
        "note": "EU equity open — global risk tone check",
    },
    {
        "id": "ny_pre",
        "label": "US pre-market",
        "tz": NY,
        "local_time": time(4, 0),
        "window_minutes": 60,
        "note": "US futures positioning before cash",
    },
    {
        "id": "ny_open",
        "label": "NY cash open",
        "tz": NY,
        "local_time": time(9, 30),
        "window_minutes": 30,
        "note": "Global risk reset — sets next-session GIFT tone",
    },
    {
        "id": "india_last_hour",
        "label": "India last hour",
        "tz": IST,
        "local_time": time(14, 30),
        "window_minutes": 60,
        "note": "Expiry gamma, institutional roll, retail chase risk",
    },
    {
        "id": "india_close",
        "label": "India close",
        "tz": IST,
        "local_time": time(15, 30),
        "window_minutes": 15,
        "note": "Closing auction — block fresh entries",
    },
]


def build_session_timeline(now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    now_ist = (now or datetime.now(IST)).astimezone(IST)
    rows: List[Dict[str, Any]] = []
    for definition in SESSION_DEFINITIONS:
        start_ist = _combine_local(now_ist, definition["local_time"], definition["tz"])
        status, minute_offset = _session_status(now_ist, start_ist, int(definition["window_minutes"]))
        rows.append(
            {
                "id": definition["id"],
                "label": definition["label"],
                "ist_time": start_ist.strftime("%H:%M IST"),
                "start_ist": start_ist.isoformat(),
                "status": status,
                "minutes_until" if status == "UPCOMING" else "minutes_since": minute_offset,
                "window_minutes": definition["window_minutes"],
                "note": definition["note"],
            }
        )
    rows.sort(key=lambda row: row["start_ist"])
    return rows


def fetch_nifty_technical_levels(kite: Any, spot: float = 0.0) -> Dict[str, Any]:
    empty = {f"ema_{period}": None for period in EMA_PERIODS}
    empty["source"] = "unavailable"
    empty["as_of"] = None
    if kite is None:
        return empty
    try:
        to_date = datetime.now()
        from_date = to_date - timedelta(days=420)
        candles = kite.historical_data(
            NIFTY_SPOT_TOKEN,
            from_date,
            to_date,
            "day",
            continuous=False,
            oi=False,
        )
        closes = [float(row["close"]) for row in sorted(candles, key=lambda row: row["date"])]
        if not closes:
            return empty
        levels: Dict[str, Any] = {
            "source": "kite_daily",
            "as_of": str(candles[-1]["date"])[:10],
            "last_close": round(closes[-1], 2),
        }
        for period in EMA_PERIODS:
            value = ema_last(closes, period)
            levels[f"ema_{period}"] = value
            if value is not None and spot > 0:
                levels[f"ema_{period}_dist"] = round(spot - value, 2)
                levels[f"ema_{period}_dist_pct"] = round((spot - value) / value * 100, 3)
        return levels
    except Exception as exc:
        fallback = dict(empty)
        fallback["source"] = "error"
        fallback["error"] = str(exc)
        return fallback


def technical_level_labels(levels: Dict[str, Any]) -> List[Tuple[str, float]]:
    output: List[Tuple[str, float]] = []
    for period in EMA_PERIODS:
        key = f"ema_{period}"
        value = levels.get(key)
        if value:
            output.append((f"EMA {period}", float(value)))
    return output


def near_technical_level(strike: float, levels: Dict[str, Any], tolerance: float = TECH_LEVEL_TOLERANCE) -> List[str]:
    reasons: List[str] = []
    for label, value in technical_level_labels(levels):
        if abs(strike - value) <= tolerance:
            reasons.append(label.lower())
    return reasons


def fetch_india_vix() -> Dict[str, Any]:
    try:
        import requests

        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": "Mozilla/5.0",
                "Accept": "*/*",
                "Referer": "https://www.nseindia.com/",
            }
        )
        session.get("https://www.nseindia.com", timeout=15)
        response = session.get("https://www.nseindia.com/api/allIndices", timeout=20)
        response.raise_for_status()
        for row in response.json().get("data") or []:
            if str(row.get("indexSymbol", "")).upper() == "INDIA VIX":
                return {
                    "last": row.get("last"),
                    "open": row.get("open"),
                    "high": row.get("high"),
                    "low": row.get("low"),
                    "previous_close": row.get("previousClose"),
                    "percent_change": row.get("percentChange"),
                    "source": "nse_allIndices",
                }
    except Exception as exc:
        return {"source": "error", "error": str(exc)}
    return {"source": "unavailable"}


def build_session_context(kite: Any, spot: float = 0.0, now: Optional[datetime] = None) -> Dict[str, Any]:
    sessions = build_session_timeline(now=now)
    technical_levels = fetch_nifty_technical_levels(kite, spot=spot)
    vix_detail = fetch_india_vix()
    technical_levels["india_vix"] = vix_detail.get("last")
    technical_levels["india_vix_detail"] = vix_detail
    active = [row for row in sessions if row["status"] == "ACTIVE"]
    upcoming = [row for row in sessions if row["status"] == "UPCOMING"]
    next_session = active[0] if active else (upcoming[0] if upcoming else None)
    return {
        "generated_at": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
        "sessions": sessions,
        "active_sessions": active,
        "next_session": next_session,
        "technical_levels": technical_levels,
        "gift_nifty": build_gift_nifty_snapshot(),
        "retail_notes": [
            "Retail clusters at round strikes, ORB levels, and daily EMAs.",
            "London and NY opens often trigger volatility even in India afternoon.",
            "Treat EMA 100/200 as magnet levels — watch OI velocity when spot approaches.",
        ],
    }
