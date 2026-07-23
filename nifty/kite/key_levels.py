#!/usr/bin/env python3
"""
Institutional + retail key level pack for NIFTY 50.

Computes from daily OHLC (Kite → yfinance ^NSEI → morning desk fallback):
  - Classical pivot points (R3–S3, PP)
  - Camarilla (CR4–CR1, CS1–CS4)
  - EMA 9/21/50/200 + SMA 20/50/100/200 with spot distance
  - Fibonacci retracements (52W swing)
  - Period highs/lows (52W, 6M, month, week) + range position
  - ATR(14)
  - OI ceiling/floor/max pain with OI in lakhs (from oi_map)
  - Bollinger (20, 2σ) and Keltner (EMA20 ± 2×ATR14) on daily series
  - Session VWAP proxy (tick TWAP from 09:15 IST)

UPDATED 2026-07-24 to add Bollinger/Keltner/session-TWAP from
quant-desk-engine v4/ATLAS's evolved nifty_key_levels.py (mentor-authored),
per user-approved direction. These are additive new payload fields only —
deliberately NOT added to flatten_levels_for_alerts's input dict, so they
cannot become new "key area" zones for OI alerts (the user's flagged
display-only choice). build_key_levels() gained one new optional
`spot_history` parameter (default None); existing callers that don't pass
it get identical output for every existing field, plus a new
"session_twap" field that gracefully reads "No session ticks yet" instead
of computing a real TWAP.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

NIFTY_SPOT_TOKEN = 256265
IST = ZoneInfo("Asia/Kolkata")
CASH_SESSION_OPEN = time(9, 15)

from nifty.kite.spot import prior_session_candle

FIB_RATIOS = (
    ("23.6%", 0.236),
    ("38.2%", 0.382),
    ("50.0%", 0.500),
    ("61.8%", 0.618),
    ("78.6%", 0.786),
)

EMA_PERIODS = (9, 20, 21, 50, 100, 200)
SMA_PERIODS = (20, 50, 100, 200)


def ist_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _round(value: Optional[float], digits: int = 2) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), digits)


def ema_series(closes: List[float], period: int) -> Optional[float]:
    if len(closes) < period:
        return None
    multiplier = 2 / (period + 1)
    value = sum(closes[:period]) / period
    for price in closes[period:]:
        value = (price * multiplier) + (value * (1 - multiplier))
    return round(value, 2)


def sma_last(closes: List[float], period: int) -> Optional[float]:
    if len(closes) < period:
        return None
    return round(sum(closes[-period:]) / period, 2)


def atr_last(candles: List[Dict[str, float]], period: int = 14) -> Optional[float]:
    if len(candles) < period + 1:
        return None
    trs: List[float] = []
    for idx in range(1, len(candles)):
        high = candles[idx]["high"]
        low = candles[idx]["low"]
        prev_close = candles[idx - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    if len(trs) < period:
        return None
    return round(sum(trs[-period:]) / period, 2)


def format_oi_lakh(oi: Optional[int]) -> Optional[str]:
    if oi is None:
        return None
    return f"{oi / 100_000:.2f}L"


def spot_vs_level(spot: float, level: Optional[float]) -> Dict[str, Any]:
    if level is None or spot <= 0:
        return {"distance": None, "above": None, "label": None}
    dist = round(spot - level, 2)
    above = dist > 0
    if abs(dist) <= 25:
        tag = "At price"
    elif above:
        tag = "Above"
    else:
        tag = "Below"
    return {"distance": dist, "above": above, "label": tag}


def classical_pivots(high: float, low: float, close: float) -> Dict[str, Any]:
    pp = (high + low + close) / 3
    r1 = 2 * pp - low
    s1 = 2 * pp - high
    r2 = pp + (high - low)
    s2 = pp - (high - low)
    r3 = high + 2 * (pp - low)
    s3 = low - 2 * (high - pp)
    return {
        "PP": _round(pp),
        "R1": _round(r1),
        "R2": _round(r2),
        "R3": _round(r3),
        "S1": _round(s1),
        "S2": _round(s2),
        "S3": _round(s3),
        "labels": {
            "R3": "Extreme R",
            "R2": "Strong R",
            "R1": "First R",
            "PP": "Pivot",
            "S1": "Support",
            "S2": "Strong S",
            "S3": "Extreme S",
        },
    }


def camarilla_levels(high: float, low: float, close: float) -> Dict[str, Any]:
    rng = high - low
    mult = 1.1
    return {
        "CR4": _round(close + rng * mult / 2),
        "CR3": _round(close + rng * mult / 4),
        "CR2": _round(close + rng * mult / 6),
        "CR1": _round(close + rng * mult / 12),
        "CS1": _round(close - rng * mult / 12),
        "CS2": _round(close - rng * mult / 6),
        "CS3": _round(close - rng * mult / 4),
        "CS4": _round(close - rng * mult / 2),
        "labels": {
            "CR4": "Breakout buy",
            "CR3": "Resistance",
            "CR2": "Minor R",
            "CR1": "Minor R",
            "CS1": "Minor S",
            "CS2": "Minor S",
            "CS3": "Support",
            "CS4": "Sell below",
        },
    }


def fibonacci_levels(swing_high: float, swing_low: float) -> Dict[str, Any]:
    span = swing_high - swing_low
    levels: Dict[str, Any] = {"swing_high": _round(swing_high), "swing_low": _round(swing_low)}
    for label, ratio in FIB_RATIOS:
        levels[label] = _round(swing_high - span * ratio)
    levels["labels"] = {"61.8%": "Key retracement", "78.6%": "Deep support zone"}
    return levels


def fetch_daily_candles_kite() -> Tuple[List[Dict[str, float]], str]:
    try:
        from dotenv import load_dotenv
        from kiteconnect import KiteConnect
        from pathlib import Path

        from nifty.paths import ENV_FILE as _ENV; load_dotenv(_ENV)
        api_key = __import__("os").environ.get("KITE_API_KEY", "")
        access_token = __import__("os").environ.get("KITE_ACCESS_TOKEN", "")
        if not api_key or not access_token:
            return [], "kite_unavailable"
        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(access_token)
        to_date = datetime.now()
        from_date = to_date - timedelta(days=420)
        raw = kite.historical_data(
            NIFTY_SPOT_TOKEN,
            from_date,
            to_date,
            "day",
            continuous=False,
            oi=False,
        )
        candles = [
            {
                "date": str(row["date"])[:10],
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
            }
            for row in sorted(raw, key=lambda item: item["date"])
        ]
        return candles, "kite_daily"
    except Exception:
        return [], "kite_error"


def fetch_daily_candles_yfinance() -> Tuple[List[Dict[str, float]], str]:
    try:
        import yfinance as yf

        ticker = yf.Ticker("^NSEI")
        hist = ticker.history(period="2y", interval="1d")
        if hist.empty:
            return [], "yfinance_empty"
        candles: List[Dict[str, float]] = []
        for idx, row in hist.iterrows():
            candles.append(
                {
                    "date": idx.strftime("%Y-%m-%d"),
                    "open": float(row["Open"]),
                    "high": float(row["High"]),
                    "low": float(row["Low"]),
                    "close": float(row["Close"]),
                }
            )
        return candles, "yfinance_nseI"
    except Exception:
        return [], "yfinance_error"


def fetch_daily_candles(morning_nifty: Optional[Dict[str, Any]] = None) -> Tuple[List[Dict[str, float]], str]:
    candles, source = fetch_daily_candles_kite()
    if candles:
        return candles, source
    candles, source = fetch_daily_candles_yfinance()
    if candles:
        return candles, source
    if morning_nifty:
        try:
            return [
                {
                    "date": date.today().isoformat(),
                    "open": float(morning_nifty.get("open") or 0),
                    "high": float(morning_nifty.get("high") or 0),
                    "low": float(morning_nifty.get("low") or 0),
                    "close": float(morning_nifty.get("last") or morning_nifty.get("previous_close") or 0),
                }
            ], "morning_desk_snapshot_only"
        except (TypeError, ValueError):
            pass
    return [], "unavailable"


def period_extremes(candles: List[Dict[str, float]], spot: float) -> Dict[str, Any]:
    if not candles:
        return {}
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    w52_high = max(highs[-252:]) if len(highs) >= 252 else max(highs)
    w52_low = min(lows[-252:]) if len(lows) >= 252 else min(lows)
    m6_high = max(highs[-126:]) if len(highs) >= 126 else max(highs)
    m6_low = min(lows[-126:]) if len(lows) >= 126 else min(lows)
    month_candles = [c for c in candles if c["date"][:7] == candles[-1]["date"][:7]]
    week_candles = candles[-5:]
    month_high = max(c["high"] for c in month_candles) if month_candles else None
    month_low = min(c["low"] for c in month_candles) if month_candles else None
    week_high = max(c["high"] for c in week_candles) if week_candles else None
    week_low = min(c["low"] for c in week_candles) if week_candles else None
    span = w52_high - w52_low
    range_pct = round(((spot - w52_low) / span) * 100, 1) if span > 0 and spot > 0 else None
    return {
        "52w_high": _round(w52_high),
        "52w_low": _round(w52_low),
        "6m_high": _round(m6_high),
        "6m_low": _round(m6_low),
        "month_high": _round(month_high),
        "month_low": _round(month_low),
        "week_high": _round(week_high),
        "week_low": _round(week_low),
        "range_position_pct": range_pct,
    }


def moving_average_panel(closes: List[float], spot: float) -> Dict[str, Any]:
    panel: Dict[str, Any] = {}
    for period in EMA_PERIODS:
        value = ema_series(closes, period)
        key = f"ema_{period}"
        panel[key] = value
        vs = spot_vs_level(spot, value)
        panel[f"{key}_dist"] = vs["distance"]
        panel[f"{key}_tag"] = vs["label"]
    for period in SMA_PERIODS:
        value = sma_last(closes, period)
        key = f"sma_{period}"
        panel[key] = value
        vs = spot_vs_level(spot, value)
        panel[f"{key}_dist"] = vs["distance"]
        panel[f"{key}_tag"] = vs["label"]
    return panel


def _level_row(name: str, value: Optional[float], spot: float, note: str = "") -> Dict[str, Any]:
    vs = spot_vs_level(spot, value)
    return {
        "name": name,
        "value": value,
        "note": note,
        "distance": vs["distance"],
        "tag": vs["label"],
    }


def ist_session_open_dt(trade_date: Optional[date] = None) -> datetime:
    """NSE cash continuous open (09:15 IST)."""
    d = trade_date or date.today()
    return datetime.combine(d, CASH_SESSION_OPEN, tzinfo=IST)


def compute_session_twap(
    spot_history: Sequence[Tuple[float, float]],
    *,
    trade_date: Optional[date] = None,
    session_open: Optional[datetime] = None,
    as_of_epoch: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Session VWAP proxy for NIFTY spot index.

    NSE index cash has no meaningful volume on Kite — use time-weighted average
    of live ticks from 09:15 IST (TWAP). Futures-volume VWAP can be added later.
    """
    session_open = session_open or ist_session_open_dt(trade_date)
    start_epoch = session_open.timestamp()
    points = sorted(
        ((float(ts), float(px)) for ts, px in spot_history if float(px) > 0 and float(ts) >= start_epoch),
        key=lambda item: item[0],
    )
    if not points:
        return {
            "session_vwap": None,
            "method": "tick_twap",
            "tick_count": 0,
            "session_open": session_open.strftime("%Y-%m-%d %H:%M:%S"),
            "note": "No session ticks yet — starts at 09:15 IST",
        }

    if len(points) == 1:
        vwap = points[0][1]
        return {
            "session_vwap": _round(vwap),
            "method": "tick_twap",
            "tick_count": 1,
            "session_open": session_open.strftime("%Y-%m-%d %H:%M:%S"),
            "first_tick_at": datetime.fromtimestamp(points[0][0], tz=IST).strftime("%H:%M:%S"),
            "note": "Single tick — TWAP equals last price",
        }

    weighted = 0.0
    duration = 0.0
    for idx in range(len(points) - 1):
        t0, p0 = points[idx]
        t1, _ = points[idx + 1]
        dt = max(0.0, t1 - t0)
        if dt <= 0:
            continue
        weighted += p0 * dt
        duration += dt
    # Hold last price until as_of for open interval
    t_last, p_last = points[-1]
    now_epoch = as_of_epoch if as_of_epoch is not None else datetime.now(tz=IST).timestamp()
    tail = max(0.0, now_epoch - t_last)
    if tail > 0:
        weighted += p_last * tail
        duration += tail
    vwap = weighted / duration if duration > 0 else p_last
    return {
        "session_vwap": _round(vwap),
        "method": "tick_twap",
        "tick_count": len(points),
        "session_open": session_open.strftime("%Y-%m-%d %H:%M:%S"),
        "first_tick_at": datetime.fromtimestamp(points[0][0], tz=IST).strftime("%H:%M:%S"),
        "last_tick_at": datetime.fromtimestamp(t_last, tz=IST).strftime("%H:%M:%S"),
        "note": "Tick TWAP from 09:15 IST (index cash volume unavailable)",
    }


