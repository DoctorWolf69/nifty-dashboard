#!/usr/bin/env python3
"""Compile official NSE EOD data into desk journal filing artifacts."""

from __future__ import annotations

import argparse
import json
import sqlite3
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from nifty.morning.phases import load_fii_dii_history, summarize_fii_trend
from nifty.core.journal import NiftyJournalStore, ist_now, today_str
from nifty.sources.oi_map import _compute_max_pain, _read_fo_bhavcopy

from nifty.paths import PROJECT_ROOT as BASE_DIR
NSE_EOD_DB = BASE_DIR / "data" / "nse_eod" / "nse_eod.sqlite"
RAW_EOD_DIR = BASE_DIR / "data" / "nse_eod" / "raw"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build NSE EOD desk filing JSON")
    parser.add_argument("--date", default="", help="Trade date YYYY-MM-DD (default: previous session)")
    return parser.parse_args()


def previous_trading_day(start: Optional[date] = None) -> date:
    day = start or date.today()
    day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def _fo_bhav_path(day: date) -> Optional[Path]:
    folder = RAW_EOD_DIR / day.isoformat()
    if not folder.exists():
        return None
    for pattern in ("BhavCopy_NSE_FO*.zip", "BhavCopy_NSE_FO*.csv"):
        matches = sorted(folder.glob(pattern))
        if matches:
            return matches[0]
    return None


def _oi_map_for_expiry(df: pd.DataFrame, symbol: str, expiry: str) -> Dict[str, Any]:
    ticker = symbol.upper()
    subset = df[(df["TckrSymb"] == ticker) & (df["OptnTp"].isin(["CE", "PE"])) & (df["XpryDt"] == expiry)].copy()
    if subset.empty:
        return {"error": f"no_rows_for_{ticker}_{expiry}"}

    subset["StrkPric"] = pd.to_numeric(subset["StrkPric"], errors="coerce")
    subset["OpnIntrst"] = pd.to_numeric(subset["OpnIntrst"], errors="coerce").fillna(0)

    rows: List[Dict[str, Any]] = []
    for _, row in subset.iterrows():
        rows.append({"strike": float(row["StrkPric"]), "oi": int(row["OpnIntrst"]), "type": str(row["OptnTp"])})

    ce = subset[subset["OptnTp"] == "CE"].groupby("StrkPric")["OpnIntrst"].sum().sort_values(ascending=False)
    pe = subset[subset["OptnTp"] == "PE"].groupby("StrkPric")["OpnIntrst"].sum().sort_values(ascending=False)
    underlying_series = subset["UndrlygPric"].dropna()
    underlying = float(underlying_series.iloc[0]) if not underlying_series.empty else None
    max_pain = _compute_max_pain(rows)
    total_call_oi = int(ce.sum()) if not ce.empty else 0
    total_put_oi = int(pe.sum()) if not pe.empty else 0
    pcr_oi = round(total_put_oi / total_call_oi, 3) if total_call_oi else None
    top_calls = [{"strike": float(k), "oi": int(v)} for k, v in ce.head(5).items()]
    top_puts = [{"strike": float(k), "oi": int(v)} for k, v in pe.head(5).items()]

    return {
        "symbol": ticker,
        "expiry": expiry,
        "underlying_price": underlying,
        "max_pain": max_pain,
        "max_pain_vs_spot": round(max_pain - underlying, 2) if max_pain and underlying else None,
        "pcr_oi": pcr_oi,
        "ceiling": {"strike": top_calls[0]["strike"], "oi": top_calls[0]["oi"], "rank": 1} if top_calls else {},
        "floor": {"strike": top_puts[0]["strike"], "oi": top_puts[0]["oi"], "rank": 1} if top_puts else {},
        "top_call_oi_strikes": top_calls,
        "top_put_oi_strikes": top_puts,
    }


