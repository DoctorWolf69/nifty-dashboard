#!/usr/bin/env python3
"""
Optional FinStack data bridge for desk morning captures.

Uses the installed finstack package when available; falls back to yfinance for
index/commodity symbols FinStack rejects (^GSPC, CL=F, etc.).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("finstack_bridge")

_FINSTACK_AVAILABLE = False

try:
    from finstack.briefs import get_morning_fno_brief
    from finstack.data.analytics import get_sector_performance
    from finstack.data.global_markets import get_forex_rate, get_global_quote
    from finstack.data.market_intelligence import get_options_oi_analytics
    from finstack.data.probability import get_nifty_outlook

    _FINSTACK_AVAILABLE = True
except ImportError:
    get_morning_fno_brief = None  # type: ignore
    get_sector_performance = None  # type: ignore
    get_forex_rate = None  # type: ignore
    get_global_quote = None  # type: ignore
    get_options_oi_analytics = None  # type: ignore
    get_nifty_outlook = None  # type: ignore


def finstack_available() -> bool:
    return _FINSTACK_AVAILABLE


def _safe_call(fn: Optional[Callable[..., Any]], *args: Any, **kwargs: Any) -> Dict[str, Any]:
    if fn is None:
        return {"error": "finstack_not_installed"}
    try:
        result = fn(*args, **kwargs)
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
                return parsed if isinstance(parsed, dict) else {"result": parsed}
            except json.JSONDecodeError:
                return {"text": result}
        if isinstance(result, dict):
            return result
        return {"result": result}
    except Exception as exc:
        logger.warning("FinStack call failed (%s): %s", getattr(fn, "__name__", "fn"), exc)
        return {"error": str(exc)}


def fetch_yfinance_quote(symbol: str) -> Dict[str, Any]:
    try:
        import yfinance as yf

        ticker = yf.Ticker(symbol)
        info = ticker.info or {}
        price = info.get("regularMarketPrice") or info.get("currentPrice")
        if price is None:
            hist = ticker.history(period="5d")
            if hist.empty:
                return {"error": f"no_yfinance_data:{symbol}"}
            price = float(hist["Close"].iloc[-1])
            prev = float(hist["Close"].iloc[-2]) if len(hist) > 1 else price
            change = price - prev
            change_pct = (change / prev) * 100 if prev else 0
            return {
                "symbol": symbol,
                "name": symbol,
                "price": round(price, 4),
                "change": round(change, 4),
                "change_pct": round(change_pct, 4),
                "source": "yfinance_history",
            }
        prev_close = info.get("regularMarketPreviousClose") or price
        change = (price or 0) - (prev_close or 0)
        change_pct = info.get("regularMarketChangePercent")
        if change_pct is None and prev_close:
            change_pct = (change / prev_close) * 100
        return {
            "symbol": symbol,
            "name": info.get("shortName") or info.get("longName") or symbol,
            "price": price,
            "change": info.get("regularMarketChange", change),
            "change_pct": change_pct,
            "open": info.get("regularMarketOpen"),
            "high": info.get("regularMarketDayHigh"),
            "low": info.get("regularMarketDayLow"),
            "prev_close": prev_close,
            "source": "yfinance",
        }
    except Exception as exc:
        return {"error": str(exc), "symbol": symbol}


def fetch_stock_quote(symbol: str) -> Dict[str, Any]:
    if any(ch in symbol for ch in ("^", "=", "-")):
        row = fetch_yfinance_quote(symbol)
        if not row.get("error"):
            return row
    row = _safe_call(get_global_quote, symbol)
    if row.get("error"):
        fallback = fetch_yfinance_quote(symbol)
        if not fallback.get("error"):
            return fallback
    return row


def fetch_forex(from_currency: str, to_currency: str = "INR") -> Dict[str, Any]:
    row = _safe_call(get_forex_rate, from_currency, to_currency)
    if row.get("error"):
        pair = f"{from_currency}{to_currency}=X"
        yf_row = fetch_yfinance_quote(pair)
        if not yf_row.get("error"):
            return {"pair": pair, "rate": yf_row.get("price"), "change_pct": yf_row.get("change_pct"), "source": "yfinance"}
    return row


def fetch_sector_performance() -> Dict[str, Any]:
    return _safe_call(get_sector_performance)


def fetch_nifty_outlook() -> Dict[str, Any]:
    return _safe_call(get_nifty_outlook)


def fetch_options_oi_analytics(symbol: str) -> Dict[str, Any]:
    return _safe_call(get_options_oi_analytics, symbol.upper())


def fetch_morning_fno_brief() -> Dict[str, Any]:
    return _safe_call(get_morning_fno_brief)


def fetch_global_quotes(symbols: List[str]) -> Dict[str, Any]:
    quotes: Dict[str, Any] = {}
    errors: List[str] = []
    for symbol in symbols:
        row = fetch_stock_quote(symbol)
        if row.get("error"):
            errors.append(f"{symbol}: {row.get('error')}")
        quotes[symbol] = row
    return {"quotes": quotes, "errors": errors}