def bollinger_bands(
    closes: List[float],
    spot: float,
    *,
    period: int = 20,
    std_mult: float = 2.0,
) -> Dict[str, Any]:
    if len(closes) < period:
        return {}
    window = closes[-period:]
    middle = sum(window) / period
    variance = sum((x - middle) ** 2 for x in window) / period
    std = math.sqrt(variance) if variance > 0 else 0.0
    upper = middle + std_mult * std
    lower = middle - std_mult * std
    return {
        "period": period,
        "std_mult": std_mult,
        "middle": _round(middle),
        "upper": _round(upper),
        "lower": _round(lower),
        "bandwidth_pts": _round(upper - lower),
        "rows": [
            _level_row("BB Upper", _round(upper), spot, f"+{std_mult}σ"),
            _level_row("BB Middle", _round(middle), spot, f"SMA{period}"),
            _level_row("BB Lower", _round(lower), spot, f"-{std_mult}σ"),
        ],
    }


def keltner_channel(
    candles: List[Dict[str, float]],
    closes: List[float],
    spot: float,
    *,
    ema_period: int = 20,
    atr_period: int = 14,
    atr_mult: float = 2.0,
) -> Dict[str, Any]:
    ema_mid = ema_series(closes, ema_period)
    atr = atr_last(candles, atr_period)
    if ema_mid is None or atr is None:
        return {}
    upper = ema_mid + atr_mult * atr
    lower = ema_mid - atr_mult * atr
    return {
        "ema_period": ema_period,
        "atr_period": atr_period,
        "atr_mult": atr_mult,
        "middle": _round(ema_mid),
        "upper": _round(upper),
        "lower": _round(lower),
        "bandwidth_pts": _round(upper - lower),
        "rows": [
            _level_row("KC Upper", _round(upper), spot, f"EMA{ema_period}+{atr_mult}×ATR"),
            _level_row("KC Middle", _round(ema_mid), spot, f"EMA{ema_period}"),
            _level_row("KC Lower", _round(lower), spot, f"EMA{ema_period}-{atr_mult}×ATR"),
        ],
    }


