#!/usr/bin/env python3
"""
Six-phase morning desk content capture (timing-agnostic).

Phases 1-4: global → India pre-market → instrument selection → OI map
Phase 5-6: live on dashboard (bias confirm/reject wired there)

Outputs under journal/:
  global_desk_YYYY-MM-DD.json
  morning_desk_YYYY-MM-DD.json
  instrument_selection_YYYY-MM-DD.json
  oi_map_YYYY-MM-DD.json
  key_levels_YYYY-MM-DD.json
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from nifty.sources.finstack import (
    fetch_forex,
    fetch_global_quotes,
    fetch_morning_fno_brief,
    fetch_nifty_outlook,
    fetch_options_oi_analytics,
    fetch_sector_performance,
    fetch_stock_quote,
    finstack_available,
)
from nifty.kite.spot import (
    cash_session_open,
    fetch_kite_nifty_quote,
    merge_nifty_snapshot,
    premarket_open_window,
    prior_session_candle,
)
from nifty.core.expiry import is_nifty_weekly_expiry
from nifty.sources.gift import build_gift_nifty_snapshot
from nifty.kite.key_levels import build_key_levels, fetch_daily_candles
from nifty.sources.oi_map import build_oi_map as build_nse_oi_map
from nifty.core.journal import NiftyJournalStore, ist_now, today_str

from nifty.paths import PROJECT_ROOT as BASE_DIR
NSE_EOD_DB = BASE_DIR / "data" / "nse_eod" / "nse_eod.sqlite"
RAW_EOD_DIR = BASE_DIR / "data" / "nse_eod" / "raw"
JOURNAL_DIR = BASE_DIR / "journal"

NSE_HOME = "https://www.nseindia.com"
FII_DII_URL = f"{NSE_HOME}/api/fiidiiTradeReact"
ALL_INDICES_URL = f"{NSE_HOME}/api/allIndices"

GLOBAL_SYMBOLS = {
    "sp500": "^GSPC",
    "nasdaq": "^IXIC",
    "dow": "^DJI",
    "nikkei": "^N225",
    "hang_seng": "^HSI",
    "crude_wti": "CL=F",
    "dxy": "DX-Y.NYB",
    "us_10y": "^TNX",
}


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


def fetch_fii_dii(session: requests.Session) -> List[Dict[str, Any]]:
    response = session.get(FII_DII_URL, timeout=20)
    response.raise_for_status()
    rows = response.json()
    return rows if isinstance(rows, list) else []


def fetch_index_row(session: requests.Session, symbol: str) -> Dict[str, Any]:
    response = session.get(ALL_INDICES_URL, timeout=20)
    response.raise_for_status()
    for row in response.json().get("data") or []:
        if str(row.get("indexSymbol") or row.get("index") or "").upper() == symbol.upper():
            return row
    return {}


def _parse_fii_date(raw: str) -> Optional[date]:
    for fmt in ("%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _fii_net_from_rows(rows: List[Dict[str, Any]]) -> Tuple[Optional[float], Optional[float], Optional[str]]:
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


def load_fii_dii_history(days: int = 5) -> List[Dict[str, Any]]:
    """Collect FII/DII rows from SQLite, raw JSON, and prior morning desk files."""
    by_date: Dict[str, Dict[str, Any]] = {}

    if NSE_EOD_DB.exists():
        with sqlite3.connect(NSE_EOD_DB) as conn:
            rows = conn.execute(
                """
                SELECT trade_date, category, netvalue, date
                FROM fii_dii
                ORDER BY trade_date DESC
                """
            ).fetchall()
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for trade_date, category, netvalue, label in rows:
            grouped.setdefault(str(trade_date), []).append(
                {"category": category, "netValue": netvalue, "date": label}
            )
        for trade_date, group in grouped.items():
            fii_net, dii_net, day_label = _fii_net_from_rows(group)
            by_date[trade_date] = {
                "trade_date": trade_date,
                "fii_net_crores": fii_net,
                "dii_net_crores": dii_net,
                "fii_dii_date": day_label,
                "source": "nse_eod_sqlite",
            }

    if RAW_EOD_DIR.exists():
        for folder in sorted(RAW_EOD_DIR.iterdir(), reverse=True):
            if not folder.is_dir():
                continue
            for path in folder.glob("fii_dii_*.json"):
                try:
                    rows = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(rows, list):
                    continue
                fii_net, dii_net, day_label = _fii_net_from_rows(rows)
                parsed = _parse_fii_date(day_label) or date.fromisoformat(folder.name)
                key = parsed.isoformat()
                by_date.setdefault(
                    key,
                    {
                        "trade_date": key,
                        "fii_net_crores": fii_net,
                        "dii_net_crores": dii_net,
                        "fii_dii_date": day_label,
                        "source": "nse_eod_raw",
                    },
                )

    for path in sorted(JOURNAL_DIR.glob("morning_desk_*.json"), reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        key = str(payload.get("trade_date") or path.stem.replace("morning_desk_", ""))
        if key in by_date:
            continue
        fii_net = _as_float(payload.get("fii_net_crores"))
        dii_net = _as_float(payload.get("dii_net_crores"))
        if fii_net is None and dii_net is None:
            continue
        by_date[key] = {
            "trade_date": key,
            "fii_net_crores": fii_net,
            "dii_net_crores": dii_net,
            "fii_dii_date": payload.get("fii_dii_date"),
            "source": "morning_desk_journal",
        }

    history = sorted(by_date.values(), key=lambda row: row["trade_date"], reverse=True)[:days]
    history.reverse()
    return history


def summarize_fii_trend(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    fii_values = [row["fii_net_crores"] for row in history if row.get("fii_net_crores") is not None]
    dii_values = [row["dii_net_crores"] for row in history if row.get("dii_net_crores") is not None]
    fii_5d = round(sum(fii_values), 2) if fii_values else None
    dii_5d = round(sum(dii_values), 2) if dii_values else None
    streak = "NEUTRAL"
    if fii_values:
        if all(v > 0 for v in fii_values[-3:]):
            streak = "FII_BUYING_STREAK"
        elif all(v < 0 for v in fii_values[-3:]):
            streak = "FII_SELLING_STREAK"
        elif fii_5d and fii_5d > 3000:
            streak = "FII_NET_BUY_5D"
        elif fii_5d and fii_5d < -3000:
            streak = "FII_NET_SELL_5D"
    return {
        "days": len(history),
        "fii_net_5d_crores": fii_5d,
        "dii_net_5d_crores": dii_5d,
        "daily": history,
        "streak_label": streak,
    }


def classify_candle(open_: float, high: float, low: float, close: float) -> Dict[str, Any]:
    body = abs(close - open_)
    full_range = max(high - low, 0.01)
    upper_wick = high - max(open_, close)
    lower_wick = min(open_, close) - low
    body_pct = round((body / full_range) * 100, 1)
    direction = "FLAT"
    if close > open_ + full_range * 0.05:
        direction = "BULLISH"
    elif close < open_ - full_range * 0.05:
        direction = "BEARISH"

    pattern = "INSIDE"
    if body_pct < 15 and upper_wick > body * 2 and lower_wick > body * 2:
        pattern = "DOJI"
    elif lower_wick > body * 2 and upper_wick < body:
        pattern = "HAMMER" if direction != "BEARISH" else "HANGING_MAN"
    elif upper_wick > body * 2 and lower_wick < body:
        pattern = "SHOOTING_STAR" if direction != "BULLISH" else "INVERTED_HAMMER"
    elif body_pct > 65:
        pattern = "MARUBOZU"
    elif direction == "BULLISH":
        pattern = "GREEN_DAY"
    elif direction == "BEARISH":
        pattern = "RED_DAY"

    return {
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "body_pct_of_range": body_pct,
        "direction": direction,
        "pattern": pattern,
    }


def build_prev_day_structure(
    nifty: Dict[str, Any],
    trade_date: Optional[date] = None,
) -> Dict[str, Any]:
    candles, candle_source = fetch_daily_candles()
    prior, prior_date = prior_session_candle(candles, trade_date)
    if prior:
        open_ = _as_float(prior.get("open"))
        high = _as_float(prior.get("high"))
        low = _as_float(prior.get("low"))
        last = _as_float(prior.get("close"))
        idx = candles.index(prior)
        prev_close = _as_float(candles[idx - 1].get("close")) if idx > 0 else None
        if None not in (last, open_, high, low):
            candle = classify_candle(open_, high, low, last)
            vs_prev = round(last - prev_close, 2) if prev_close else None
            vs_prev_pct = round((vs_prev / prev_close) * 100, 2) if prev_close and vs_prev is not None else None
            return {
                "session_date": prior_date,
                "source": candle_source,
                "prev_close": prev_close,
                "session_close": last,
                "change_vs_prev_close": vs_prev,
                "change_vs_prev_close_pct": vs_prev_pct,
                "close_vs_prev": "ABOVE" if vs_prev and vs_prev > 0 else ("BELOW" if vs_prev and vs_prev < 0 else "AT"),
                "candle": candle,
            }

    prev_close = _as_float(nifty.get("previous_close") or nifty.get("previousClose"))
    last = _as_float(nifty.get("last"))
    open_ = _as_float(nifty.get("open"))
    high = _as_float(nifty.get("high"))
    low = _as_float(nifty.get("low"))
    if None in (prev_close, last, open_, high, low) or open_ == 0:
        return {"error": "incomplete_nifty_ohlc", "source": "nse_fallback"}

    candle = classify_candle(open_, high, low, last)
    vs_prev = round(last - prev_close, 2)
    vs_prev_pct = round((vs_prev / prev_close) * 100, 2) if prev_close else None
    return {
        "source": "nse_fallback",
        "prev_close": prev_close,
        "session_close": last,
        "change_vs_prev_close": vs_prev,
        "change_vs_prev_close_pct": vs_prev_pct,
        "close_vs_prev": "ABOVE" if vs_prev > 0 else ("BELOW" if vs_prev < 0 else "AT"),
        "candle": candle,
    }


def _bias_label_from_score(score: float) -> str:
    if score >= 1.5:
        return "BULLISH"
    if score >= 0.5:
        return "CAUTIOUSLY_BULLISH"
    if score <= -1.5:
        return "BEARISH"
    if score <= -0.5:
        return "CAUTIOUSLY_BEARISH"
    return "NEUTRAL"


def derive_global_bias(global_quotes: Dict[str, Any], gift: Dict[str, Any], macro: Dict[str, Any]) -> Dict[str, Any]:
    score = 0.0
    factors: List[str] = []
    quotes = global_quotes.get("quotes") or {}

    def _pct(symbol: str) -> Optional[float]:
        row = quotes.get(symbol) or {}
        return _as_float(row.get("change_pct"))

    for symbol, weight in (("sp500", 1.0), ("nasdaq", 0.8), ("dow", 0.6)):
        pct = _pct(GLOBAL_SYMBOLS[symbol])
        if pct is None:
            continue
        if pct > 0.3:
            score += weight
            factors.append(f"{symbol} +{pct:.2f}%")
        elif pct < -0.3:
            score -= weight
            factors.append(f"{symbol} {pct:.2f}%")

    for symbol, weight in (("nikkei", 0.5), ("hang_seng", 0.5)):
        pct = _pct(GLOBAL_SYMBOLS[symbol])
        if pct is None:
            continue
        if pct > 0.3:
            score += weight
            factors.append(f"{symbol} +{pct:.2f}%")
        elif pct < -0.3:
            score -= weight
            factors.append(f"{symbol} {pct:.2f}%")

    crude = _as_float((quotes.get(GLOBAL_SYMBOLS["crude_wti"]) or {}).get("change_pct"))
    if crude is not None:
        if crude > 1.0:
            score -= 0.4
            factors.append(f"crude up {crude:.2f}% (inflation pressure)")
        elif crude < -1.0:
            score += 0.2
            factors.append(f"crude down {crude:.2f}%")

    dxy = _as_float((quotes.get(GLOBAL_SYMBOLS["dxy"]) or {}).get("change_pct"))
    if dxy is not None:
        if dxy > 0.2:
            score -= 0.3
            factors.append(f"DXY firm +{dxy:.2f}%")
        elif dxy < -0.2:
            score += 0.2
            factors.append(f"DXY weak {dxy:.2f}%")

    us10y = _as_float((quotes.get(GLOBAL_SYMBOLS["us_10y"]) or {}).get("change_pct"))
    if us10y is not None and abs(us10y) > 0.5:
        if us10y > 0:
            score -= 0.3
            factors.append(f"US 10Y yields rising {us10y:.2f}%")
        else:
            score += 0.2
            factors.append(f"US 10Y yields easing {us10y:.2f}%")

    gift_bias = str(gift.get("overnight_bias") or "")
    if gift_bias == "GAP_UP":
        score += 0.8
        factors.append("GIFT premium → gap-up bias")
    elif gift_bias == "GAP_DOWN":
        score -= 0.8
        factors.append("GIFT discount → gap-down bias")

    label = _bias_label_from_score(score)
    return {"label": label, "score": round(score, 2), "factors": factors}


def build_global_desk(trade_date: Optional[date] = None) -> Dict[str, Any]:
    errors: List[str] = []
    gift: Dict[str, Any] = {}
    try:
        gift = build_gift_nifty_snapshot()
    except Exception as exc:
        errors.append(f"gift: {exc}")

    global_quotes = fetch_global_quotes(list(GLOBAL_SYMBOLS.values()))
    errors.extend(global_quotes.get("errors") or [])

    usd_inr = fetch_forex("USD", "INR")
    macro = {
        "usd_inr": usd_inr,
        "crude_wti": global_quotes.get("quotes", {}).get(GLOBAL_SYMBOLS["crude_wti"]),
        "dxy": global_quotes.get("quotes", {}).get(GLOBAL_SYMBOLS["dxy"]),
        "us_10y_yield": global_quotes.get("quotes", {}).get(GLOBAL_SYMBOLS["us_10y"]),
    }
    global_bias = derive_global_bias(global_quotes, gift, macro)

    movers: List[Dict[str, Any]] = []
    for key, symbol in GLOBAL_SYMBOLS.items():
        row = (global_quotes.get("quotes") or {}).get(symbol) or {}
        if row.get("error"):
            continue
        movers.append(
            {
                "id": key,
                "symbol": symbol,
                "name": row.get("name") or key,
                "price": row.get("price"),
                "change_pct": row.get("change_pct"),
            }
        )
    movers.sort(key=lambda item: abs(_as_float(item.get("change_pct")) or 0), reverse=True)

    return {
        "trade_date": today_str(trade_date),
        "captured_at": ist_now(),
        "source": "desk_phase_capture.py",
        "phase": 1,
        "finstack_available": finstack_available(),
        "gift_nifty": gift,
        "global_movers": movers,
        "macro": macro,
        "global_bias": global_bias,
        "errors": errors,
    }


def analyze_sectors(sector_payload: Dict[str, Any]) -> Dict[str, Any]:
    sectors = sector_payload.get("sectors") or []
    if not sectors:
        return {"error": "no_sector_data", "sectors": []}

    green = [row for row in sectors if _as_float(row.get("change_pct")) > 0]
    red = [row for row in sectors if _as_float(row.get("change_pct")) < 0]
    flat = [row for row in sectors if _as_float(row.get("change_pct")) == 0]
    total = len(sectors)
    green_pct = round((len(green) / total) * 100, 1) if total else 0

    best = sector_payload.get("best_performer") or (max(sectors, key=lambda r: _as_float(r.get("change_pct")) or -999))
    worst = sector_payload.get("worst_performer") or (min(sectors, key=lambda r: _as_float(r.get("change_pct")) or 999))
    spread = round(
        (_as_float(best.get("change_pct")) or 0) - (_as_float(worst.get("change_pct")) or 0),
        2,
    )

    nifty_row = next((row for row in sectors if "Nifty 50" in str(row.get("sector", ""))), None)
    bank_row = next((row for row in sectors if "Bank" in str(row.get("sector", ""))), None)
    aligned = "MIXED"
    if green_pct >= 70:
        aligned = "BROAD_RALLY"
    elif green_pct <= 30:
        aligned = "BROAD_WEAKNESS"
    elif nifty_row and bank_row:
        n_chg = _as_float(nifty_row.get("change_pct")) or 0
        b_chg = _as_float(bank_row.get("change_pct")) or 0
        if n_chg > 0 and b_chg > 0:
            aligned = "NIFTY_BANK_ALIGNED_UP"
        elif n_chg < 0 and b_chg < 0:
            aligned = "NIFTY_BANK_ALIGNED_DOWN"
        elif (n_chg > 0) != (b_chg > 0):
            aligned = "NIFTY_BANK_DIVERGING"

    return {
        "sectors": sectors,
        "breadth": {
            "green": len(green),
            "red": len(red),
            "flat": len(flat),
            "total": total,
            "green_pct": green_pct,
        },
        "best_performer": best,
        "worst_performer": worst,
        "sector_spread_pct": spread,
        "alignment_label": aligned,
        "nifty_vs_bank": {
            "nifty_change_pct": _as_float((nifty_row or {}).get("change_pct")),
            "bank_change_pct": _as_float((bank_row or {}).get("change_pct")),
        },
    }


def derive_india_bias(
    fii_trend: Dict[str, Any],
    vix: Dict[str, Any],
    prev_structure: Dict[str, Any],
    sector_scan: Dict[str, Any],
    nifty_outlook: Dict[str, Any],
    gift: Dict[str, Any],
) -> Dict[str, Any]:
    score = 0.0
    factors: List[str] = []

    fii_5d = _as_float(fii_trend.get("fii_net_5d_crores"))
    if fii_5d is not None:
        if fii_5d > 3000:
            score += 1.0
            factors.append(f"FII 5d net +{fii_5d:.0f} Cr")
        elif fii_5d > 1000:
            score += 0.5
            factors.append(f"FII 5d net +{fii_5d:.0f} Cr (mild)")
        elif fii_5d < -3000:
            score -= 1.0
            factors.append(f"FII 5d net {fii_5d:.0f} Cr")
        elif fii_5d < -1000:
            score -= 0.5
            factors.append(f"FII 5d net {fii_5d:.0f} Cr (mild)")

    vix_chg = _as_float(vix.get("percent_change") or vix.get("percentChange"))
    vix_last = _as_float(vix.get("last"))
    if vix_last is not None:
        if vix_last > 18:
            score -= 0.5
            factors.append(f"VIX elevated {vix_last}")
        elif vix_last < 13:
            score += 0.3
            factors.append(f"VIX calm {vix_last}")
    if vix_chg is not None and vix_chg > 5:
        score -= 0.4
        factors.append(f"VIX spiked +{vix_chg:.1f}%")

    candle = (prev_structure.get("candle") or {})
    if candle.get("direction") == "BULLISH":
        score += 0.4
        factors.append(f"Prev day {candle.get('pattern')} bullish")
    elif candle.get("direction") == "BEARISH":
        score -= 0.4
        factors.append(f"Prev day {candle.get('pattern')} bearish")

    alignment = sector_scan.get("alignment_label")
    if alignment == "BROAD_RALLY":
        score += 0.6
        factors.append("Sector breadth broad rally")
    elif alignment == "BROAD_WEAKNESS":
        score -= 0.6
        factors.append("Sector breadth broad weakness")
    elif alignment == "NIFTY_BANK_DIVERGING":
        factors.append("Nifty vs Bank diverging — stock-picking day")

    outlook_signal = str(nifty_outlook.get("signal") or "")
    prob_up = _as_float(nifty_outlook.get("probability_up"))
    if "Bullish" in outlook_signal:
        score += 0.5
        factors.append(f"Nifty outlook {outlook_signal} ({prob_up}%)")
    elif "Bearish" in outlook_signal:
        score -= 0.5
        factors.append(f"Nifty outlook {outlook_signal} ({prob_up}%)")

    gift_bias = str(gift.get("overnight_bias") or "")
    cash_gap = gift.get("cash_open_gap") or {}
    cash_gap_type = str(cash_gap.get("gap_type") or "")
    if cash_gap_type in {"GAP_UP", "MILD_UP"}:
        score += 0.5 if cash_gap_type == "GAP_UP" else 0.25
        factors.append(f"Cash open {cash_gap_type} {cash_gap.get('gap_pts')} pts")
    elif cash_gap_type in {"GAP_DOWN", "MILD_DOWN"}:
        score -= 0.5 if cash_gap_type == "GAP_DOWN" else 0.25
        factors.append(f"Cash open {cash_gap_type} {cash_gap.get('gap_pts')} pts")
    elif gift_bias == "GAP_UP":
        score += 0.5
        factors.append("GIFT gap-up")
    elif gift_bias == "GAP_DOWN":
        score -= 0.5
        factors.append("GIFT gap-down")

    label = _bias_label_from_score(score)
    if nifty_outlook.get("signal") and label == "NEUTRAL":
        label = str(nifty_outlook.get("signal")).upper().replace(" ", "_")

    return {
        "label": label,
        "score": round(score, 2),
        "factors": factors,
        "nifty_outlook": {
            "probability_up": prob_up,
            "signal": outlook_signal,
            "bull_factors": nifty_outlook.get("bull_factors"),
            "bear_factors": nifty_outlook.get("bear_factors"),
        },
    }


def build_india_premarket(
    trade_date: Optional[date] = None,
    global_desk: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    session = nse_session()
    fii_dii = fetch_fii_dii(session)
    nifty_row = fetch_index_row(session, "NIFTY 50")
    kite_quote = fetch_kite_nifty_quote()
    nifty = merge_nifty_snapshot(nifty_row, kite_quote, now=datetime.now())
    vix = fetch_index_row(session, "INDIA VIX")
    fii_net, dii_net, fii_day = _fii_net_from_rows(fii_dii)

    gift = (global_desk or {}).get("gift_nifty") or {}
    gift_errors: List[str] = []
    if not gift:
        try:
            gift = build_gift_nifty_snapshot()
        except Exception as exc:
            gift_errors.append(str(exc))

    fii_trend = summarize_fii_trend(load_fii_dii_history(days=5))
    prev_structure = build_prev_day_structure(nifty, trade_date=trade_date)

    cash_gap = nifty.get("open_gap") or {}
    if (cash_session_open() or premarket_open_window()) and cash_gap.get("gap_type") not in (None, "UNKNOWN"):
        gift = dict(gift)
        gift["cash_open_gap"] = cash_gap
        if premarket_open_window() and not cash_session_open():
            gift["overnight_bias_note"] = (
                f"Pre-open indicative {cash_gap.get('gap_type')} {cash_gap.get('gap_pts')} pts "
                f"(Kite @ pre-market scan — confirm at 9:15 cash open)"
            )
        else:
            gift["overnight_bias_note"] = (
                f"Cash open {cash_gap.get('gap_type')} {cash_gap.get('gap_pts')} pts "
                f"(Kite/NSE at capture) — overrides GIFT-only pre-open read"
            )

    sector_raw = fetch_sector_performance()
    sector_scan = analyze_sectors(sector_raw)
    nifty_outlook = fetch_nifty_outlook()
    finstack_brief = fetch_morning_fno_brief()

    india_bias = derive_india_bias(fii_trend, vix, prev_structure, sector_scan, nifty_outlook, gift)

    return {
        "trade_date": today_str(trade_date),
        "captured_at": ist_now(),
        "source": "desk_phase_capture.py",
        "phase": 2,
        "fii_dii": fii_dii,
        "fii_net_crores": fii_net,
        "dii_net_crores": dii_net,
        "fii_dii_date": fii_day,
        "fii_dii_trend": fii_trend,
        "nifty": {
            "last": nifty.get("last"),
            "open": nifty.get("open"),
            "high": nifty.get("high"),
            "low": nifty.get("low"),
            "previous_close": nifty.get("previous_close"),
            "percent_change": nifty.get("percent_change"),
            "primary_source": nifty.get("primary_source"),
            "data_quality": nifty.get("data_quality"),
            "open_gap": nifty.get("open_gap"),
            "kite": nifty.get("kite"),
            "nse_raw": {
                "last": nifty.get("raw_last"),
                "open": nifty.get("raw_open"),
            },
        },
        "india_vix": {
            "last": vix.get("last"),
            "open": vix.get("open"),
            "high": vix.get("high"),
            "low": vix.get("low"),
            "previous_close": vix.get("previousClose"),
            "percent_change": vix.get("percentChange"),
        },
        "prev_day_structure": prev_structure,
        "sector_scan": sector_scan,
        "india_bias": india_bias,
        "gift_nifty": gift,
        "finstack_morning_fno_brief": finstack_brief,
        "errors": gift_errors,
    }


def build_instrument_selection(
    global_desk: Dict[str, Any],
    india_desk: Dict[str, Any],
    trade_date: Optional[date] = None,
) -> Dict[str, Any]:
    global_bias = (global_desk.get("global_bias") or {}).get("label", "NEUTRAL")
    india_bias = (india_desk.get("india_bias") or {}).get("label", "NEUTRAL")
    sector_scan = india_desk.get("sector_scan") or {}
    breadth = sector_scan.get("breadth") or {}
    alignment = sector_scan.get("alignment_label", "MIXED")
    best = sector_scan.get("best_performer") or {}
    worst = sector_scan.get("worst_performer") or {}

    combined_score = (
        (_as_float((global_desk.get("global_bias") or {}).get("score")) or 0)
        + (_as_float((india_desk.get("india_bias") or {}).get("score")) or 0)
    )
    combined_bias = _bias_label_from_score(combined_score)

    green_pct = _as_float(breadth.get("green_pct")) or 0
    spread = _as_float(sector_scan.get("sector_spread_pct")) or 0
    conviction = "MEDIUM"
    instrument = "NIFTY"
    mode = "INDEX"
    rationale: List[str] = []
    day = trade_date or date.today()
    expiry_day = is_nifty_weekly_expiry(day)

    if expiry_day:
        instrument = "BANKNIFTY"
        mode = "INDEX"
        conviction = "MEDIUM"
        rationale.append("Expiry day — BankNifty primary (support 54,000–53,800 zone)")
        rationale.append("Nifty options only after 9:45 — no Nifty options in first 30 min")
        rationale.append("Observe 9:15–9:45 — ORB trap + delta hedging on expiry")
        if global_bias.startswith("BEAR") or india_bias.startswith("BEAR"):
            rationale.append("Bearish context — bullish signals blocked until 10:30")

    if green_pct < 35 and spread > 1.5 and abs(combined_score) < 1.0 and not expiry_day:
        instrument = "NO_TRADE"
        mode = "STAND_ASIDE"
        conviction = "LOW"
        rationale.append("Sectors mixed, no clear alignment — no-trade filter")
    elif alignment == "NIFTY_BANK_DIVERGING":
        bank_chg = _as_float((sector_scan.get("nifty_vs_bank") or {}).get("bank_change_pct")) or 0
        nifty_chg = _as_float((sector_scan.get("nifty_vs_bank") or {}).get("nifty_change_pct")) or 0
        if abs(bank_chg) > abs(nifty_chg) + 0.3:
            instrument = "BANKNIFTY"
            mode = "INDEX"
            conviction = "MEDIUM"
            rationale.append("Bank Nifty leading/diverging vs Nifty — trade bank index")
        else:
            leader = best.get("sector", "SECTOR")
            instrument = str(leader).upper().replace(" ", "_")
            mode = "SECTOR_INDEX"
            conviction = "MEDIUM"
            rationale.append(f"Sector rotation — leader {leader}")
    elif alignment in {"BROAD_RALLY", "BROAD_WEAKNESS", "NIFTY_BANK_ALIGNED_UP", "NIFTY_BANK_ALIGNED_DOWN"}:
        instrument = "NIFTY"
        mode = "INDEX"
        conviction = "HIGH" if abs(combined_score) >= 1.5 else "MEDIUM"
        rationale.append(f"Sectors aligned ({alignment}) — Nifty index trade")
    else:
        instrument = "NIFTY"
        mode = "INDEX"
        conviction = "MEDIUM"
        rationale.append("Default Nifty weekly F&O — desk specialization")

    fighting_move = False
    if global_bias.startswith("BULL") and india_bias.startswith("BEAR"):
        fighting_move = True
        rationale.append("Global bullish vs India bearish — context conflict, size down")
    elif global_bias.startswith("BEAR") and india_bias.startswith("BULL"):
        fighting_move = True
        rationale.append("Global bearish vs India bullish — context conflict, size down")

    if combined_bias == "NEUTRAL" and instrument != "NO_TRADE":
        conviction = "LOW" if conviction == "HIGH" else conviction
        rationale.append("Combined bias neutral — wait for participant confirmation")

    return {
        "trade_date": today_str(trade_date),
        "captured_at": ist_now(),
        "source": "desk_phase_capture.py",
        "phase": 3,
        "instrument": instrument,
        "mode": mode,
        "conviction": conviction,
        "combined_bias": combined_bias,
        "combined_score": round(combined_score, 2),
        "global_bias": global_bias,
        "india_bias": india_bias,
        "sector_alignment": alignment,
        "sector_breadth": breadth,
        "best_sector": best,
        "worst_sector": worst,
        "fighting_move": fighting_move,
        "rationale": rationale,
        "no_trade": instrument == "NO_TRADE",
        "is_expiry_day": expiry_day,
        "secondary_instrument": "NIFTY" if expiry_day else None,
        "observe_until": "09:45 IST" if expiry_day else "09:30 IST",
    }


def _normalize_symbol_for_oi(instrument: str) -> str:
    mapping = {
        "NIFTY": "NIFTY",
        "BANKNIFTY": "BANKNIFTY",
        "NIFTY_BANK": "BANKNIFTY",
        "BANK_NIFTY": "BANKNIFTY",
        "NO_TRADE": "NIFTY",
    }
    key = instrument.upper().replace(" ", "_")
    if key in mapping:
        return mapping[key]
    if "BANK" in key:
        return "BANKNIFTY"
    return "NIFTY"


def build_oi_map(
    instrument_selection: Dict[str, Any],
    trade_date: Optional[date] = None,
) -> Dict[str, Any]:
    chosen = str(instrument_selection.get("instrument") or "NIFTY")
    symbol = _normalize_symbol_for_oi(chosen)
    nse_map = build_nse_oi_map(symbol, trade_date=trade_date)
    finstack = fetch_options_oi_analytics(symbol)

    source = nse_map.get("source") if not nse_map.get("error") else "finstack_or_none"
    analytics = nse_map if not nse_map.get("error") else finstack

    if nse_map.get("error") and not finstack.get("error"):
        analysis = (finstack.get("analysis") or [])
        if analysis:
            front = analysis[0]
            top_calls = front.get("top_call_oi_strikes") or []
            top_puts = front.get("top_put_oi_strikes") or []
            analytics = {
                "source": "finstack_options_oi_analytics",
                "symbol": symbol,
                "expiry": front.get("expiry"),
                "underlying_price": finstack.get("underlying_price"),
                "max_pain": front.get("max_pain"),
                "max_pain_vs_spot": front.get("max_pain_vs_spot"),
                "pcr_oi": front.get("pcr_oi"),
                "ceiling": {"strike": top_calls[0].get("strike"), "oi": top_calls[0].get("oi"), "rank": 1}
                if top_calls
                else {},
                "floor": {"strike": top_puts[0].get("strike"), "oi": top_puts[0].get("oi"), "rank": 1}
                if top_puts
                else {},
                "top_call_oi_strikes": top_calls[:5],
                "top_put_oi_strikes": top_puts[:5],
            }
            source = analytics["source"]

    ceiling = analytics.get("ceiling") or {}
    floor = analytics.get("floor") or {}
    underlying = _as_float(analytics.get("underlying_price"))
    return {
        "trade_date": today_str(trade_date),
        "captured_at": ist_now(),
        "source": "desk_phase_capture.py",
        "phase": 4,
        "chosen_instrument": chosen,
        "symbol": symbol,
        "data_source": source,
        "underlying_price": underlying,
        "expiry": analytics.get("expiry"),
        "max_pain": analytics.get("max_pain"),
        "max_pain_vs_spot": analytics.get("max_pain_vs_spot"),
        "pcr_oi": analytics.get("pcr_oi"),
        "ceiling": ceiling,
        "floor": floor,
        "top_call_oi_strikes": analytics.get("top_call_oi_strikes") or [],
        "top_put_oi_strikes": analytics.get("top_put_oi_strikes") or [],
        "raw_analytics": {"nse": nse_map, "finstack": finstack},
        "map_note": "CE ceiling = highest call OI strike (resistance). PE floor = highest put OI strike (support).",
    }


def run_morning_pipeline(trade_date: Optional[date] = None) -> Dict[str, Any]:
    """Run phases 1-4 and persist all journal artifacts."""
    store = NiftyJournalStore()
    day = trade_date or date.today()

    global_desk = build_global_desk(day)
    india_desk = build_india_premarket(day, global_desk=global_desk)
    instrument = build_instrument_selection(global_desk, india_desk, day)
    oi_map = build_oi_map(instrument, day)

    store.write_global_desk(global_desk, day=day)
    store.write_morning_desk(india_desk, day=day)
    store.write_instrument_selection(instrument, day=day)
    store.write_oi_map(oi_map, day=day)
    spot = _as_float((india_desk.get("nifty") or {}).get("last"))
    if not spot or spot <= 0:
        gap = (india_desk.get("nifty") or {}).get("open_gap") or {}
        spot = _as_float(gap.get("reference_open"))
    key_levels = build_key_levels(
        spot=float(spot or 0),
        morning_nifty=india_desk.get("nifty"),
        oi_map=oi_map,
        trade_date=day,
    )
    store.write_key_levels(key_levels, day=day)

    summary = {
        "trade_date": today_str(day),
        "captured_at": ist_now(),
        "phases_completed": [1, 2, 3, 4],
        "global_bias": global_desk.get("global_bias", {}).get("label"),
        "india_bias": india_desk.get("india_bias", {}).get("label"),
        "combined_bias": instrument.get("combined_bias"),
        "instrument": instrument.get("instrument"),
        "conviction": instrument.get("conviction"),
        "oi_ceiling": (oi_map.get("ceiling") or {}).get("strike"),
        "oi_floor": (oi_map.get("floor") or {}).get("strike"),
        "max_pain": oi_map.get("max_pain"),
        "files": {
            "global_desk": str(store.journal_dir / f"global_desk_{today_str(day)}.json"),
            "morning_desk": str(store.journal_dir / f"morning_desk_{today_str(day)}.json"),
            "instrument_selection": str(store.journal_dir / f"instrument_selection_{today_str(day)}.json"),
            "oi_map": str(store.journal_dir / f"oi_map_{today_str(day)}.json"),
            "key_levels": str(store.journal_dir / f"key_levels_{today_str(day)}.json"),
        },
    }
    store.write_json_snapshot(store.journal_dir / f"morning_pipeline_{today_str(day)}.json", summary)
    return {
        "summary": summary,
        "global_desk": global_desk,
        "morning_desk": india_desk,
        "instrument_selection": instrument,
        "oi_map": oi_map,
        "key_levels": key_levels,
    }


def _read_journal_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def build_auto_desk_brief(
    global_desk: Dict[str, Any],
    india_desk: Dict[str, Any],
    instrument: Dict[str, Any],
    oi_map: Dict[str, Any],
    key_levels: Dict[str, Any],
    trade_date: Optional[date] = None,
) -> Dict[str, Any]:
    """Desk brief at 9:01 — merges auto fields with any manual scenarios from earlier capture."""
    day = trade_date or date.today()
    label = today_str(day)
    existing = _read_journal_json(JOURNAL_DIR / f"desk_brief_{label}.json")

    nifty = india_desk.get("nifty") or {}
    gift = india_desk.get("gift_nifty") or {}
    prev = india_desk.get("prev_day_structure") or {}
    open_gap = nifty.get("open_gap") or gift.get("cash_open_gap") or {}
    prev_close = _as_float(nifty.get("previous_close") or prev.get("prev_close"))
    kite_last = _as_float((nifty.get("kite") or {}).get("last") or nifty.get("last"))
    global_bias = (global_desk.get("global_bias") or {}).get("label", "NEUTRAL")
    combined_bias = instrument.get("combined_bias", "NEUTRAL")
    max_pain = oi_map.get("max_pain")
    floor_strike = _as_float((oi_map.get("floor") or {}).get("strike"))
    ceiling_strike = _as_float((oi_map.get("ceiling") or {}).get("strike"))
    gift_premium = _as_float(gift.get("premium_vs_nse_close") or gift.get("premium_pts"))

    gap_type = str(open_gap.get("gap_type") or gift.get("overnight_bias") or "UNKNOWN")
    gap_pts = open_gap.get("gap_pts")
    narrative = (
        f"Pre-market 9:01 — Kite {kite_last or '?'} vs {prev_close or '?'} close, "
        f"gap {gap_type}"
        + (f" {gap_pts} pts" if gap_pts is not None else "")
        + f". GIFT premium {gift_premium or '?'} pts. "
        f"Bias {combined_bias}. MP {max_pain or '?'}. Confirm at 9:15."
    )

    brief = {
        "trade_date": label,
        "title": existing.get("title") or f"Pre-Market Scan — {day.strftime('%d %B %Y')}",
        "captured_at": ist_now(),
        "narrative_one_line": narrative,
        "morning_bias": (india_desk.get("india_bias") or {}).get("label"),
        "combined_bias": combined_bias,
        "global_verdict": f"{global_bias} — {', '.join((global_desk.get('global_bias') or {}).get('factors') or [])[:120]}",
        "is_expiry_day": bool(instrument.get("is_expiry_day")),
        "max_pain": max_pain,
        "prev_close": prev_close,
        "yesterday_low": _as_float(prev.get("candle", {}).get("low")),
        "yesterday_high": _as_float(prev.get("candle", {}).get("high")),
        "kite_preopen": kite_last,
        "cash_open_gap": {
            **open_gap,
            "note": "Pre-9:15 Kite indicative — confirm at cash open",
        },
        "key_range": {
            "support": floor_strike or _as_float((key_levels.get("pivots") or {}).get("s1")),
            "resistance": ceiling_strike or _as_float((key_levels.get("pivots") or {}).get("r1")),
            "max_pain": max_pain,
        },
        "primary_instrument": instrument.get("instrument"),
        "secondary_instrument": instrument.get("secondary_instrument"),
        "observe_until": instrument.get("observe_until") or "09:30 IST",
        "nifty_options_from": "09:30 IST",
        "gift_overnight": {
            "premium_pts": gift_premium,
            "bias": gift.get("overnight_bias"),
            "gift_last": _as_float(gift.get("gift_last") or gift.get("last")),
            "nse_close_ref": prev_close,
        },
        "source": f"premarket_scan_{label}_0901",
    }
    for key in ("scenarios", "entry_rules"):
        if existing.get(key):
            brief[key] = existing[key]
    return brief


def run_premarket_scan(trade_date: Optional[date] = None) -> Dict[str, Any]:
    """
    9:01 IST pre-open refresh — GIFT, Kite indicative gap, bias, key levels, desk brief.
    Requires morning pipeline (08:15) for OI map baseline; runs full morning if missing.
    """
    store = NiftyJournalStore()
    day = trade_date or date.today()
    label = today_str(day)
    errors: List[str] = []

    global_path = JOURNAL_DIR / f"global_desk_{label}.json"
    if not global_path.exists():
        return run_morning_pipeline(day)

    global_desk = _read_journal_json(global_path)
    try:
        gift = build_gift_nifty_snapshot()
        global_desk = dict(global_desk)
        global_desk["gift_nifty"] = gift
        global_desk["captured_at"] = ist_now()
        global_desk["premarket_refresh_at"] = ist_now()
    except Exception as exc:
        errors.append(f"gift_refresh: {exc}")

    india_desk = build_india_premarket(day, global_desk=global_desk)
    india_desk["scan_type"] = "pre_open_901"
    india_desk["phase"] = "premarket_scan"
    if errors:
        india_desk.setdefault("errors", []).extend(errors)

    instrument = build_instrument_selection(global_desk, india_desk, day)
    instrument["phase"] = "premarket_scan"

    oi_path = JOURNAL_DIR / f"oi_map_{label}.json"
    oi_map = _read_journal_json(oi_path)
    if not oi_map:
        oi_map = build_oi_map(instrument, day)

    spot = _as_float((india_desk.get("nifty") or {}).get("last"))
    if not spot or spot <= 0:
        gap = (india_desk.get("nifty") or {}).get("open_gap") or {}
        spot = _as_float(gap.get("reference_open"))
    key_levels = build_key_levels(
        spot=float(spot or 0),
        morning_nifty=india_desk.get("nifty"),
        oi_map=oi_map,
        trade_date=day,
    )
    key_levels["phase"] = "premarket_scan"

    desk_brief = build_auto_desk_brief(global_desk, india_desk, instrument, oi_map, key_levels, day)

    store.write_global_desk(global_desk, day=day)
    store.write_morning_desk(india_desk, day=day)
    store.write_instrument_selection(instrument, day=day)
    store.write_key_levels(key_levels, day=day)
    store.write_json_snapshot(JOURNAL_DIR / f"desk_brief_{label}.json", desk_brief)

    summary = {
        "trade_date": label,
        "captured_at": ist_now(),
        "scan_type": "pre_open_901",
        "combined_bias": instrument.get("combined_bias"),
        "instrument": instrument.get("instrument"),
        "kite_preopen": desk_brief.get("kite_preopen"),
        "cash_open_gap": desk_brief.get("cash_open_gap"),
        "gift_premium_pts": (desk_brief.get("gift_overnight") or {}).get("premium_pts"),
        "max_pain": oi_map.get("max_pain"),
        "narrative_one_line": desk_brief.get("narrative_one_line"),
        "errors": errors,
        "files": {
            "global_desk": str(global_path),
            "morning_desk": str(JOURNAL_DIR / f"morning_desk_{label}.json"),
            "instrument_selection": str(JOURNAL_DIR / f"instrument_selection_{label}.json"),
            "key_levels": str(JOURNAL_DIR / f"key_levels_{label}.json"),
            "desk_brief": str(JOURNAL_DIR / f"desk_brief_{label}.json"),
            "premarket_scan": str(JOURNAL_DIR / f"premarket_scan_{label}.json"),
        },
    }
    store.write_json_snapshot(JOURNAL_DIR / f"premarket_scan_{label}.json", summary)

    return {
        "summary": summary,
        "global_desk": global_desk,
        "morning_desk": india_desk,
        "instrument_selection": instrument,
        "oi_map": oi_map,
        "key_levels": key_levels,
        "desk_brief": desk_brief,
    }
