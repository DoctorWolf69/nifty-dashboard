#!/usr/bin/env python3
"""Full Nifty sector index scan — FinStack first, NSE allIndices + Kite backfill.

Ported faithfully from quant-desk-engine v4/ATLAS's kite_sector_scan.py
(mentor-authored). No logic changed. Adaptations:
- `from desk_kite_spot import load_kite_optional` -> nifty.kite.spot
  (already ported, same function name).
- BASE_DIR/SECTOR_CONSTITUENTS_PATH resolve via nifty.paths.PROJECT_ROOT,
  matching nifty/sources/sector_leaders.py's identical constant (same
  config/sector_constituents.json file both modules read).
- `from finstack_bridge import fetch_sector_performance` (inside
  fetch_full_sector_performance, a local/deferred import in the source
  too) -> nifty.sources.finstack.fetch_sector_performance, which already
  exists in nifty-dashboard (wraps finstack.data.analytics.get_sector_performance
  when the optional finstack package is installed, else returns an error
  dict) — same null-safe contract the source relied on.

Genuinely new capability: nifty-dashboard had no sector-index scan at all
before this. fetch_sector_performance_eod() is the dependency
nifty/sources/sector_leaders.py's build_eod_sector_scan() needs (live
same-day vs Kite historical-candle backfill for past sessions);
load_desk_sector_names() is the canonical sector name list both modules
share.

Not yet wired into the live pipeline.
Self-check: python -m nifty.sources.kite_sector_scan
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests

from nifty.kite.spot import load_kite_optional
from nifty.paths import PROJECT_ROOT

SECTOR_CONSTITUENTS_PATH = PROJECT_ROOT / "config" / "sector_constituents.json"
NSE_HOME = "https://www.nseindia.com"
ALL_INDICES_URL = f"{NSE_HOME}/api/allIndices"


def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def nse_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Referer": NSE_HOME,
        }
    )
    session.get(NSE_HOME, timeout=15)
    return session

# Desk display name -> (NSE allIndices indexSymbol, Kite instrument key)
DESK_SECTOR_SYMBOLS: Dict[str, Tuple[str, str]] = {
    "Nifty 50": ("NIFTY 50", "NSE:NIFTY 50"),
    "Bank Nifty": ("NIFTY BANK", "NSE:NIFTY BANK"),
    "Nifty IT": ("NIFTY IT", "NSE:NIFTY IT"),
    "Nifty Pharma": ("NIFTY PHARMA", "NSE:NIFTY PHARMA"),
    "Nifty Auto": ("NIFTY AUTO", "NSE:NIFTY AUTO"),
    "Nifty FMCG": ("NIFTY FMCG", "NSE:NIFTY FMCG"),
    "Nifty Metal": ("NIFTY METAL", "NSE:NIFTY METAL"),
    "Nifty Energy": ("NIFTY ENERGY", "NSE:NIFTY ENERGY"),
    "Nifty Realty": ("NIFTY REALTY", "NSE:NIFTY REALTY"),
}

# Extended sector indices (observatory / breadth context — not in stock-leader baskets)
EXTENDED_SECTOR_SYMBOLS: Dict[str, Tuple[str, str]] = {
    "Nifty Media": ("NIFTY MEDIA", "NSE:NIFTY MEDIA"),
    "Nifty PSU Bank": ("NIFTY PSU BANK", "NSE:NIFTY PSU BANK"),
    "Nifty Private Bank": ("NIFTY PVT BANK", "NSE:NIFTY PVT BANK"),
    "Nifty Financial Services": ("NIFTY FIN SERVICE", "NSE:NIFTY FIN SERVICE"),
    "Nifty Consumption": ("NIFTY CONSUMPTION", "NSE:NIFTY CONSUMPTION"),
    "Nifty Infrastructure": ("NIFTY INFRA", "NSE:NIFTY INFRA"),
    "Nifty Commodities": ("NIFTY COMMODITIES", "NSE:NIFTY COMMODITIES"),
}


def load_desk_sector_names(*, include_extended: bool = False) -> List[str]:
    names: List[str] = []
    if SECTOR_CONSTITUENTS_PATH.exists():
        payload = json.loads(SECTOR_CONSTITUENTS_PATH.read_text(encoding="utf-8"))
        names.extend(str(k) for k in payload.keys())
    else:
        names.extend(DESK_SECTOR_SYMBOLS.keys())
    if include_extended:
        for name in EXTENDED_SECTOR_SYMBOLS:
            if name not in names:
                names.append(name)
    # Stable order: Nifty 50 + Bank first, then alphabetically
    priority = {"Nifty 50": 0, "Bank Nifty": 1}
    return sorted(set(names), key=lambda n: (priority.get(n, 99), n))


def _sector_row(
    sector: str,
    *,
    value: Optional[float],
    change_pct: Optional[float],
    source: str,
    nse_symbol: Optional[str] = None,
    kite_symbol: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "sector": sector,
        "value": value,
        "change_pct": change_pct,
        "source": source,
        "nse_symbol": nse_symbol,
        "kite_symbol": kite_symbol,
    }


def _fetch_nse_all_indices() -> Dict[str, Dict[str, Any]]:
    session = nse_session()
    response = session.get(ALL_INDICES_URL, timeout=20)
    response.raise_for_status()
    by_symbol: Dict[str, Dict[str, Any]] = {}
    for row in response.json().get("data") or []:
        sym = str(row.get("indexSymbol") or row.get("index") or "").upper()
        if sym:
            by_symbol[sym] = row
    return by_symbol


def fetch_sectors_from_nse(sector_names: Optional[List[str]] = None) -> Dict[str, Any]:
    """Prior-session index % from NSE allIndices (percentChange field)."""
    names = sector_names or load_desk_sector_names()
    symbol_map = {**DESK_SECTOR_SYMBOLS, **EXTENDED_SECTOR_SYMBOLS}
    errors: List[str] = []
    sectors: List[Dict[str, Any]] = []

    try:
        nse_rows = _fetch_nse_all_indices()
    except Exception as exc:
        return {"error": str(exc), "sectors": [], "source": "nse_allIndices"}

    for name in names:
        nse_sym, kite_sym = symbol_map.get(name, (None, None))
        if not nse_sym:
            errors.append(f"no_symbol_map:{name}")
            continue
        row = nse_rows.get(nse_sym.upper()) or {}
        if not row:
            errors.append(f"missing_nse:{nse_sym}")
            continue
        sectors.append(
            _sector_row(
                name,
                value=_as_float(row.get("last")),
                change_pct=_as_float(row.get("percentChange")),
                source="nse_allIndices",
                nse_symbol=nse_sym,
                kite_symbol=kite_sym,
            )
        )

    return {"sectors": sectors, "errors": errors, "source": "nse_allIndices"}


def fetch_sectors_from_kite(sector_names: Optional[List[str]] = None) -> Dict[str, Any]:
    """Live index quotes from Kite — change vs previous close."""
    names = sector_names or load_desk_sector_names()
    symbol_map = {**DESK_SECTOR_SYMBOLS, **EXTENDED_SECTOR_SYMBOLS}
    kite = load_kite_optional()
    if kite is None:
        return {"error": "kite_unavailable", "sectors": [], "source": "kite"}

    symbols = []
    name_by_kite: Dict[str, str] = {}
    for name in names:
        pair = symbol_map.get(name)
        if not pair:
            continue
        kite_sym = pair[1]
        symbols.append(kite_sym)
        name_by_kite[kite_sym] = name

    if not symbols:
        return {"error": "no_symbols", "sectors": [], "source": "kite"}

    try:
        quotes = kite.quote(symbols)
    except Exception as exc:
        return {"error": str(exc), "sectors": [], "source": "kite"}

    sectors: List[Dict[str, Any]] = []
    errors: List[str] = []
    for kite_sym, name in name_by_kite.items():
        raw = quotes.get(kite_sym) or {}
        if not raw:
            errors.append(f"missing_kite:{kite_sym}")
            continue
        last = _as_float(raw.get("last_price"))
        ohlc = raw.get("ohlc") or {}
        prev = _as_float(ohlc.get("close"))
        pct = _as_float(raw.get("net_change"))
        if pct is None and last is not None and prev and prev > 0:
            pct = round((last - prev) / prev * 100, 2)
        nse_sym = symbol_map.get(name, (None, None))[0]
        sectors.append(
            _sector_row(
                name,
                value=last,
                change_pct=pct,
                source="kite",
                nse_symbol=nse_sym,
                kite_symbol=kite_sym,
            )
        )

    return {"sectors": sectors, "errors": errors, "source": "kite"}


def _merge_sector_payloads(*payloads: Dict[str, Any]) -> Dict[str, Any]:
    merged: Dict[str, Dict[str, Any]] = {}
    sources: List[str] = []
    errors: List[str] = []

    priority = {"finstack": 0, "nse_allIndices": 1, "kite": 2}

    for payload in payloads:
        if not payload:
            continue
        src = str(payload.get("source") or "unknown")
        if src not in sources:
            sources.append(src)
        errors.extend(payload.get("errors") or [])
        for row in payload.get("sectors") or []:
            name = str(row.get("sector") or "")
            if not name:
                continue
            existing = merged.get(name)
            row_src = str(row.get("source") or src)
            if existing is None:
                merged[name] = dict(row)
                continue
            existing_pri = priority.get(str(existing.get("source") or ""), 99)
            new_pri = priority.get(row_src, 99)
            # Prefer higher-priority source; backfill missing fields from lower priority
            if new_pri < existing_pri:
                backfill = {k: v for k, v in existing.items() if existing.get(k) is not None}
                merged[name] = {**backfill, **row}
            else:
                for key, val in row.items():
                    if merged[name].get(key) in (None, "", 0) and val not in (None, ""):
                        merged[name][key] = val

    sectors = list(merged.values())
    sectors.sort(
        key=lambda r: (
            0 if r.get("sector") == "Nifty 50" else 1 if r.get("sector") == "Bank Nifty" else 2,
            str(r.get("sector") or ""),
        )
    )

    if sectors:
        best = max(sectors, key=lambda r: _as_float(r.get("change_pct")) or -999)
        worst = min(sectors, key=lambda r: _as_float(r.get("change_pct")) or 999)
    else:
        best = worst = {}

    return {
        "sectors": sectors,
        "best_performer": best,
        "worst_performer": worst,
        "sources": sources,
        "errors": errors,
        "source": "+".join(sources) if sources else "none",
    }


def fetch_sector_performance_historical(trade_date: date) -> Dict[str, Any]:
    """Sector index % for a past session via Kite daily candles."""
    label = trade_date.isoformat()
    kite = load_kite_optional()
    names = load_desk_sector_names()
    symbol_map = {**DESK_SECTOR_SYMBOLS, **EXTENDED_SECTOR_SYMBOLS}
    sectors: List[Dict[str, Any]] = []
    errors: List[str] = []

    if kite is None:
        return {
            "sectors": [],
            "errors": ["kite_unavailable"],
            "source": "kite_historical",
            "sector_index_session": label,
        }

    kite_syms = [symbol_map[n][1] for n in names if n in symbol_map]
    try:
        quotes = kite.quote(kite_syms)
    except Exception as exc:
        return {
            "sectors": [],
            "errors": [str(exc)],
            "source": "kite_historical",
            "sector_index_session": label,
        }

    from_dt = datetime.combine(trade_date - timedelta(days=14), datetime.min.time())
    to_dt = datetime.combine(trade_date + timedelta(days=1), datetime.min.time())

    for name in names:
        pair = symbol_map.get(name)
        if not pair:
            errors.append(f"no_symbol_map:{name}")
            continue
        nse_sym, kite_sym = pair
        raw = quotes.get(kite_sym) or {}
        token = raw.get("instrument_token")
        if not token:
            errors.append(f"no_token:{name}")
            continue
        try:
            candles_raw = kite.historical_data(
                token, from_dt, to_dt, "day", continuous=False, oi=False
            )
        except Exception as exc:
            errors.append(f"hist:{name}:{exc}")
            continue
        candles = [
            {"date": str(row["date"])[:10], "close": float(row["close"])}
            for row in sorted(candles_raw, key=lambda item: item["date"])
        ]
        by_date = {row["date"]: row for row in candles}
        if label not in by_date:
            errors.append(f"no_candle:{name}:{label}")
            continue
        dates = sorted(by_date.keys())
        idx = dates.index(label)
        if idx == 0:
            errors.append(f"no_prior:{name}")
            continue
        curr = by_date[label]["close"]
        prev = by_date[dates[idx - 1]]["close"]
        pct = round((curr - prev) / prev * 100, 2) if prev > 0 else None
        sectors.append(
            _sector_row(
                name,
                value=curr,
                change_pct=pct,
                source="kite_historical",
                nse_symbol=nse_sym,
                kite_symbol=kite_sym,
            )
        )

    payload: Dict[str, Any] = {
        "sectors": sectors,
        "errors": errors,
        "source": "kite_historical",
        "sector_index_session": label,
    }
    if sectors:
        payload["best_performer"] = max(
            sectors, key=lambda row: _as_float(row.get("change_pct")) or -999
        )
        payload["worst_performer"] = min(
            sectors, key=lambda row: _as_float(row.get("change_pct")) or 999
        )
    return payload


def fetch_sector_performance_eod(trade_date: date) -> Dict[str, Any]:
    """Sector index % for trade_date — live NSE/FinStack same day, Kite historical otherwise."""
    label = trade_date.isoformat()
    if trade_date >= date.today():
        payload = fetch_full_sector_performance()
        payload["sector_index_session"] = label
        payload["sector_index_mode"] = "live_eod"
        return payload
    return fetch_sector_performance_historical(trade_date)


def fetch_full_sector_performance(*, include_extended: bool = False) -> Dict[str, Any]:
    """
    FinStack sector_performance when available; backfill missing desk sectors from
    NSE allIndices, then Kite for any still missing.
    """
    names = load_desk_sector_names(include_extended=include_extended)
    finstack_payload: Dict[str, Any] = {"sectors": [], "source": "finstack"}
    try:
        from nifty.sources.finstack import fetch_sector_performance

        finstack_payload = fetch_sector_performance()
        finstack_payload.setdefault("source", "finstack")
    except Exception as exc:
        finstack_payload = {"error": str(exc), "sectors": [], "source": "finstack"}

    finstack_names = {str(s.get("sector")) for s in (finstack_payload.get("sectors") or [])}
    missing = [n for n in names if n not in finstack_names]

    nse_payload: Dict[str, Any] = {"sectors": []}
    kite_payload: Dict[str, Any] = {"sectors": []}

    if missing:
        nse_payload = fetch_sectors_from_nse(missing)
        nse_names = {str(s.get("sector")) for s in (nse_payload.get("sectors") or [])}
        still_missing = [n for n in missing if n not in nse_names]
        if still_missing:
            kite_payload = fetch_sectors_from_kite(still_missing)

    merged = _merge_sector_payloads(finstack_payload, nse_payload, kite_payload)
    merged["finstack_count"] = len(finstack_payload.get("sectors") or [])
    merged["backfill_count"] = len(merged.get("sectors") or []) - merged["finstack_count"]
    if finstack_payload.get("error"):
        merged.setdefault("errors", []).append(f"finstack:{finstack_payload['error']}")
    return merged


def _selftest() -> None:
    import tempfile
    from pathlib import Path

    global SECTOR_CONSTITUENTS_PATH
    tmp = Path(tempfile.mkdtemp(prefix="kite-sector-scan-selftest-"))
    original_path = SECTOR_CONSTITUENTS_PATH
    try:
        # No config file -> falls back to the built-in desk sector list.
        SECTOR_CONSTITUENTS_PATH = tmp / "sector_constituents.json"
        names = load_desk_sector_names()
        assert names[0] == "Nifty 50" and names[1] == "Bank Nifty"
        assert "Nifty IT" in names
        assert "Nifty Media" not in names  # extended sectors excluded by default

        names_ext = load_desk_sector_names(include_extended=True)
        assert "Nifty Media" in names_ext

        SECTOR_CONSTITUENTS_PATH.write_text(
            json.dumps({"IT": ["TCS"], "BANKING": ["HDFCBANK"]}), encoding="utf-8"
        )
        configured_names = load_desk_sector_names()
        assert set(configured_names) == {"IT", "BANKING"}
    finally:
        SECTOR_CONSTITUENTS_PATH = original_path

    row = _sector_row("Nifty IT", value=35000.0, change_pct=1.2, source="nse_allIndices", nse_symbol="NIFTY IT")
    assert row["sector"] == "Nifty IT" and row["change_pct"] == 1.2

    # _merge_sector_payloads: finstack wins over nse_allIndices for the same sector.
    finstack_payload = {
        "sectors": [_sector_row("Nifty 50", value=25000.0, change_pct=0.5, source="finstack")],
        "source": "finstack",
    }
    nse_payload = {
        "sectors": [
            _sector_row("Nifty 50", value=25001.0, change_pct=0.9, source="nse_allIndices"),
            _sector_row("Nifty IT", value=35000.0, change_pct=-1.1, source="nse_allIndices"),
        ],
        "source": "nse_allIndices",
    }
    merged = _merge_sector_payloads(finstack_payload, nse_payload)
    by_sector = {r["sector"]: r for r in merged["sectors"]}
    assert by_sector["Nifty 50"]["change_pct"] == 0.5  # finstack priority wins
    assert by_sector["Nifty IT"]["change_pct"] == -1.1  # backfilled from nse
    assert merged["best_performer"]["sector"] == "Nifty 50"
    assert merged["worst_performer"]["sector"] == "Nifty IT"
    assert merged["sectors"][0]["sector"] == "Nifty 50"  # stable sort: Nifty 50 first

    # fetch_sectors_from_kite: kite unavailable in this env -> graceful error dict.
    kite_row = fetch_sectors_from_kite(["Nifty 50"])
    assert kite_row["source"] == "kite"
    assert kite_row.get("error") in ("kite_unavailable", "no_symbols") or kite_row.get("sectors") == []

    # fetch_sector_performance_historical: kite unavailable -> graceful error dict, never raises.
    hist = fetch_sector_performance_historical(date(2026, 7, 1))
    assert hist["source"] == "kite_historical"
    assert hist["sector_index_session"] == "2026-07-01"
    assert hist["sectors"] == []

    print("[sources.kite_sector_scan] selftest OK: sector names, merge priority, historical/kite fallback")


if __name__ == "__main__":
    _selftest()
