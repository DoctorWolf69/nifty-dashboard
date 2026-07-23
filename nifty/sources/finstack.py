#!/usr/bin/env python3
"""
Optional FinStack data bridge for desk morning captures.

Uses the installed finstack package when available; falls back to yfinance for
index/commodity symbols FinStack rejects (^GSPC, CL=F, etc.).

_patch_finstack_data_sources() (ported from quant-desk-engine v4/ATLAS's
finstack_bridge.py, no logic changed beyond import paths) patches FinStack's
broken Yahoo G-Sec/VIX tickers with nifty.sources.gsec_yield/india_vix data
and adds a 120s cache to get_nifty_outlook. It is a no-op whenever the
optional finstack package isn't installed (_FINSTACK_AVAILABLE is False) -
which is the case in this environment today - so nifty.morning.phases's
live derive_india_bias (which consumes fetch_nifty_outlook()'s output) sees
no behavior change until/unless finstack is actually installed. Only wired
in the same 3 call sites the source wires it into.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("finstack_bridge")

_FINSTACK_AVAILABLE = False
_FINSTACK_PATCHED = False
_OUTLOOK_CACHE: Dict[str, Any] = {}
_OUTLOOK_CACHE_AT = 0.0
_OUTLOOK_CACHE_TTL_SEC = 120

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


def _fetch_nse_vix_last() -> Optional[float]:
    try:
        import requests

        from nifty.sources.india_vix import fetch_live_india_vix

        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
                ),
                "Accept": "*/*",
                "Referer": "https://www.nseindia.com",
            }
        )
        session.get("https://www.nseindia.com", timeout=15)
        row = fetch_live_india_vix(session)
        last = row.get("last")
        return round(float(last), 3) if last is not None else None
    except Exception as exc:
        logger.debug("NSE India VIX fallback failed: %s", exc)
        return None


def _patch_finstack_data_sources() -> None:
    global _FINSTACK_PATCHED
    if _FINSTACK_PATCHED or not _FINSTACK_AVAILABLE:
        return

    try:
        from finstack.data import probability as prob

        if not getattr(prob, "_desk_gsec_patched", False):
            original_gsec = prob._get_gsec_10y

            def _desk_gsec_10y() -> Optional[float]:
                from nifty.sources.gsec_yield import india_gsec_10y_scalar

                yield_pct = india_gsec_10y_scalar()
                if yield_pct is not None:
                    return yield_pct
                return original_gsec()

            prob._get_gsec_10y = _desk_gsec_10y
            prob._desk_gsec_patched = True

        if not getattr(prob, "_desk_vix_patched", False):
            original_vix = prob._get_vix

            def _desk_vix() -> Optional[float]:
                nse_vix = _fetch_nse_vix_last()
                if nse_vix is not None:
                    return nse_vix
                return original_vix()

            prob._get_vix = _desk_vix
            prob._desk_vix_patched = True

        if not getattr(prob, "_desk_outlook_cached", False):
            original_outlook = prob.get_nifty_outlook

            def _cached_get_nifty_outlook() -> Dict[str, Any]:
                global _OUTLOOK_CACHE_AT
                now = time.time()
                if _OUTLOOK_CACHE and (now - _OUTLOOK_CACHE_AT) < _OUTLOOK_CACHE_TTL_SEC:
                    return dict(_OUTLOOK_CACHE)
                result = original_outlook()
                if isinstance(result, dict):
                    _OUTLOOK_CACHE.clear()
                    _OUTLOOK_CACHE.update(result)
                    _OUTLOOK_CACHE_AT = now
                return result

            prob.get_nifty_outlook = _cached_get_nifty_outlook
            prob._desk_outlook_cached = True
    except Exception as exc:
        logger.warning("FinStack data-source patch failed: %s", exc)

    _FINSTACK_PATCHED = True


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
    _patch_finstack_data_sources()
    return _safe_call(get_sector_performance)


def fetch_nifty_outlook() -> Dict[str, Any]:
    _patch_finstack_data_sources()
    try:
        from finstack.data.probability import get_nifty_outlook as outlook_fn
    except ImportError:
        return {"error": "finstack_not_installed"}
    return _safe_call(outlook_fn)


def fetch_options_oi_analytics(symbol: str) -> Dict[str, Any]:
    return _safe_call(get_options_oi_analytics, symbol.upper())


def fetch_morning_fno_brief() -> Dict[str, Any]:
    _patch_finstack_data_sources()
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


def _selftest() -> None:
    global _FINSTACK_PATCHED
    original_patched = _FINSTACK_PATCHED
    try:
        # finstack isn't installed in this environment -> _FINSTACK_AVAILABLE is False,
        # so the patch must be a guaranteed no-op (never raises, never flips the flag).
        assert _FINSTACK_AVAILABLE is False
        _FINSTACK_PATCHED = False
        _patch_finstack_data_sources()
        assert _FINSTACK_PATCHED is False

        # fetch_* functions call the patch first; without finstack installed they
        # must still degrade to the same "not installed" error dict as before.
        assert fetch_sector_performance() == {"error": "finstack_not_installed"}
        assert fetch_nifty_outlook() == {"error": "finstack_not_installed"}
        assert fetch_morning_fno_brief() == {"error": "finstack_not_installed"}
        assert fetch_options_oi_analytics("NIFTY") == {"error": "finstack_not_installed"}

        # _fetch_nse_vix_last degrades to None on any failure rather than raising
        # (network access is not guaranteed in this environment).
        result = _fetch_nse_vix_last()
        assert result is None or isinstance(result, float)
    finally:
        _FINSTACK_PATCHED = original_patched

    print("[sources.finstack] selftest OK: patch is a no-op without finstack installed, fetch_* degrade gracefully")


if __name__ == "__main__":
    _selftest()