def _participant_summary(raw_path: Path) -> List[Dict[str, Any]]:
    if not raw_path.exists():
        return []
    df = pd.read_csv(raw_path)
    if df.empty or len(df) < 2:
        return []
    header = df.iloc[0].tolist()
    rows: List[Dict[str, Any]] = []
    for _, row in df.iloc[1:].iterrows():
        label = str(row.iloc[0]).strip()
        if not label or label.upper() == "TOTAL":
            continue
        payload = {"participant": label}
        for idx, key in enumerate(header[1:], start=1):
            if idx >= len(row):
                break
            name = str(key).strip()
            if not name:
                continue
            try:
                payload[name] = int(str(row.iloc[idx]).replace(",", "").strip())
            except ValueError:
                value = row.iloc[idx]
                if pd.isna(value):
                    continue
                payload[name] = value
        rows.append(payload)
    return rows


def _raw_file_checks(day: date) -> Dict[str, bool]:
    folder = RAW_EOD_DIR / day.isoformat()
    ddmmyyyy = day.strftime("%d%m%Y")
    return {
        "fii_dii": (folder / f"fii_dii_{ddmmyyyy}.json").exists(),
        "india_vix": (folder / f"india_vix_{ddmmyyyy}.json").exists(),
        "equity_bhavcopy": bool(list(folder.glob("BhavCopy_NSE_CM*.zip"))),
        "delivery_report": (folder / f"sec_bhavdata_full_{ddmmyyyy}.csv").exists(),
        "mkt_activity_report": (folder / f"MA{ddmmyyyy}.csv").exists(),
        "fo_bhavcopy": bool(list(folder.glob("BhavCopy_NSE_FO*.zip"))),
        "participant_oi": (folder / f"fao_participant_oi_{ddmmyyyy}.csv").exists(),
        "participant_vol": (folder / f"fao_participant_vol_{ddmmyyyy}.csv").exists(),
    }


def _delivery_summary(day: date) -> Dict[str, Any]:
    path = RAW_EOD_DIR / day.isoformat() / f"sec_bhavdata_full_{day.strftime('%d%m%Y')}.csv"
    if not path.exists():
        return {"error": "delivery_report_missing"}

    from nifty.sources.nse_eod import read_delivery_report

    df = read_delivery_report(path)
    if df is None or df.empty:
        return {"error": "delivery_report_unreadable"}

    df = df.copy()
    df["Record Type"] = pd.to_numeric(df["Record Type"], errors="coerce")
    eq = df[(df["Record Type"] == 20) & (df["Series"].astype(str).str.upper() == "EQ")].copy()
    if eq.empty:
        return {"error": "no_eq_delivery_rows"}

    for col in ("Quantity Traded", "Deliverable Quantity(gross across client level)", "% of Deliverable Quantity to Traded Quantity"):
        eq[col] = pd.to_numeric(eq[col], errors="coerce")

    eq = eq.dropna(subset=["Quantity Traded", "Deliverable Quantity(gross across client level)"])
    total_traded = int(eq["Quantity Traded"].sum())
    total_delivered = int(eq["Deliverable Quantity(gross across client level)"].sum())
    market_delivery_pct = round((total_delivered / total_traded) * 100, 2) if total_traded else None

    def _row(row: pd.Series) -> Dict[str, Any]:
        return {
            "symbol": str(row["Name of Security"]),
            "traded_qty": int(row["Quantity Traded"]),
            "delivered_qty": int(row["Deliverable Quantity(gross across client level)"]),
            "delivery_pct": float(row["% of Deliverable Quantity to Traded Quantity"]),
        }

    top_delivery = [_row(row) for _, row in eq.nlargest(15, "Deliverable Quantity(gross across client level)").iterrows()]
    high_delivery = [
        _row(row)
        for _, row in eq[
            (eq["Quantity Traded"] >= 1_000_000) & (eq["% of Deliverable Quantity to Traded Quantity"] >= 55)
        ]
        .nlargest(15, "Deliverable Quantity(gross across client level)")
        .iterrows()
    ]

    nifty_watch = [
        "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "SBIN", "BHARTIARTL", "ITC", "LT",
        "AXISBANK", "KOTAKBANK", "HINDUNILVR", "BAJFINANCE", "MARUTI", "TATAMOTORS", "SUNPHARMA",
        "NTPC", "POWERGRID", "M&M", "HCLTECH", "TITAN", "ULTRACEMCO", "WIPRO", "NESTLEIND",
    ]
    index_rows = eq[eq["Name of Security"].isin(nifty_watch)].copy()
    index_rows = index_rows.sort_values("Deliverable Quantity(gross across client level)", ascending=False)
    index_delivery = [_row(row) for _, row in index_rows.iterrows()]

    return {
        "source_file": str(path),
        "series": "EQ",
        "stocks_count": int(len(eq)),
        "total_traded_qty": total_traded,
        "total_delivered_qty": total_delivered,
        "market_delivery_pct": market_delivery_pct,
        "read": (
            "Delivery % = share of today's volume taken into demat (not intraday squared off). "
            "High delivery + high volume = genuine accumulation/distribution."
        ),
        "top_by_delivered_qty": top_delivery,
        "high_delivery_pct_min_10L_traded_55pct": high_delivery,
        "nifty_heavyweights": index_delivery,
    }


