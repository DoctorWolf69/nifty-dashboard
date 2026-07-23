#!/usr/bin/env python3
"""India VIX helpers — live NSE fetch, payload date validation, journal backfill.

Ported faithfully from quant-desk-engine v4/ATLAS's nifty_india_vix.py
(mentor-authored). No logic changed. Only adaptation: JOURNAL_DIR/
RAW_EOD_DIR resolve via nifty.paths, and parse_fii_date imports from
nifty.sources.fii_dii (this session's port) instead of the standalone
nifty_fii_dii module.

nifty-dashboard's existing India VIX fetch (in nifty/kite/key_levels.py's
technical_levels panel and nifty/morning/phases.py) reads the live value
directly with no date validation — this module adds the same
payload-date-match discipline as fii_dii.py, plus a 2-tier backfill
(raw NSE-EOD archive, then the daily_levels session-close snapshot) for
historical/replay use.

Not yet wired into the live pipeline.
Self-check: python -m nifty.sources.india_vix
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from nifty.paths import JOURNAL_DIR, NSE_EOD_DIR
from nifty.sources.fii_dii import parse_fii_date

RAW_EOD_DIR = NSE_EOD_DIR / "raw"
ALL_INDICES_URL = "https://www.nseindia.com/api/allIndices"


def vix_payload_trade_date(payload: Dict[str, Any]) -> Optional[date]:
    """NSE allIndices VIX row tags the session via previousDay."""
    return parse_fii_date(str(payload.get("previousDay") or payload.get("previousday") or ""))


def normalize_detail_to_nse_vix(detail: Dict[str, Any], day: date) -> Dict[str, Any]:
    day_label = day.strftime("%d-%b-%Y")
    return {
        "key": "BROAD MARKET INDICES",
        "index": "INDIA VIX",
        "indexSymbol": "INDIA VIX",
        "last": detail.get("last"),
        "open": detail.get("open"),
        "high": detail.get("high"),
        "low": detail.get("low"),
        "previousClose": detail.get("previous_close", detail.get("previousClose")),
        "percentChange": detail.get("percent_change", detail.get("percentChange")),
        "variation": detail.get("variation"),
        "previousDay": day_label,
    }


def _vix_detail_from_daily_levels(payload: Dict[str, Any]) -> Dict[str, Any]:
    for block_key in ("session_close", "orb_close"):
        block = payload.get(block_key) or {}
        detail = (
            ((block.get("session_context") or {}).get("technical_levels") or {}).get("india_vix_detail")
            or {}
        )
        if detail.get("last") is not None:
            return detail
    return {}


def backfill_india_vix(day: date) -> Optional[Tuple[Dict[str, Any], str]]:
    """Resolve historical India VIX close when the live API only returns today."""
    ddmmyyyy = day.strftime("%d%m%Y")

    if RAW_EOD_DIR.exists():
        dated_raw = RAW_EOD_DIR / day.isoformat() / f"india_vix_{ddmmyyyy}.json"
        if dated_raw.exists():
            try:
                payload = json.loads(dated_raw.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload = {}
            if isinstance(payload, dict) and vix_payload_trade_date(payload) == day:
                return payload, f"nse_eod_raw:{dated_raw.name}"

        for path in sorted(RAW_EOD_DIR.glob("*/india_vix_*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and vix_payload_trade_date(payload) == day:
                return payload, f"nse_eod_raw:{path.parent.name}/{path.name}"

    levels_path = JOURNAL_DIR / f"daily_levels_{day.isoformat()}.json"
    if levels_path.exists():
        try:
            levels = json.loads(levels_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            levels = {}
        detail = _vix_detail_from_daily_levels(levels)
        if detail.get("last") is not None:
            return (
                normalize_detail_to_nse_vix(detail, day),
                f"daily_levels_session:{levels_path.name}",
            )

    return None


def fetch_live_india_vix(session: requests.Session) -> Dict[str, Any]:
    response = session.get(
        ALL_INDICES_URL,
        timeout=30,
        headers={"Referer": "https://www.nseindia.com/market-data/live-market-indices"},
    )
    response.raise_for_status()
    body = response.json()
    rows = body.get("data") if isinstance(body, dict) else None
    vix = next(
        (row for row in rows or [] if str(row.get("indexSymbol", "")).upper() == "INDIA VIX"),
        None,
    )
    if not vix:
        raise ValueError("india_vix_missing_in_allIndices")
    return vix


def resolve_india_vix(
    day: date,
    session: requests.Session,
) -> Tuple[Optional[Dict[str, Any]], str, List[str]]:
    errors: List[str] = []
    try:
        live = fetch_live_india_vix(session)
        live_day = vix_payload_trade_date(live)
        if live_day == day:
            return live, "nse_live_api", errors
        if live_day:
            errors.append(
                f"live_api_date_mismatch: requested={day.isoformat()} payload={live_day.isoformat()}"
            )
        else:
            errors.append("live_api_missing_previousDay")
    except Exception as exc:
        errors.append(f"live_api_error:{exc}")

    backfill = backfill_india_vix(day)
    if backfill:
        payload, source = backfill
        if vix_payload_trade_date(payload) == day:
            return payload, source, errors

    errors.append(f"no_vix_backfill_for_{day.isoformat()}")
    return None, "", errors


def _selftest() -> None:
    import tempfile

    payload = {"previousDay": "21-Jul-2026", "last": "14.52"}
    assert vix_payload_trade_date(payload) == date(2026, 7, 21)
    assert vix_payload_trade_date({}) is None

    normalized = normalize_detail_to_nse_vix({"last": 14.5, "open": 14.0}, date(2026, 7, 21))
    assert normalized["indexSymbol"] == "INDIA VIX"
    assert normalized["previousDay"] == "21-Jul-2026"
    assert normalized["last"] == 14.5

    levels = {"session_close": {"session_context": {"technical_levels": {"india_vix_detail": {"last": 15.1}}}}}
    assert _vix_detail_from_daily_levels(levels)["last"] == 15.1
    assert _vix_detail_from_daily_levels({}) == {}

    # backfill_india_vix: no journal/raw dirs -> None, never raises.
    global JOURNAL_DIR, RAW_EOD_DIR
    tmp = Path(tempfile.mkdtemp(prefix="india-vix-selftest-"))
    original_journal_dir = JOURNAL_DIR
    original_raw_dir = RAW_EOD_DIR
    try:
        JOURNAL_DIR = tmp
        RAW_EOD_DIR = tmp / "raw"
        assert backfill_india_vix(date(2026, 7, 21)) is None

        # A daily_levels journal with a session-close VIX detail resolves.
        levels_path = tmp / "daily_levels_2026-07-21.json"
        levels_path.write_text(json.dumps(levels), encoding="utf-8")
        result = backfill_india_vix(date(2026, 7, 21))
        assert result is not None
        found_payload, source = result
        assert source.startswith("daily_levels_session:")
        assert found_payload["last"] == 15.1
    finally:
        JOURNAL_DIR = original_journal_dir
        RAW_EOD_DIR = original_raw_dir

    print("[sources.india_vix] selftest OK: date validation, normalization, backfill chain")


if __name__ == "__main__":
    _selftest()
