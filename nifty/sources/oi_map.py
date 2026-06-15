#!/usr/bin/env python3
"""Build OI ceiling/floor/max-pain map from NSE FO bhavcopy or live option chain."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests

from nifty.paths import PROJECT_ROOT as BASE_DIR
RAW_EOD_DIR = BASE_DIR / "data" / "nse_eod" / "raw"
NSE_HOME = "https://www.nseindia.com"

SYMBOL_TICKER = {
    "NIFTY": "NIFTY",
    "BANKNIFTY": "BANKNIFTY",
}


def _compute_max_pain(rows: List[Dict[str, Any]]) -> Optional[float]:
    strikes = sorted({float(row["strike"]) for row in rows if row.get("strike") is not None})
    if not strikes:
        return None
    call_oi = {float(r["strike"]): int(r.get("oi") or 0) for r in rows if r.get("type") == "CE"}
    put_oi = {float(r["strike"]): int(r.get("oi") or 0) for r in rows if r.get("type") == "PE"}
    best_strike = None
    best_pain = None
    for spot in strikes:
        pain = 0.0
        for strike, oi in call_oi.items():
            pain += max(0.0, spot - strike) * oi
        for strike, oi in put_oi.items():
            pain += max(0.0, strike - spot) * oi
        if best_pain is None or pain < best_pain:
            best_pain = pain
            best_strike = spot
    return best_strike


def _read_fo_bhavcopy(path: Path) -> Optional[pd.DataFrame]:
    if not path.exists():
        return None
    try:
        if path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as archive:
                names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
                if not names:
                    return None
                with archive.open(names[0]) as handle:
                    return pd.read_csv(handle)
        return pd.read_csv(path)
    except Exception:
        return None


def _parse_expiry_date(value: Any) -> Optional[date]:
    if value in (None, ""):
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _pick_front_expiry(expiries: List[Any], trade_date: Optional[date] = None) -> Any:
    """Pick nearest non-expired series (>= trade_date), not earliest row in bhavcopy."""
    day = trade_date or date.today()
    future = []
    for exp in expiries:
        parsed = _parse_expiry_date(exp)
        if parsed is not None and parsed >= day:
            future.append((parsed, exp))
    if future:
        future.sort(key=lambda item: item[0])
        return future[0][1]
    # Fallback: latest expiry in file (post-roll edge case)
    parsed_all = [(d, exp) for exp in expiries if (d := _parse_expiry_date(exp)) is not None]
    if parsed_all:
        parsed_all.sort(key=lambda item: item[0], reverse=True)
        return parsed_all[0][1]
    return sorted(expiries)[0]


def _find_latest_fo_bhavcopy(before_day: date) -> Tuple[Optional[Path], Optional[str]]:
    if not RAW_EOD_DIR.exists():
        return None, None
    candidates: List[Tuple[date, Path]] = []
    for folder in RAW_EOD_DIR.iterdir():
        if not folder.is_dir():
            continue
        try:
            folder_day = date.fromisoformat(folder.name)
        except ValueError:
            continue
        if folder_day > before_day:
            continue
        for path in folder.glob("BhavCopy_NSE_FO*.zip"):
            candidates.append((folder_day, path))
        for path in folder.glob("BhavCopy_NSE_FO*.csv"):
            candidates.append((folder_day, path))
    if not candidates:
        return None, None
    candidates.sort(key=lambda item: item[0], reverse=True)
    chosen_day, chosen_path = candidates[0]
    return chosen_path, chosen_day.isoformat()


def build_oi_map_from_bhavcopy(symbol: str, trade_date: Optional[date] = None) -> Dict[str, Any]:
    day = trade_date or date.today()
    path, source_day = _find_latest_fo_bhavcopy(day)
    if path is None:
        return {"error": "no_fo_bhavcopy_found"}

    df = _read_fo_bhavcopy(path)
    if df is None or df.empty:
        return {"error": "fo_bhavcopy_unreadable", "source_file": str(path)}

    ticker = SYMBOL_TICKER.get(symbol.upper(), symbol.upper())
    subset = df[(df["TckrSymb"] == ticker) & (df["OptnTp"].isin(["CE", "PE"]))].copy()
    if subset.empty:
        return {"error": f"no_options_rows_for_{ticker}", "source_file": str(path)}

    expiry_col = "XpryDt"
    nearest_expiry = _pick_front_expiry(list(subset[expiry_col].dropna().unique()), trade_date=day)
    front = subset[subset[expiry_col] == nearest_expiry].copy()
    front["StrkPric"] = pd.to_numeric(front["StrkPric"], errors="coerce")
    front["OpnIntrst"] = pd.to_numeric(front["OpnIntrst"], errors="coerce").fillna(0)

    rows: List[Dict[str, Any]] = []
    for _, row in front.iterrows():
        rows.append(
            {
                "strike": float(row["StrkPric"]),
                "oi": int(row["OpnIntrst"]),
                "type": str(row["OptnTp"]),
            }
        )

    ce = (
        front[front["OptnTp"] == "CE"]
        .groupby("StrkPric")["OpnIntrst"]
        .sum()
        .sort_values(ascending=False)
    )
    pe = (
        front[front["OptnTp"] == "PE"]
        .groupby("StrkPric")["OpnIntrst"]
        .sum()
        .sort_values(ascending=False)
    )
    underlying = front["UndrlygPric"].dropna()
    underlying_price = float(underlying.iloc[0]) if not underlying.empty else None
    max_pain = _compute_max_pain(rows)
    total_call_oi = int(ce.sum()) if not ce.empty else 0
    total_put_oi = int(pe.sum()) if not pe.empty else 0
    pcr_oi = round(total_put_oi / total_call_oi, 3) if total_call_oi else None

    top_calls = [{"strike": float(k), "oi": int(v)} for k, v in ce.head(5).items()]
    top_puts = [{"strike": float(k), "oi": int(v)} for k, v in pe.head(5).items()]

    return {
        "source": "nse_fo_bhavcopy",
        "source_file": str(path),
        "source_trade_date": source_day,
        "symbol": ticker,
        "expiry": str(nearest_expiry),
        "underlying_price": underlying_price,
        "max_pain": max_pain,
        "max_pain_vs_spot": round(max_pain - underlying_price, 2) if max_pain and underlying_price else None,
        "pcr_oi": pcr_oi,
        "ceiling": {"strike": top_calls[0]["strike"], "oi": top_calls[0]["oi"], "rank": 1} if top_calls else {},
        "floor": {"strike": top_puts[0]["strike"], "oi": top_puts[0]["oi"], "rank": 1} if top_puts else {},
        "top_call_oi_strikes": top_calls,
        "top_put_oi_strikes": top_puts,
    }


def fetch_live_nse_option_chain(symbol: str, session: Optional[requests.Session] = None) -> Dict[str, Any]:
    own_session = session is None
    sess = session or requests.Session()
    if own_session:
        sess.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
                ),
                "Accept": "*/*",
                "Referer": NSE_HOME,
            }
        )
        sess.get(NSE_HOME, timeout=15)
        sess.get(f"{NSE_HOME}/option-chain", timeout=15)

    url = f"{NSE_HOME}/api/option-chain-indices?symbol={symbol.upper()}"
    response = sess.get(url, timeout=20)
    if response.status_code != 200:
        return {"error": f"nse_option_chain_{response.status_code}"}
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return {"error": "nse_option_chain_invalid_json"}

    records = payload.get("records") or {}
    data = records.get("data") or []
    if not data:
        return {"error": "nse_option_chain_empty"}

    underlying = records.get("underlyingValue")
    expiry_dates = records.get("expiryDates") or []
    expiry = expiry_dates[0] if expiry_dates else None

    rows: List[Dict[str, Any]] = []
    for item in data:
        if item.get("expiryDate") != expiry:
            continue
        strike = item.get("strikePrice")
        ce = item.get("CE") or {}
        pe = item.get("PE") or {}
        if ce:
            rows.append({"strike": float(strike), "oi": int(ce.get("openInterest") or 0), "type": "CE"})
        if pe:
            rows.append({"strike": float(strike), "oi": int(pe.get("openInterest") or 0), "type": "PE"})

    ce_sorted = sorted([r for r in rows if r["type"] == "CE"], key=lambda r: r["oi"], reverse=True)
    pe_sorted = sorted([r for r in rows if r["type"] == "PE"], key=lambda r: r["oi"], reverse=True)
    max_pain = _compute_max_pain(rows)
    total_call_oi = sum(r["oi"] for r in rows if r["type"] == "CE")
    total_put_oi = sum(r["oi"] for r in rows if r["type"] == "PE")
    pcr_oi = round(total_put_oi / total_call_oi, 3) if total_call_oi else None

    return {
        "source": "nse_live_option_chain",
        "symbol": symbol.upper(),
        "expiry": expiry,
        "underlying_price": underlying,
        "max_pain": max_pain,
        "max_pain_vs_spot": round(max_pain - float(underlying), 2) if max_pain and underlying else None,
        "pcr_oi": pcr_oi,
        "ceiling": {"strike": ce_sorted[0]["strike"], "oi": ce_sorted[0]["oi"], "rank": 1} if ce_sorted else {},
        "floor": {"strike": pe_sorted[0]["strike"], "oi": pe_sorted[0]["oi"], "rank": 1} if pe_sorted else {},
        "top_call_oi_strikes": ce_sorted[:5],
        "top_put_oi_strikes": pe_sorted[:5],
    }


def build_oi_map(symbol: str, trade_date: Optional[date] = None) -> Dict[str, Any]:
    """Prefer live NSE chain; fall back to latest FO bhavcopy."""
    live = fetch_live_nse_option_chain(symbol)
    if not live.get("error"):
        return live
    bhav = build_oi_map_from_bhavcopy(symbol, trade_date=trade_date)
    if not bhav.get("error"):
        bhav["live_chain_error"] = live.get("error")
        return bhav
    return {"error": live.get("error") or bhav.get("error"), "live": live, "bhavcopy": bhav}
