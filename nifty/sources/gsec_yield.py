#!/usr/bin/env python3
"""India 10Y G-Sec yield — RBI/FBIL table scrape with static fallback.

Ported faithfully from quant-desk-engine v4/ATLAS's nifty_gsec_yield.py
(mentor-authored). No logic changed. Fully self-contained in the source
too (only stdlib re/time + requests), so no import adaptation was needed.

Genuinely new data point — nifty-dashboard has no G-Sec yield fetcher.
1-hour in-process cache; falls back to a static reference yield
(REFERENCE_YIELD_PCT) if RBI's NSDP page is unreachable or its table shape
changes, so callers always get a usable number rather than None.

Not yet wired into the live pipeline.
Self-check: python -m nifty.sources.gsec_yield
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, Optional

import requests

RBI_NSDP_URL = "https://rbi.org.in/Scripts/BS_NSDPDisplay.aspx?param=4"
GSEC_10Y_LABEL = "10-Year G-Sec Par Yield (FBIL)"
REFERENCE_YIELD_PCT = 6.85

_CACHE: Dict[str, Any] = {}
_CACHE_AT = 0.0
_CACHE_TTL_SEC = 3600


def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_rbi_10y_gsec_yield(html: str) -> Optional[float]:
    """Parse the latest 10Y G-Sec par yield from RBI's NSDP statistics page."""
    label = GSEC_10Y_LABEL.lower()
    for tr in re.finditer(r"<tr[^>]*>(.*?)</tr>", html, re.I | re.S):
        row = tr.group(1)
        if label not in row.lower():
            continue
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.I | re.S)
        nums: list[float] = []
        for cell in cells[1:]:
            clean = re.sub(r"<[^>]+>", "", cell).strip()
            if re.fullmatch(r"\d+\.\d{2}", clean):
                nums.append(float(clean))
        if nums:
            return nums[-1]
    return None


def fetch_rbi_10y_gsec_yield(session: Optional[requests.Session] = None) -> Dict[str, Any]:
    """Fetch India 10Y benchmark G-Sec yield from RBI/FBIL NSDP table."""
    own_session = session is None
    session = session or requests.Session()
    if own_session:
        session.headers.setdefault("User-Agent", "Mozilla/5.0 (compatible; quant-desk-engine/1.0)")

    try:
        response = session.get(RBI_NSDP_URL, timeout=30)
        response.raise_for_status()
        yield_pct = parse_rbi_10y_gsec_yield(response.text)
        if yield_pct is None:
            return {"error": "rbi_10y_row_not_found", "source": "rbi_nsdp"}
        return {
            "yield_pct": round(yield_pct, 3),
            "label": GSEC_10Y_LABEL,
            "source": "rbi_nsdp_fbil",
            "url": RBI_NSDP_URL,
        }
    except Exception as exc:
        return {"error": str(exc), "source": "rbi_nsdp"}


def fetch_india_gsec_10y_yield(force_refresh: bool = False) -> Dict[str, Any]:
    """Resolve India 10Y G-Sec yield with in-process cache and reference fallback."""
    global _CACHE_AT
    now = time.time()
    if not force_refresh and _CACHE.get("yield_pct") is not None and (now - _CACHE_AT) < _CACHE_TTL_SEC:
        return dict(_CACHE)

    row = fetch_rbi_10y_gsec_yield()
    if row.get("yield_pct") is not None:
        _CACHE.clear()
        _CACHE.update(row)
        _CACHE_AT = now
        return dict(row)

    fallback = {
        "yield_pct": REFERENCE_YIELD_PCT,
        "label": GSEC_10Y_LABEL,
        "source": "rbi_reference_fallback",
        "note": "RBI/FBIL live row unavailable; using desk reference yield",
        "upstream_error": row.get("error"),
    }
    _CACHE.clear()
    _CACHE.update(fallback)
    _CACHE_AT = now
    return dict(fallback)


def india_gsec_10y_scalar(row: Optional[Dict[str, Any]] = None) -> Optional[float]:
    payload = row if row is not None else fetch_india_gsec_10y_yield()
    return _as_float(payload.get("yield_pct"))


def _selftest() -> None:
    sample_html = """
    <table>
      <tr><td>Row Label</td><td>Header</td></tr>
      <tr><td>10-Year G-Sec Par Yield (FBIL)</td><td>label</td><td>6.72</td><td>6.85</td></tr>
    </table>
    """
    assert parse_rbi_10y_gsec_yield(sample_html) == 6.85
    assert parse_rbi_10y_gsec_yield("<table><tr><td>Nothing here</td></tr></table>") is None
    assert parse_rbi_10y_gsec_yield("") is None

    assert _as_float("6.85") == 6.85
    assert _as_float(None) is None
    assert _as_float("") is None

    # Cache: force_refresh=False with an empty cache still fetches (network call,
    # not exercised here) — but the fallback contract must always yield a usable
    # scalar even if the upstream call fails. Simulate directly via the cache dict.
    global _CACHE, _CACHE_AT
    _CACHE.clear()
    _CACHE.update({"yield_pct": 6.9, "label": GSEC_10Y_LABEL, "source": "rbi_nsdp_fbil"})
    _CACHE_AT = time.time()
    cached = fetch_india_gsec_10y_yield()
    assert cached["yield_pct"] == 6.9
    assert cached["source"] == "rbi_nsdp_fbil"  # served from cache, not re-fetched

    assert india_gsec_10y_scalar({"yield_pct": "7.01"}) == 7.01
    assert india_gsec_10y_scalar({}) is None

    print("[sources.gsec_yield] selftest OK: HTML parsing, cache reuse, scalar extraction")


if __name__ == "__main__":
    _selftest()