def _manifest_status(day: date) -> Dict[str, Any]:
    path = RAW_EOD_DIR / day.isoformat() / "manifest.json"
    downloads = _raw_file_checks(day)
    manifest: Dict[str, Any] = {}
    if path.exists():
        manifest = json.loads(path.read_text(encoding="utf-8"))
        for item in manifest.get("downloads") or []:
            name = str(item.get("name") or "")
            if name:
                downloads[name] = downloads.get(name, False) or bool(item.get("ok"))
    return {
        "ok": path.exists(),
        "generated_at": manifest.get("generated_at"),
        "downloads": downloads,
        "imports": manifest.get("imports") or [],
    }


def build_eod_filing(trade_date: date) -> Dict[str, Any]:
    errors: List[str] = []
    label = trade_date.isoformat()

    fii_rows = [row for row in load_fii_dii_history(days=5) if row.get("trade_date") <= label]
    fii_trend = summarize_fii_trend(fii_rows)
    today_fii = next((row for row in reversed(fii_rows) if row.get("trade_date") == label), {})

    vix: Dict[str, Any] = {}
    if NSE_EOD_DB.exists():
        with sqlite3.connect(NSE_EOD_DB) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT last, open, high, low, previousclose, percentchange FROM india_vix WHERE trade_date = ? LIMIT 1",
                (label,),
            ).fetchone()
            if row:
                vix = dict(row)

    fo_path = _fo_bhav_path(trade_date)
    fo_df = _read_fo_bhavcopy(fo_path) if fo_path else None
    nifty_expiries: List[str] = []
    oi_maps: Dict[str, Any] = {}
    official_close: Optional[float] = None

    if fo_df is not None and not fo_df.empty:
        nifty_subset = fo_df[(fo_df["TckrSymb"] == "NIFTY") & (fo_df["OptnTp"].isin(["CE", "PE"]))]
        nifty_expiries = sorted(nifty_subset["XpryDt"].dropna().unique().tolist())
        if nifty_expiries:
            oi_maps["expired_weekly"] = _oi_map_for_expiry(fo_df, "NIFTY", nifty_expiries[0])
            official_close = oi_maps["expired_weekly"].get("underlying_price")
        next_nifty_expiry = next((exp for exp in nifty_expiries if exp > label), nifty_expiries[0] if nifty_expiries else None)
        if next_nifty_expiry and next_nifty_expiry != nifty_expiries[0]:
            oi_maps["next_weekly"] = _oi_map_for_expiry(fo_df, "NIFTY", next_nifty_expiry)
        elif len(nifty_expiries) > 1:
            oi_maps["next_weekly"] = _oi_map_for_expiry(fo_df, "NIFTY", nifty_expiries[1])

        bank_expiries = sorted(
            fo_df[(fo_df["TckrSymb"] == "BANKNIFTY") & (fo_df["OptnTp"].isin(["CE", "PE"]))]["XpryDt"]
            .dropna()
            .unique()
            .tolist()
        )
        next_bank_expiry = next((exp for exp in bank_expiries if exp > label), None)
        if next_bank_expiry:
            oi_maps["banknifty_next"] = _oi_map_for_expiry(fo_df, "BANKNIFTY", next_bank_expiry)
    else:
        errors.append("fo_bhavcopy_missing")

    participant_path = RAW_EOD_DIR / label / f"fao_participant_oi_{trade_date.strftime('%d%m%Y')}.csv"
    participant_oi = _participant_summary(participant_path)
    delivery = _delivery_summary(trade_date)

    manifest = _manifest_status(trade_date)
    cm_ok = all(
        manifest.get("downloads", {}).get(key)
        for key in ("equity_bhavcopy", "delivery_report", "mkt_activity_report")
    )
    fo_ok = all(manifest.get("downloads", {}).get(key) for key in ("fo_bhavcopy", "participant_oi", "participant_vol"))

    return {
        "trade_date": label,
        "recorded_at": ist_now(),
        "source": "desk_eod_filing.py",
        "manifest": manifest,
        "data_complete": {
            "fii_dii": bool(today_fii),
            "india_vix": bool(vix),
            "cm_reports": cm_ok,
            "fo_reports": fo_ok,
        },
        "nifty_official": {
            "close": official_close,
            "prev_close": 23123.0,
            "change_pts": round(official_close - 23123.0, 2) if official_close else None,
            "source": "fo_bhavcopy_underlying",
        },
        "fii_dii": {
            "date": today_fii.get("fii_dii_date"),
            "fii_net_crores": today_fii.get("fii_net_crores"),
            "dii_net_crores": today_fii.get("dii_net_crores"),
            "trend": fii_trend,
        },
        "india_vix": vix,
        "participant_oi_summary": participant_oi,
        "delivery_volume": delivery,
        "nifty_oi_maps": oi_maps,
        "nifty_expiries_in_bhavcopy": nifty_expiries,
        "errors": errors,
        "files": {
            "manifest": str(RAW_EOD_DIR / label / "manifest.json"),
            "sqlite": str(NSE_EOD_DB),
            "fo_bhavcopy": str(fo_path) if fo_path else None,
        },
    }