def flatten_levels_for_alerts(key_levels: Dict[str, Any]) -> List[Tuple[str, float]]:
    """Return (label, price) pairs for key-area proximity checks."""
    out: List[Tuple[str, float]] = []
    for section_key in ("pivots", "camarilla", "fibonacci", "moving_averages", "period_extremes", "oi"):
        section = key_levels.get(section_key) or {}
        if section_key == "moving_averages":
            for key, value in section.items():
                if key.startswith(("ema_", "sma_")) and not key.endswith(("_dist", "_tag")) and value is not None:
                    out.append((key.replace("_", " ").upper(), float(value)))
            continue
        if section_key == "oi":
            for side in ("ceiling", "floor", "max_pain"):
                row = section.get(side) or {}
                strike = row.get("strike") if isinstance(row, dict) else row
                if strike is not None:
                    out.append((f"OI {side}", float(strike)))
            mp = section.get("max_pain")
            if isinstance(mp, (int, float)):
                out.append(("max pain", float(mp)))
            continue
        if section_key == "period_extremes":
            mapping = {
                "52w_high": "52W high",
                "52w_low": "52W low",
                "week_high": "week high",
                "week_low": "week low",
            }
            for key, label in mapping.items():
                val = section.get(key)
                if val is not None:
                    out.append((label, float(val)))
            continue
        skip = {"labels", "swing_high", "swing_low"}
        labels = section.get("labels") or {}
        for key, value in section.items():
            if key in skip or not isinstance(value, (int, float)):
                continue
            note = labels.get(key, key)
            out.append((f"{key} {note}".strip(), float(value)))
    return out


