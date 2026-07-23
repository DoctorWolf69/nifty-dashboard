#!/usr/bin/env python3
"""FII/DII helpers — live NSE fetch, payload date validation, journal backfill.

Ported faithfully from quant-desk-engine v4/ATLAS's nifty_fii_dii.py
(mentor-authored). No logic changed. Only adaptation: JOURNAL_DIR and
RAW_EOD_DIR resolve via nifty.paths instead of bare __file__-relative
parents.

Genuinely new capability: nifty-dashboard's existing FII/DII fetch
(nifty/morning/phases.py's fetch_fii_dii + _fii_net_from_rows) only pulls
net values with no date validation — it silently trusts whatever the live
API returns even if its payload date doesn't match the requested trading
day. This module validates the live payload's own date field against the
requested day and falls back through raw NSE-EOD archives, then morning
desk journals (in three shapes: raw rows, header net figures, or trend
history), before giving up — the errors list documents exactly which
sources were tried and why each failed. Used by nse_official_flows.py
(this session's next port) for its own FII/DII net figures.

Not yet wired into the live pipeline.
Self-check: python -m nifty.sources.fii_dii
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from nifty.paths import JOURNAL_DIR, NSE_EOD_DIR

RAW_EOD_DIR = NSE_EOD_DIR / "raw"
FII_DII_URL = "https://www.nseindia.com/api/fiidiiTradeReact"


def parse_fii_date(raw: str) -> Optional[date]:
    for fmt in ("%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(raw or "").strip(), fmt).date()
        except ValueError:
            continue
    return None


def fii_rows_trade_date(rows: List[Dict[str, Any]]) -> Optional[date]:
    for row in rows:
        parsed = parse_fii_date(str(row.get("date") or ""))
        if parsed:
            return parsed
    return None


def fii_net_from_rows(rows: List[Dict[str, Any]]) -> Tuple[Optional[float], Optional[float], str]:
    fii = next((row for row in rows if "FII" in str(row.get("category", "")).upper()), {})
    dii = next((row for row in rows if str(row.get("category", "")).upper() == "DII"), {})
    fii_net = _as_float(fii.get("netValue") or fii.get("netvalue"))
    dii_net = _as_float(dii.get("netValue") or dii.get("netvalue"))
    day_label = str(fii.get("date") or dii.get("date") or "")
    return fii_net, dii_net, day_label


def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def rows_from_summary(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Rebuild NSE-shaped rows — preserve buy/sell when present on history entry."""
    day_label = str(entry.get("fii_dii_date") or entry.get("trade_date") or "")

    def _row(category: str, prefix: str) -> Dict[str, Any]:
        side = entry.get(prefix) or {}
        buy = entry.get(f"{prefix}_buy_crores") or side.get("buy_crores")
        sell = entry.get(f"{prefix}_sell_crores") or side.get("sell_crores")
        net = entry.get(f"{prefix}_net_crores") or side.get("net_crores")
        if buy is None and sell is None and net is None:
            return {}
        return {
            "category": category,
            "date": day_label,
            "buyValue": str(buy) if buy is not None else "",
            "sellValue": str(sell) if sell is not None else "",
            "netValue": str(net) if net is not None else "",
        }

    rows: List[Dict[str, Any]] = []
    dii = _row("DII", "dii")
    fii = _row("FII/FPI", "fii")
    if dii:
        rows.append(dii)
    if fii:
        rows.append(fii)
    if rows:
        return rows

    fii_net = entry.get("fii_net_crores")
    dii_net = entry.get("dii_net_crores")
    return [
        {
            "category": "DII",
            "date": day_label,
            "netValue": str(dii_net) if dii_net is not None else "",
            "buyValue": "",
            "sellValue": "",
        },
        {
            "category": "FII/FPI",
            "date": day_label,
            "netValue": str(fii_net) if fii_net is not None else "",
            "buyValue": "",
            "sellValue": "",
        },
    ]


