#!/usr/bin/env python3
"""Load persisted morning desk artifacts for live session use."""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path
from typing import Any, Dict, Optional

from nifty.core.expiry import (
    build_expiry_session_rules,
    is_expiry_session,
)
from nifty.analytics.futures import load_eod_futures_context, previous_trading_day
from nifty.core.journal import JOURNAL_DIR, ist_now, today_str
from nifty.kite.key_levels import refresh_key_levels_live

OI_REFRESH_SECONDS = 900
JOURNAL_RELOAD_SECONDS = 120

_INSTRUMENT_OI_SYMBOL = {
    "NIFTY": "NIFTY",
    "BANKNIFTY": "BANKNIFTY",
    "NO_TRADE": "NIFTY",
}


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except json.JSONDecodeError:
        return {}


def load_morning_bundle(day: Optional[date] = None) -> Dict[str, Any]:
    """Return all morning phase artifacts for a trade date."""
    label = today_str(day)
    journal = JOURNAL_DIR
    day_date = day or date.today()
    global_desk = _read_json(journal / f"global_desk_{label}.json")
    morning_desk = _read_json(journal / f"morning_desk_{label}.json")
    instrument = _read_json(journal / f"instrument_selection_{label}.json")
    oi_map = _read_json(journal / f"oi_map_{label}.json")
    key_levels = _read_json(journal / f"key_levels_{label}.json")
    desk_brief = _read_json(journal / f"desk_brief_{label}.json")
    is_expiry = is_expiry_session(trade_date=day_date) or bool(desk_brief.get("is_expiry_day"))
    combined_bias = (
        desk_brief.get("combined_bias")
        or desk_brief.get("morning_bias")
        or instrument.get("combined_bias")
        or morning_desk.get("india_bias", {}).get("label")
        or global_desk.get("global_bias", {}).get("label")
        or "UNKNOWN"
    )
    max_pain = desk_brief.get("max_pain") or oi_map.get("max_pain")
    prev_close = _as_float((morning_desk.get("nifty") or {}).get("previous_close"))
    expiry_rules = build_expiry_session_rules(
        is_expiry=is_expiry,
        combined_bias=str(combined_bias),
        max_pain=_as_float(max_pain),
        spot=0.0,
        prev_close=prev_close,
    )
    if desk_brief.get("max_pain") and prev_close:
        expiry_rules["max_pain_context"] = {
            **(expiry_rules.get("max_pain_context") or {}),
            "max_pain": desk_brief.get("max_pain"),
            "note": desk_brief.get("narrative_one_line"),
        }
    futures_eod = load_eod_futures_context(previous_trading_day(day_date))
    return {
        "trade_date": label,
        "global_desk": global_desk,
        "morning_desk": morning_desk,
        "instrument_selection": instrument,
        "oi_map": oi_map,
        "key_levels": key_levels,
        "desk_brief": desk_brief,
        "is_expiry_day": is_expiry,
        "expiry_rules": expiry_rules,
        "expiry_scenarios": desk_brief.get("scenarios") or [],
        "combined_bias": combined_bias,
        "chosen_instrument": desk_brief.get("primary_instrument") or instrument.get("instrument") or "NIFTY",
        "secondary_instrument": desk_brief.get("secondary_instrument"),
        "conviction": instrument.get("conviction"),
        "gift_overnight_bias": (
            (morning_desk.get("gift_nifty") or {}).get("overnight_bias")
            or (global_desk.get("gift_nifty") or {}).get("overnight_bias")
        ),
        "cash_open_gap": (morning_desk.get("nifty") or {}).get("open_gap"),
        "oi_ceiling": (oi_map.get("ceiling") or {}).get("strike"),
        "oi_floor": (oi_map.get("floor") or {}).get("strike"),
        "max_pain": desk_brief.get("max_pain") or oi_map.get("max_pain"),
        "futures_eod": futures_eod,
        "loaded": bool(global_desk or morning_desk or instrument or oi_map or key_levels or desk_brief),
    }


