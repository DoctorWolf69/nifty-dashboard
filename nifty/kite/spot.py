#!/usr/bin/env python3
"""Kite NIFTY spot helpers for morning desk capture (authoritative at cash open)."""

from __future__ import annotations

from datetime import datetime, time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
NSE_NIFTY_SYMBOL = "NSE:NIFTY 50"
GAP_THRESHOLD = 30.0
MILD_GAP_THRESHOLD = 10.0

from nifty.paths import PROJECT_ROOT as BASE_DIR


def _as_float(value: Any) -> Optional[float]:
    try:
        if value in (None, "", 0, "0"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def cash_session_open(now: Optional[datetime] = None) -> bool:
    now_ist = (now or datetime.now(IST)).astimezone(IST)
    clock = now_ist.time()
    if now_ist.weekday() >= 5:
        return False
    return time(9, 15) <= clock <= time(15, 30)


def premarket_open_window(now: Optional[datetime] = None) -> bool:
    """NSE pre-open auction window (9:00–9:14) — Kite last is indicative gap."""
    now_ist = (now or datetime.now(IST)).astimezone(IST)
    clock = now_ist.time()
    if now_ist.weekday() >= 5:
        return False
    return time(9, 0) <= clock < time(9, 15)


def load_kite_optional():
    try:
        from dotenv import load_dotenv
        from kiteconnect import KiteConnect
        import os

        load_dotenv(BASE_DIR / ".env")
        api_key = os.environ.get("KITE_API_KEY", "").strip()
        access_token = os.environ.get("KITE_ACCESS_TOKEN", "").strip()
        if not api_key or not access_token:
            return None
        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(access_token)
        return kite
    except Exception:
        return None


def fetch_kite_nifty_quote() -> Optional[Dict[str, Any]]:
    kite = load_kite_optional()
    if kite is None:
        return None
    try:
        raw = kite.quote([NSE_NIFTY_SYMBOL]).get(NSE_NIFTY_SYMBOL) or {}
        ohlc = raw.get("ohlc") or {}
        last = _as_float(raw.get("last_price"))
        open_ = _as_float(ohlc.get("open"))
        high = _as_float(ohlc.get("high"))
        low = _as_float(ohlc.get("low"))
        prev_close = _as_float(ohlc.get("close"))
        if last is None and prev_close is None:
            return None
        return {
            "last": last,
            "open": open_,
            "high": high,
            "low": low,
            "previous_close": prev_close,
            "source": "kite",
            "symbol": NSE_NIFTY_SYMBOL,
        }
    except Exception as exc:
        return {"error": str(exc), "source": "kite"}


def classify_open_gap(
    spot: Optional[float],
    open_: Optional[float],
    prev_close: Optional[float],
    *,
    threshold: float = GAP_THRESHOLD,
    mild_threshold: float = MILD_GAP_THRESHOLD,
) -> Dict[str, Any]:
    if prev_close is None or prev_close <= 0:
        return {"gap_pts": None, "gap_pct": None, "gap_type": "UNKNOWN", "reference_open": None}
    reference_open = open_ if open_ and open_ > 0 else spot
    if reference_open is None or reference_open <= 0:
        return {"gap_pts": None, "gap_pct": None, "gap_type": "UNKNOWN", "reference_open": None}
    gap_pts = round(reference_open - prev_close, 2)
    gap_pct = round((gap_pts / prev_close) * 100, 2)
    if gap_pts <= -threshold:
        gap_type = "GAP_DOWN"
    elif gap_pts >= threshold:
        gap_type = "GAP_UP"
    elif gap_pts <= -mild_threshold:
        gap_type = "MILD_DOWN"
    elif gap_pts >= mild_threshold:
        gap_type = "MILD_UP"
    else:
        gap_type = "FLAT"
    return {
        "gap_pts": gap_pts,
        "gap_pct": gap_pct,
        "gap_type": gap_type,
        "reference_open": reference_open,
    }


def _parse_nse_index_row(row: Dict[str, Any]) -> Dict[str, Any]:
    last = _as_float(row.get("last"))
    open_ = _as_float(row.get("open"))
    high = _as_float(row.get("high"))
    low = _as_float(row.get("low"))
    prev_close = _as_float(row.get("previousClose"))
    pct = _as_float(row.get("percentChange"))
    return {
        "last": last,
        "open": open_,
        "high": high,
        "low": low,
        "previous_close": prev_close,
        "percent_change": pct,
        "source": "nse_allIndices",
        "previous_day": row.get("previousDay"),
        "raw_last": last,
        "raw_open": open_,
    }


def merge_nifty_snapshot(
    nse_row: Dict[str, Any],
    kite_quote: Optional[Dict[str, Any]] = None,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Prefer Kite for live OHLC when cash is open or NSE returns zero open."""
    nse = _parse_nse_index_row(nse_row)
    kite = None if not kite_quote or kite_quote.get("error") else kite_quote
    nse_open_missing = not nse.get("open")
    session_open = cash_session_open(now)
    pre_open = premarket_open_window(now)

    merged = dict(nse)
    merged["data_quality"] = "ok"
    merged["primary_source"] = "nse_allIndices"

    if kite:
        merged["kite"] = {
            "last": kite.get("last"),
            "open": kite.get("open"),
            "high": kite.get("high"),
            "low": kite.get("low"),
            "previous_close": kite.get("previous_close"),
        }
        use_kite = session_open or nse_open_missing or pre_open
        if use_kite:
            for field in ("last", "open", "high", "low", "previous_close"):
                kite_val = kite.get(field if field != "previous_close" else "previous_close")
                if kite_val is not None and kite_val > 0:
                    merged[field] = kite_val
            merged["primary_source"] = "kite"
            if nse_open_missing and session_open:
                merged["data_quality"] = "nse_stale_kite_used"
            elif nse_open_missing:
                merged["data_quality"] = "nse_open_missing_kite_used"

    if not merged.get("open") and merged.get("last") and merged.get("previous_close"):
        if session_open and merged.get("primary_source") == "nse_allIndices":
            merged["data_quality"] = "nse_open_missing_no_kite"

    if merged.get("last") and merged.get("previous_close"):
        merged["percent_change"] = round(
            ((merged["last"] - merged["previous_close"]) / merged["previous_close"]) * 100,
            2,
        )

    merged["open_gap"] = classify_open_gap(
        merged.get("last"),
        merged.get("open"),
        merged.get("previous_close"),
    )
    return merged


def prior_session_candle(
    candles: list,
    trade_date: Optional[Any] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not candles:
        return None, None
    if trade_date is not None:
        td = trade_date.isoformat() if hasattr(trade_date, "isoformat") else str(trade_date)[:10]
        prior = [c for c in candles if str(c.get("date", ""))[:10] < td]
        if prior:
            row = prior[-1]
            return row, str(row.get("date", ""))[:10]
    if len(candles) >= 2:
        row = candles[-2]
        return row, str(row.get("date", ""))[:10]
    row = candles[-1]
    return row, str(row.get("date", ""))[:10]
