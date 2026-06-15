#!/usr/bin/env python3
"""
GIFT Nifty (NSE IX) live quote client.

Uses the public NSE IX market-rate API for NIFTY index futures and NSE India
for the cash reference close. Works after India cash close during GIFT session 2
(~16:35–02:45 IST) and pre-market session 1 (~06:30–15:40 IST).
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests

IST = ZoneInfo("Asia/Kolkata")
NSEIX_BASE = "https://www.nseix.com"
NSE_HOME = "https://www.nseindia.com"

GIFT_SESSIONS = (
    {"id": "gift_session_1", "label": "GIFT session 1", "start": time(6, 30), "end": time(15, 40)},
    {"id": "gift_session_2", "label": "GIFT session 2 (post-India)", "start": time(16, 35), "end": time(2, 45)},
)


def _parse_price(value: Any) -> Optional[float]:
    if value in (None, "", "-"):
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _parse_expiry(value: str) -> datetime:
    return datetime.strptime(value.strip(), "%d-%b-%Y")


def gift_session_status(now: Optional[datetime] = None) -> Dict[str, Any]:
    now_ist = (now or datetime.now(IST)).astimezone(IST)
    clock = now_ist.time()
    active_label = "CLOSED"
    active_id = None
    for session in GIFT_SESSIONS:
        start = session["start"]
        end = session["end"]
        if start <= end:
            if start <= clock <= end:
                active_label = session["label"]
                active_id = session["id"]
                break
        else:
            # crosses midnight (session 2)
            if clock >= start or clock <= end:
                active_label = session["label"]
                active_id = session["id"]
                break
    return {
        "status": "OPEN" if active_id else "CLOSED",
        "active_session": active_id,
        "active_session_label": active_label,
        "ist_time": now_ist.strftime("%Y-%m-%d %H:%M:%S IST"),
    }


def nseix_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Referer": f"{NSEIX_BASE}/",
            "Origin": NSEIX_BASE,
        }
    )
    session.get(NSEIX_BASE, timeout=15)
    return session


def nse_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Referer": NSE_HOME,
        }
    )
    session.get(NSE_HOME, timeout=15)
    return session


def fetch_gift_nifty_futures(session: Optional[requests.Session] = None) -> List[Dict[str, Any]]:
    client = session or nseix_session()
    response = client.get(f"{NSEIX_BASE}/api/market-rate?type=derivative", timeout=20)
    response.raise_for_status()
    rows = response.json().get("data") or []
    futures: List[Dict[str, Any]] = []
    for row in rows:
        if row.get("SYMBOL") != "NIFTY" or row.get("INSTRUMENTTYPE") != "FUTIDX":
            continue
        futures.append(
            {
                "symbol": row.get("SYMBOL"),
                "expiry": row.get("EXPIRYDATE"),
                "last_price": _parse_price(row.get("LASTPRICE")),
                "day_change": _parse_price(row.get("DAYCHANGE_1", row.get("DAYCHANGE"))),
                "pct_change": _parse_price(str(row.get("PERCHANGE", "")).replace("%", "")),
                "contracts_traded": int(str(row.get("CONTRACTSTRADED") or 0).replace(",", "") or 0),
                "exchange_time": row.get("TIMESTMP"),
                "token": row.get("TOKEN_NMBR"),
            }
        )
    futures.sort(key=lambda item: _parse_expiry(str(item["expiry"])))
    return futures


def pick_front_month(futures: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return futures[0] if futures else None


def fetch_nse_nifty_cash(session: Optional[requests.Session] = None) -> Dict[str, Any]:
    client = session or nse_session()
    response = client.get(f"{NSE_HOME}/api/allIndices", timeout=20)
    response.raise_for_status()
    for row in response.json().get("data") or []:
        if row.get("indexSymbol") == "NIFTY 50" or row.get("index") == "NIFTY 50":
            last = _parse_price(row.get("last"))
            prev_close = _parse_price(row.get("previousClose"))
            return {
                "index": "NIFTY 50",
                "last": last,
                "previous_close": prev_close,
                "open": _parse_price(row.get("open")),
                "high": _parse_price(row.get("high")),
                "low": _parse_price(row.get("low")),
                "pct_change": _parse_price(row.get("percentChange")),
                "previous_day": row.get("previousDay"),
            }
    return {}


def build_gift_nifty_snapshot(
    nseix: Optional[requests.Session] = None,
    nse: Optional[requests.Session] = None,
) -> Dict[str, Any]:
    session_info = gift_session_status()
    errors: List[str] = []
    futures: List[Dict[str, Any]] = []
    cash: Dict[str, Any] = {}
    try:
        nseix = nseix or nseix_session()
        futures = fetch_gift_nifty_futures(nseix)
    except Exception as exc:
        errors.append(f"gift_fetch: {exc}")
    try:
        nse = nse or nse_session()
        cash = fetch_nse_nifty_cash(nse)
    except Exception as exc:
        errors.append(f"nse_cash: {exc}")

    front = pick_front_month(futures)
    gift_last = front.get("last_price") if front else None
    # Premium vs yesterday's official cash close — not today's stale NSE "last" at open.
    ref_close = cash.get("previous_close") or cash.get("last")
    premium = round(gift_last - ref_close, 2) if gift_last is not None and ref_close else None
    premium_pct = round((premium / ref_close) * 100, 3) if premium is not None and ref_close else None

    bias = "FLAT"
    if premium is not None:
        if premium >= 30:
            bias = "GAP_UP"
        elif premium <= -30:
            bias = "GAP_DOWN"
        elif premium >= 10:
            bias = "MILD_UP"
        elif premium <= -10:
            bias = "MILD_DOWN"

    return {
        "generated_at": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
        "source": "nseix_market_rate",
        "session": session_info,
        "front_month": front,
        "all_futures": futures,
        "nse_cash": cash,
        "premium_vs_nse_close": premium,
        "premium_pct": premium_pct,
        "overnight_bias": bias,
        "errors": errors,
        "reference_close": ref_close,
        "note": (
            "GIFT Nifty front-month future vs NSE Nifty 50 previous cash close. "
            "Positive premium suggests gap-up opening bias; negative suggests gap-down. "
            "After 9:15 IST use Kite cash open for actual gap — GIFT premium is pre-open context only."
        ),
    }