def rows_from_morning_header(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    return rows_from_summary(
        {
            "fii_dii_date": payload.get("fii_dii_date"),
            "fii_net_crores": payload.get("fii_net_crores"),
            "dii_net_crores": payload.get("dii_net_crores"),
        }
    )


def backfill_fii_dii_rows(day: date) -> Optional[Tuple[List[Dict[str, Any]], str]]:
    """Resolve historical FII/DII when the live NSE API only returns today."""
    if RAW_EOD_DIR.exists():
        for path in sorted(RAW_EOD_DIR.glob(f"*/fii_dii_{day.strftime('%d%m%Y')}.json")):
            try:
                rows = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if isinstance(rows, list) and fii_rows_trade_date(rows) == day:
                return rows, f"nse_eod_raw:{path.name}"

        for path in sorted(RAW_EOD_DIR.glob("*/fii_dii_*.json")):
            try:
                rows = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if isinstance(rows, list) and fii_rows_trade_date(rows) == day:
                return rows, f"nse_eod_raw:{path.parent.name}/{path.name}"

    for path in sorted(JOURNAL_DIR.glob("morning_desk_*.json"), reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue

        raw = payload.get("fii_dii") or []
        if isinstance(raw, list) and raw and fii_rows_trade_date(raw) == day:
            return raw, f"morning_desk_rows:{path.name}"

    for path in sorted(JOURNAL_DIR.glob("morning_desk_*.json"), reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue

        if parse_fii_date(str(payload.get("fii_dii_date") or "")) == day:
            return rows_from_morning_header(payload), f"morning_desk_header:{path.name}"

    for path in sorted(JOURNAL_DIR.glob("morning_desk_*.json"), reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue

        for entry in (payload.get("fii_dii_trend") or {}).get("daily") or []:
            if parse_fii_date(str(entry.get("fii_dii_date") or "")) == day:
                return rows_from_summary(entry), f"morning_desk_trend:{path.name}"

    return None


def fetch_live_fii_dii(session: requests.Session) -> List[Dict[str, Any]]:
    response = session.get(FII_DII_URL, timeout=30, headers={"Referer": "https://www.nseindia.com/reports/fii-dii"})
    response.raise_for_status()
    rows = response.json()
    return rows if isinstance(rows, list) else []


def resolve_fii_dii_rows(
    day: date,
    session: requests.Session,
) -> Tuple[Optional[List[Dict[str, Any]]], str, List[str]]:
    """
    Return (rows, source, errors).
    Live API is used only when its payload date matches the requested trade date.
    """
    errors: List[str] = []
    try:
        live_rows = fetch_live_fii_dii(session)
        live_day = fii_rows_trade_date(live_rows)
        if live_day == day:
            return live_rows, "nse_live_api", errors
        if live_day:
            errors.append(
                f"live_api_date_mismatch: requested={day.isoformat()} payload={live_day.isoformat()}"
            )
        else:
            errors.append("live_api_missing_date")
    except Exception as exc:
        errors.append(f"live_api_error:{exc}")

    backfill = backfill_fii_dii_rows(day)
    if backfill:
        rows, source = backfill
        if fii_rows_trade_date(rows) == day:
            return rows, source, errors

    errors.append(f"no_backfill_for_{day.isoformat()}")
    return None, "", errors


def _selftest() -> None:
    import tempfile

    assert parse_fii_date("21-Jul-2026") == date(2026, 7, 21)
    assert parse_fii_date("2026-07-21") == date(2026, 7, 21)
    assert parse_fii_date("garbage") is None

    rows = [
        {"category": "FII/FPI", "date": "21-Jul-2026", "netValue": "-850.5"},
        {"category": "DII", "date": "21-Jul-2026", "netValue": "620.25"},
    ]
    assert fii_rows_trade_date(rows) == date(2026, 7, 21)
    fii_net, dii_net, day_label = fii_net_from_rows(rows)
    assert fii_net == -850.5 and dii_net == 620.25
    assert day_label == "21-Jul-2026"

    assert fii_net_from_rows([]) == (None, None, "")

    summary_rows = rows_from_summary({
        "fii_dii_date": "21-Jul-2026", "fii_net_crores": -500.0, "dii_net_crores": 300.0,
    })
    assert len(summary_rows) == 2
    assert fii_rows_trade_date(summary_rows) == date(2026, 7, 21)

    detailed_rows = rows_from_summary({
        "fii_dii_date": "21-Jul-2026",
        "fii": {"buy_crores": 1000.0, "sell_crores": 1500.0, "net_crores": -500.0},
        "dii": {"buy_crores": 800.0, "sell_crores": 500.0, "net_crores": 300.0},
    })
    assert len(detailed_rows) == 2
    dii_row = next(r for r in detailed_rows if r["category"] == "DII")
    assert dii_row["buyValue"] == "800.0"

    header_rows = rows_from_morning_header({"fii_dii_date": "21-Jul-2026", "fii_net_crores": -100.0, "dii_net_crores": 50.0})
    assert fii_rows_trade_date(header_rows) == date(2026, 7, 21)

    # backfill_fii_dii_rows: no journal/raw dirs -> None, never raises.
    # Patch this module's own globals directly (not a self-import — under
    # `python -m`, re-importing this file by its dotted name yields a SEPARATE
    # module object from __main__, so patching that copy wouldn't affect the
    # functions actually under test here).
    global JOURNAL_DIR, RAW_EOD_DIR
    empty_dir = Path(tempfile.mkdtemp(prefix="fii-dii-selftest-"))
    original_journal_dir = JOURNAL_DIR
    original_raw_dir = RAW_EOD_DIR
    try:
        JOURNAL_DIR = empty_dir
        RAW_EOD_DIR = empty_dir / "raw"
        assert backfill_fii_dii_rows(date(2026, 7, 21)) is None

        # A morning_desk journal with raw fii_dii rows resolves via the first path.
        morning_path = empty_dir / "morning_desk_2026-07-21.json"
        morning_path.write_text(json.dumps({"fii_dii": rows}), encoding="utf-8")
        result = backfill_fii_dii_rows(date(2026, 7, 21))
        assert result is not None
        found_rows, source = result
        assert source.startswith("morning_desk_rows:")
        assert fii_rows_trade_date(found_rows) == date(2026, 7, 21)
    finally:
        JOURNAL_DIR = original_journal_dir
        RAW_EOD_DIR = original_raw_dir

    print("[sources.fii_dii] selftest OK: date parsing, row reconstruction, backfill chain")


if __name__ == "__main__":
    _selftest()