def build_key_levels(
    spot: Optional[float] = None,
    morning_nifty: Optional[Dict[str, Any]] = None,
    oi_map: Optional[Dict[str, Any]] = None,
    trade_date: Optional[date] = None,
    spot_history: Optional[Sequence[Tuple[float, float]]] = None,
) -> Dict[str, Any]:
    candles, source = fetch_daily_candles(morning_nifty=morning_nifty)
    errors: List[str] = []
    if len(candles) < 2:
        errors.append("insufficient_daily_candles")

    last = candles[-1] if candles else {}
    ref, ref_date = prior_session_candle(candles, trade_date)
    prev = ref if ref else (candles[-2] if len(candles) >= 2 else last)

    spot_price = float(spot or last.get("close") or morning_nifty.get("last") or 0) if (spot or last or morning_nifty) else 0.0
    if spot_price <= 0 and morning_nifty:
        spot_price = float(morning_nifty.get("last") or morning_nifty.get("previous_close") or 0)

    ph = float(ref.get("high") or 0)
    pl = float(ref.get("low") or 0)
    pc = float(ref.get("close") or 0)

    pivots = classical_pivots(ph, pl, pc) if ph and pl and pc else {}
    camarilla = camarilla_levels(ph, pl, pc) if ph and pl and pc else {}

    closes = [c["close"] for c in candles]
    extremes = period_extremes(candles, spot_price) if candles else {}
    swing_high = extremes.get("52w_high") or ph
    swing_low = extremes.get("52w_low") or pl
    fib = fibonacci_levels(float(swing_high or ph), float(swing_low or pl)) if swing_high and swing_low else {}

    ma_panel = moving_average_panel(closes, spot_price) if closes else {}
    atr14 = atr_last(candles, 14) if candles else None
    bollinger = bollinger_bands(closes, spot_price) if closes else {}
    keltner = keltner_channel(candles, closes, spot_price) if candles and closes else {}
    session_twap = compute_session_twap(spot_history or (), trade_date=trade_date)

    oi_section: Dict[str, Any] = {}
    if oi_map:
        ceiling = oi_map.get("ceiling") or {}
        floor = oi_map.get("floor") or {}
        ce_strike = ceiling.get("strike")
        pe_strike = floor.get("strike")
        ce_oi = ceiling.get("oi")
        pe_oi = floor.get("oi")
        oi_section = {
            "ceiling": {
                "strike": ce_strike,
                "oi": ce_oi,
                "oi_lakh": format_oi_lakh(ce_oi),
                "label": f"{ce_strike} CE {format_oi_lakh(ce_oi) or '-'}" if ce_strike else None,
            },
            "floor": {
                "strike": pe_strike,
                "oi": pe_oi,
                "oi_lakh": format_oi_lakh(pe_oi),
                "label": f"{pe_strike} PE {format_oi_lakh(pe_oi) or '-'}" if pe_strike else None,
            },
            "max_pain": oi_map.get("max_pain"),
            "max_pain_label": f"~{int(oi_map['max_pain'])}" if oi_map.get("max_pain") else None,
            "pcr_oi": oi_map.get("pcr_oi"),
            "data_source": oi_map.get("data_source"),
        }

    pivot_rows = [
        _level_row(k, pivots.get(k), spot_price, (pivots.get("labels") or {}).get(k, ""))
        for k in ("R3", "R2", "R1", "PP", "S1", "S2", "S3")
        if pivots.get(k) is not None
    ]
    cam_rows = [
        _level_row(k, camarilla.get(k), spot_price, (camarilla.get("labels") or {}).get(k, ""))
        for k in ("CR4", "CR3", "CR2", "CR1", "CS1", "CS2", "CS3", "CS4")
        if camarilla.get(k) is not None
    ]
    fib_rows = [
        _level_row(k, fib.get(k), spot_price, (fib.get("labels") or {}).get(k, ""))
        for k, _ in FIB_RATIOS
        if fib.get(k) is not None
    ]

    return {
        "trade_date": (trade_date or date.today()).isoformat(),
        "captured_at": ist_now(),
        "source": "nifty_key_levels.py",
        "data_source": source,
        "spot": _round(spot_price),
        "reference_day": ref_date or ref.get("date"),
        "reference_ohlc": {"open": ref.get("open"), "high": ph, "low": pl, "close": pc},
        "pivots": pivots,
        "pivot_rows": pivot_rows,
        "camarilla": camarilla,
        "camarilla_rows": cam_rows,
        "fibonacci": fib,
        "fibonacci_rows": fib_rows,
        "moving_averages": ma_panel,
        "period_extremes": extremes,
        "atr_14d": atr14,
        "bollinger": bollinger,
        "keltner": keltner,
        "session_twap": session_twap,
        "oi": oi_section,
        # bollinger/keltner/session_twap deliberately excluded from this dict:
        # user-approved choice to keep the 3 new indicators display-only,
        # never new "key area" zones for OI alert gating.
        "flat_levels": [{"label": label, "value": value} for label, value in flatten_levels_for_alerts(
            {
                "pivots": pivots,
                "camarilla": camarilla,
                "fibonacci": fib,
                "moving_averages": ma_panel,
                "period_extremes": extremes,
                "oi": oi_section,
            }
        )],
        "errors": errors,
    }