def _as_float(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _refresh_live_oi_map(symbol: str) -> Dict[str, Any]:
    from nifty.sources.oi_map import build_oi_map

    try:
        payload = build_oi_map(symbol)
    except Exception as exc:
        return {"error": f"nse_live_oi_unavailable: {exc}"}
    if payload.get("error"):
        return payload
    payload["captured_at"] = ist_now()
    return payload


def enrich_morning_context_live(
    bundle: Dict[str, Any],
    spot: float,
    *,
    day_open: float = 0.0,
    day_high: float = 0.0,
    day_low: float = 0.0,
    prev_close: float = 0.0,
    orb_high: float = 0.0,
    orb_low: float = 0.0,
    option_expiry: str = "",
    refresh_oi: bool = False,
) -> Dict[str, Any]:
    """Apply live spot, open, and optional fresh OI chain to morning artifacts."""
    ctx = dict(bundle)
    oi_map = dict(ctx.get("oi_map") or {})
    if refresh_oi:
        symbol = _INSTRUMENT_OI_SYMBOL.get(str(ctx.get("chosen_instrument") or "NIFTY").upper(), "NIFTY")
        fresh = _refresh_live_oi_map(symbol)
        if not fresh.get("error"):
            oi_map = fresh
            ctx["oi_map"] = fresh
            ctx["oi_ceiling"] = (fresh.get("ceiling") or {}).get("strike")
            ctx["oi_floor"] = (fresh.get("floor") or {}).get("strike")
            ctx["max_pain"] = fresh.get("max_pain")
            ctx["oi_refreshed_at"] = fresh.get("captured_at")

    morning_nifty = (ctx.get("morning_desk") or {}).get("nifty") or {}
    ctx["key_levels"] = refresh_key_levels_live(
        ctx.get("key_levels") or {},
        spot,
        day_open=day_open or None,
        day_high=day_high or None,
        day_low=day_low or None,
        prev_close=prev_close or None,
        orb_high=orb_high or None,
        orb_low=orb_low or None,
        oi_map=oi_map,
        morning_nifty=morning_nifty,
    )
    live_gap = (ctx.get("key_levels") or {}).get("open_gap") or {}
    if live_gap.get("gap_type"):
        ctx["live_open_gap"] = live_gap
    ctx["live_spot"] = spot
    is_expiry = is_expiry_session(option_expiry=option_expiry or None) or bool(ctx.get("is_expiry_day"))
    ctx["is_expiry_day"] = is_expiry
    mp = _as_float((ctx.get("desk_brief") or {}).get("max_pain")) or _as_float((ctx.get("oi_map") or {}).get("max_pain"))
    pc = prev_close or _as_float((ctx.get("morning_desk") or {}).get("nifty", {}).get("previous_close"))
    ctx["expiry_rules"] = build_expiry_session_rules(
        is_expiry=is_expiry,
        combined_bias=str(ctx.get("combined_bias") or "NEUTRAL"),
        max_pain=mp,
        spot=spot,
        prev_close=pc,
    )
    ctx["live_enriched_at"] = ist_now()
    return ctx


class LiveMorningContext:
    """Cached morning bundle with periodic journal reload and live OI refresh."""

    def __init__(self) -> None:
        self.bundle: Dict[str, Any] = {}
        self.loaded = False
        self.journal_loaded_at = 0.0
        self.oi_refreshed_at = 0.0

    def refresh(
        self,
        spot: float,
        *,
        day_open: float = 0.0,
        day_high: float = 0.0,
        day_low: float = 0.0,
        prev_close: float = 0.0,
        orb_high: float = 0.0,
        orb_low: float = 0.0,
        option_expiry: str = "",
        force_journal: bool = False,
    ) -> Dict[str, Any]:
        now = time.time()
        if force_journal or not self.loaded or (now - self.journal_loaded_at) >= JOURNAL_RELOAD_SECONDS:
            self.bundle = load_morning_bundle()
            self.loaded = True
            self.journal_loaded_at = now

        refresh_oi = spot > 0 and (now - self.oi_refreshed_at) >= OI_REFRESH_SECONDS
        self.bundle = enrich_morning_context_live(
            self.bundle,
            spot,
            day_open=day_open,
            day_high=day_high,
            day_low=day_low,
            prev_close=prev_close,
            orb_high=orb_high,
            orb_low=orb_low,
            option_expiry=option_expiry,
            refresh_oi=refresh_oi,
        )
        if refresh_oi:
            self.oi_refreshed_at = now
        return self.bundle