def main() -> None:
    args = parse_args()
    trade_date = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else previous_trading_day()
    payload = build_eod_filing(trade_date)

    store = NiftyJournalStore()
    filing_path = store.journal_dir / f"nse_eod_filing_{today_str(trade_date)}.json"
    store.write_json_snapshot(filing_path, payload)

    next_oi = payload.get("nifty_oi_maps", {}).get("next_weekly")
    if next_oi and not next_oi.get("error"):
        oi_eod_path = store.journal_dir / f"oi_map_eod_{today_str(trade_date)}.json"
        store.write_json_snapshot(
            oi_eod_path,
            {
                "trade_date": today_str(trade_date),
                "recorded_at": ist_now(),
                "source": "desk_eod_filing.py",
                "note": "Post-EOD FO bhavcopy — next weekly series for tomorrow morning reference",
                **next_oi,
            },
        )

    print(f"EOD filing written: {filing_path}")
    print(f"  FII net: {payload['fii_dii'].get('fii_net_crores')} Cr | DII: {payload['fii_dii'].get('dii_net_crores')} Cr")
    print(f"  VIX close: {payload.get('india_vix', {}).get('last')}")
    print(f"  NIFTY official close: {payload.get('nifty_official', {}).get('close')}")
    next_map = payload.get("nifty_oi_maps", {}).get("next_weekly") or {}
    if next_map.get("max_pain"):
        print(f"  Next weekly max pain ({next_map.get('expiry')}): {next_map.get('max_pain')}")


if __name__ == "__main__":
    main()