def _refresh_level_rows(rows: List[Dict[str, Any]], spot: float) -> List[Dict[str, Any]]:
    refreshed: List[Dict[str, Any]] = []
    for row in rows or []:
        value = row.get("value")
        if value is None:
            continue
        refreshed.append(_level_row(str(row.get("name") or ""), float(value), spot, str(row.get("note") or "")))
    return refreshed


def _refresh_moving_average_panel(panel: Dict[str, Any], spot: float) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in (panel or {}).items():
        if key.endswith("_dist") or key.endswith("_tag"):
            continue
        if not key.startswith(("ema_", "sma_")) or value is None:
            continue
        out[key] = value
        vs = spot_vs_level(spot, float(value))
        out[f"{key}_dist"] = vs["distance"]
        out[f"{key}_tag"] = vs["label"]
    return out


def _build_oi_section(oi_map: Dict[str, Any], spot: float) -> Dict[str, Any]:
    ceiling = oi_map.get("ceiling") or {}
    floor = oi_map.get("floor") or {}
    ce_strike = ceiling.get("strike")
    pe_strike = floor.get("strike")
    ce_oi = ceiling.get("oi")
    pe_oi = floor.get("oi")
    max_pain = oi_map.get("max_pain")
    return {
        "ceiling": {
            "strike": ce_strike,
            "oi": ce_oi,
            "oi_lakh": format_oi_lakh(ce_oi),
            "label": f"{ce_strike} CE {format_oi_lakh(ce_oi) or '-'}" if ce_strike else None,
        },
        "floor": {
            "strike": pe_strike,
            "oi": pe_oi,
            "oi_lakh": format_oi_lakh(pe_oi),
            "label": f"{pe_strike} PE {format_oi_lakh(pe_oi) or '-'}" if pe_strike else None,
        },
        "max_pain": max_pain,
        "max_pain_label": f"~{int(max_pain)}" if max_pain else None,
        "max_pain_vs_spot": _round(max_pain - spot) if max_pain and spot > 0 else None,
        "pcr_oi": oi_map.get("pcr_oi"),
        "underlying_price": oi_map.get("underlying_price"),
        "data_source": oi_map.get("source") or oi_map.get("data_source"),
        "captured_at": oi_map.get("captured_at"),
    }


