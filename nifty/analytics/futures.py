#!/usr/bin/env python3
"""NIFTY index futures layer — EOD participant positioning + live fut OI vs options."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from nifty.core.journal import JOURNAL_DIR

from nifty.paths import PROJECT_ROOT as BASE_DIR

FUTURES_OI_ADD_PCT = 0.35
FUTURES_OI_UNWIND_PCT = -0.35
FUTURES_PRICE_FLAT_PTS = 6.0
FII_NET_SHORT_EXTREME = 150_000
FII_NET_LONG_EXTREME = 50_000
ENABLE_FUTURES_ALIGNMENT_BLOCK = True


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def previous_trading_day(start: Optional[date] = None) -> date:
    day = start or date.today()
    day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except json.JSONDecodeError:
        return {}


def _participant_index_fields(row: Dict[str, Any]) -> Dict[str, Any]:
    long_qty = _as_int(row.get("Future Index Long"))
    short_qty = _as_int(row.get("Future Index Short"))
    net = long_qty - short_qty
    if net <= -FII_NET_SHORT_EXTREME:
        bias = "SHORT"
    elif net >= FII_NET_LONG_EXTREME:
        bias = "LONG"
    else:
        bias = "NEUTRAL"
    return {
        "participant": str(row.get("participant") or ""),
        "index_long": long_qty,
        "index_short": short_qty,
        "index_net": net,
        "bias": bias,
    }


def summarize_participant_futures(participant_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in participant_rows:
        label = str(row.get("participant") or "").strip()
        if not label or label.upper() == "TOTAL":
            continue
        out.append(_participant_index_fields(row))
    return out


def load_eod_futures_context(trade_date: Optional[date] = None) -> Dict[str, Any]:
    """Load prior NSE participant index-futures positioning from desk EOD filing."""
    label = (trade_date or previous_trading_day()).isoformat()
    filing = _read_json(JOURNAL_DIR / f"nse_eod_filing_{label}.json")
    if not filing and trade_date is None:
        label = date.today().isoformat()
        filing = _read_json(JOURNAL_DIR / f"nse_eod_filing_{label}.json")
    participants = summarize_participant_futures(filing.get("participant_oi_summary") or [])
    fii = next((row for row in participants if row.get("participant") == "FII"), {})
    dii = next((row for row in participants if row.get("participant") == "DII"), {})
    pro = next((row for row in participants if row.get("participant") == "Pro"), {})
    macro_bias = "NEUTRAL"
    if fii.get("bias") == "SHORT":
        macro_bias = "BEARISH"
    elif fii.get("bias") == "LONG":
        macro_bias = "BULLISH"
    return {
        "source_date": filing.get("trade_date") or label,
        "loaded": bool(participants),
        "participants": participants,
        "fii_index_net": fii.get("index_net"),
        "dii_index_net": dii.get("index_net"),
        "pro_index_net": pro.get("index_net"),
        "macro_bias": macro_bias,
        "read": _macro_read(fii, dii),
    }


def _macro_read(fii: Dict[str, Any], dii: Dict[str, Any]) -> str:
    fii_net = _as_int(fii.get("index_net"))
    dii_net = _as_int(dii.get("index_net"))
    if fii_net <= -FII_NET_SHORT_EXTREME:
        return f"FII heavily short index futures ({fii_net:+,}) — macro bearish overlay"
    if fii_net >= FII_NET_LONG_EXTREME:
        return f"FII net long index futures ({fii_net:+,}) — macro bullish overlay"
    if dii_net >= FII_NET_LONG_EXTREME:
        return f"DII net long index futures ({dii_net:+,}) — domestic support overlay"
    return "Index futures positioning mixed — use live fut OI + option strikes"


def classify_futures_oi_behavior(
    oi_delta: int,
    oi_pct: float,
    price_delta: float,
    spot_delta: float = 0.0,
) -> Tuple[str, str, str]:
    """Return behavior code, human read, status tone."""
    adding = oi_pct >= FUTURES_OI_ADD_PCT or oi_delta > 0
    unwinding = oi_pct <= FUTURES_OI_UNWIND_PCT or oi_delta < 0
    price_up = price_delta > FUTURES_PRICE_FLAT_PTS
    price_down = price_delta < -FUTURES_PRICE_FLAT_PTS
    spot_up = spot_delta > FUTURES_PRICE_FLAT_PTS
    spot_down = spot_delta < -FUTURES_PRICE_FLAT_PTS

    if adding and (price_up or spot_up):
        return "LONG_BUILD", "Fut OI adding + price/spot up — directional long build", "ok"
    if adding and (price_down or spot_down):
        return "SHORT_BUILD", "Fut OI adding + price/spot down — short build / hedge sell", "bad"
    if unwinding and (price_up or spot_up):
        return "SHORT_COVER", "Fut OI unwinding + price/spot up — short covering", "ok"
    if unwinding and (price_down or spot_down):
        return "LONG_UNWIND", "Fut OI unwinding + price/spot down — long exit", "bad"
    if adding:
        return "OI_ADD_MIXED", "Fut OI adding — direction unclear vs spot", "warn"
    if unwinding:
        return "OI_UNWIND_MIXED", "Fut OI unwinding — direction unclear", "warn"
    return "FLAT", "Fut OI quiet", "info"


def snapshot_future_contract(
    item: Any,
    *,
    spot: float,
    spot_v5: Dict[str, Any],
    series_role: str = "front",
) -> Dict[str, Any]:
    base = item.snapshot()
    v5 = base.get("velocity_5m") or {}
    oi_delta = _as_int(v5.get("delta"))
    oi_pct = _as_float(v5.get("pct"))
    price_delta = _as_float(v5.get("price_delta"))
    spot_delta = _as_float(spot_v5.get("delta"))
    behavior, read, status = classify_futures_oi_behavior(oi_delta, oi_pct, price_delta, spot_delta)
    basis = round(base.get("last_price", 0) - spot, 2) if spot > 0 else None
    basis_pct = round((basis / spot) * 100, 3) if basis is not None and spot else None
    return {
        **base,
        "series_role": series_role,
        "basis_pts": basis,
        "basis_pct": basis_pct,
        "behavior": behavior,
        "behavior_read": read,
        "behavior_status": status,
        "spot_5m_delta": spot_delta,
    }


def build_futures_layer(
    futures: List[Any],
    *,
    spot: float,
    spot_v5: Dict[str, Any],
    eod_context: Dict[str, Any],
) -> Dict[str, Any]:
    rows = []
    for item in futures:
        role = getattr(item, "series_role", None) or "front"
        rows.append(snapshot_future_contract(item, spot=spot, spot_v5=spot_v5, series_role=role))
    rows.sort(key=lambda row: 0 if row.get("series_role") == "front" else 1)
    front = next((row for row in rows if row.get("series_role") == "front"), rows[0] if rows else {})
    return {
        "contracts": rows,
        "front": front,
        "front_behavior": front.get("behavior") if front else "NOT_TRACKED",
        "front_basis_pts": front.get("basis_pts") if front else None,
        "eod_participant": eod_context,
        "macro_bias": eod_context.get("macro_bias"),
        "layer_read": _layer_read(front, eod_context),
    }


def _layer_read(front: Dict[str, Any], eod: Dict[str, Any]) -> str:
    live = str(front.get("behavior") or "FLAT")
    macro = str(eod.get("macro_bias") or "NEUTRAL")
    if live in {"LONG_BUILD", "SHORT_COVER"} and macro == "BULLISH":
        return "Futures + EOD aligned BULLISH"
    if live in {"SHORT_BUILD", "LONG_UNWIND"} and macro == "BEARISH":
        return "Futures + EOD aligned BEARISH"
    if live in {"LONG_BUILD", "SHORT_COVER"} and macro == "BEARISH":
        return "Live fut building long vs EOD bearish FII — watch for squeeze / conflict"
    if live in {"SHORT_BUILD", "LONG_UNWIND"} and macro == "BULLISH":
        return "Live fut building short vs EOD bullish — conflict with options long thesis"
    return eod.get("read") or "Track front-month fut OI vs option writer alerts"


def evaluate_fut_opt_alignment(
    decision: str,
    *,
    eod_context: Dict[str, Any],
    front_behavior: str,
) -> Dict[str, Any]:
    """Check option signal vs EOD + live index futures positioning."""
    fii_net = _as_int(eod_context.get("fii_index_net"))
    macro = str(eod_context.get("macro_bias") or "NEUTRAL")
    live = str(front_behavior or "FLAT")
    alignment = "NEUTRAL"
    blocker: Optional[str] = None
    detail = ""

    if decision == "BUY_CE":
        if macro == "BEARISH" and fii_net <= -FII_NET_SHORT_EXTREME:
            if live in {"SHORT_BUILD", "LONG_UNWIND", "OI_ADD_MIXED"}:
                alignment = "CONFLICT"
                blocker = "FUTURES_MACRO_CONFLICT"
                detail = "BUY_CE vs FII short index fut + live bearish fut OI"
            else:
                alignment = "CAUTION"
                detail = "BUY_CE vs EOD FII short futures — options local only"
        elif live in {"LONG_BUILD", "SHORT_COVER"}:
            alignment = "ALIGNED"
            detail = "BUY_CE aligned with live fut long build / cover"
        elif macro == "BULLISH":
            alignment = "ALIGNED"
            detail = "BUY_CE aligned with EOD bullish fut positioning"
    elif decision == "BUY_PE":
        if macro == "BULLISH" and fii_net >= FII_NET_LONG_EXTREME:
            if live in {"LONG_BUILD", "SHORT_COVER"}:
                alignment = "CONFLICT"
                blocker = "FUTURES_MACRO_CONFLICT"
                detail = "BUY_PE vs FII long index fut + live bullish fut OI"
            else:
                alignment = "CAUTION"
                detail = "BUY_PE vs EOD FII long futures"
        elif live in {"SHORT_BUILD", "LONG_UNWIND"}:
            alignment = "ALIGNED"
            detail = "BUY_PE aligned with live fut short build / long unwind"
        elif macro == "BEARISH":
            alignment = "ALIGNED"
            detail = "BUY_PE aligned with EOD bearish FII fut book"

    return {
        "alignment": alignment,
        "blocker": blocker if ENABLE_FUTURES_ALIGNMENT_BLOCK else None,
        "detail": detail,
        "fii_index_net": fii_net,
        "macro_bias": macro,
        "live_fut_behavior": live,
    }