def refresh_key_levels_live(
    base: Dict[str, Any],
    spot: float,
    *,
    day_open: Optional[float] = None,
    day_high: Optional[float] = None,
    day_low: Optional[float] = None,
    prev_close: Optional[float] = None,
    orb_high: Optional[float] = None,
    orb_low: Optional[float] = None,
    oi_map: Optional[Dict[str, Any]] = None,
    morning_nifty: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Recompute spot distances, session open context, and OI strikes from live data."""
    if not base:
        return {}
    if spot <= 0:
        return dict(base)

    from nifty.kite.spot import classify_open_gap

    kl = dict(base)
    kl["spot"] = _round(spot)
    kl["spot_captured_at"] = base.get("captured_at")
    kl["live_updated_at"] = ist_now()
    kl["live_context"] = True

    kl["pivot_rows"] = _refresh_level_rows(base.get("pivot_rows") or [], spot)
    kl["camarilla_rows"] = _refresh_level_rows(base.get("camarilla_rows") or [], spot)
    kl["fibonacci_rows"] = _refresh_level_rows(base.get("fibonacci_rows") or [], spot)
    kl["moving_averages"] = _refresh_moving_average_panel(base.get("moving_averages") or {}, spot)

    extremes = dict(base.get("period_extremes") or {})
    w52_high = extremes.get("52w_high")
    w52_low = extremes.get("52w_low")
    if w52_high is not None and w52_low is not None:
        span = float(w52_high) - float(w52_low)
        extremes["range_position_pct"] = round(((spot - float(w52_low)) / span) * 100, 1) if span > 0 else None
    kl["period_extremes"] = extremes

    morning_open = _as_float((morning_nifty or {}).get("open"))
    morning_gap = (morning_nifty or {}).get("open_gap") or {}
    effective_open = day_open if day_open and day_open > 0 else morning_open
    if not effective_open and morning_gap.get("reference_open"):
        effective_open = _as_float(morning_gap.get("reference_open"))
    effective_prev = prev_close if prev_close and prev_close > 0 else _as_float(
        (morning_nifty or {}).get("previous_close")
    )
    open_gap = classify_open_gap(spot, effective_open, effective_prev)
    kl["open_gap"] = open_gap
    kl["session_live"] = {
        "open": _round(effective_open),
        "prev_close": _round(effective_prev),
        "day_high": _round(day_high) if day_high and day_high > 0 else None,
        "day_low": _round(day_low) if day_low and day_low > 0 else None,
        "orb_high": _round(orb_high) if orb_high and orb_high > 0 else None,
        "orb_low": _round(orb_low) if orb_low and orb_low > 0 else None,
    }

    if oi_map and not oi_map.get("error"):
        kl["oi"] = _build_oi_section(oi_map, spot)

    session_section = {
        "open": kl["session_live"].get("open"),
        "prev close": effective_prev,
        "day high": day_high if day_high and day_high > 0 else None,
        "day low": day_low if day_low and day_low > 0 else None,
        "ORB high": orb_high if orb_high and orb_high > 0 else None,
        "ORB low": orb_low if orb_low and orb_low > 0 else None,
    }
    session_flat = [
        (label, float(value))
        for label, value in session_section.items()
        if value is not None and float(value) > 0
    ]

    kl["flat_levels"] = [{"label": label, "value": value} for label, value in session_flat] + [
        {"label": label, "value": value}
        for label, value in flatten_levels_for_alerts(
            {
                "pivots": kl.get("pivots") or {},
                "camarilla": kl.get("camarilla") or {},
                "fibonacci": kl.get("fibonacci") or {},
                "moving_averages": kl.get("moving_averages") or {},
                "period_extremes": kl.get("period_extremes") or {},
                "oi": kl.get("oi") or {},
            }
        )
    ]
    return kl


def _as_float(value: Any) -> Optional[float]:
    try:
        if value in (None, "", 0, "0"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _selftest() -> None:
    # bollinger_bands: needs >= period closes, else empty (never raises).
    assert bollinger_bands([100.0] * 10, 100.0, period=20) == {}
    closes = [23000.0 + (i % 5) * 10 for i in range(25)]
    bb = bollinger_bands(closes, 23020.0)
    assert bb["upper"] > bb["middle"] > bb["lower"]
    assert len(bb["rows"]) == 3

    # keltner_channel: needs enough candles for ATR too.
    candles = [{"high": 23000.0 + i, "low": 22980.0 + i, "close": 22990.0 + i} for i in range(20)]
    assert keltner_channel([], closes, 23020.0) == {}
    kc = keltner_channel(candles, closes, 23020.0)
    assert kc["upper"] > kc["middle"] > kc["lower"]

    # compute_session_twap: no ticks -> graceful "no ticks yet", never raises.
    empty_twap = compute_session_twap([], trade_date=date(2026, 7, 21))
    assert empty_twap["session_vwap"] is None
    assert empty_twap["tick_count"] == 0

    # Single tick -> TWAP equals that price.
    open_dt = ist_session_open_dt(date(2026, 7, 21))
    single = compute_session_twap([(open_dt.timestamp() + 60, 23000.0)], trade_date=date(2026, 7, 21))
    assert single["session_vwap"] == 23000.0
    assert single["tick_count"] == 1

    # Multiple ticks -> time-weighted average, held to as_of_epoch for the open interval.
    t0 = open_dt.timestamp()
    history = [(t0 + 60, 23000.0), (t0 + 120, 23100.0)]
    twap = compute_session_twap(history, trade_date=date(2026, 7, 21), as_of_epoch=t0 + 180)
    assert twap["tick_count"] == 2
    assert twap["session_vwap"] is not None

    # Ticks before session open are excluded.
    pre_open = compute_session_twap([(t0 - 3600, 22000.0)], trade_date=date(2026, 7, 21))
    assert pre_open["tick_count"] == 0

    # build_key_levels: new fields present, and NOT leaked into flat_levels
    # (the user-approved display-only choice) — flat_levels only contains
    # labels from pivots/camarilla/fibonacci/moving_averages/period_extremes/oi.
    kl = build_key_levels(spot=23020.0, morning_nifty={"last": 23020.0}, trade_date=date(2026, 7, 21))
    assert "bollinger" in kl
    assert "keltner" in kl
    assert "session_twap" in kl
    flat_labels = " ".join(row["label"] for row in kl["flat_levels"])
    assert "BB" not in flat_labels
    assert "KC" not in flat_labels

    # Existing callers that don't pass spot_history still get a graceful
    # session_twap (no ticks yet), not a crash — backward compatible.
    assert kl["session_twap"]["tick_count"] == 0

    print("[kite.key_levels] selftest OK: bollinger/keltner/session-twap, kept out of alert-gating flat_levels")


if __name__ == "__main__":
    _selftest()
