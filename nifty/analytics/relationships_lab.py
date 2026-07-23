#!/usr/bin/env python3
"""
Market Relationships Lab — read-only research & visualization payload.

Ported faithfully from quant-desk-engine's nifty_relationships_lab.py
(mentor-authored). No logic changed. This module NEVER affects signal
generation, decision execution, or trade management.

The foundational journal-parsing helpers this file depends on
(_load_jsonl, _parse_ts, _fmt_time, _recorded_ts, _liquidity_grab_info,
load_paper_trades_from_journal, list_available_journal_days,
journal_day_inventory, _as_float, _as_int) were extracted earlier into
nifty.analytics.journal_reader — imported here rather than duplicated.
This file holds everything else: the actual graph/timeline/replay builders
("the new graphs").

It aggregates live state + journals into relationship-centric views for
intuition building.

Not yet wired into the live pipeline.
Self-check: python -m nifty.analytics.relationships_lab
"""

from __future__ import annotations

import math
import statistics
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from nifty.paths import JOURNAL_DIR
from nifty.analytics.journal_reader import (
    _as_float,
    _as_int,
    _fmt_time,
    _liquidity_grab_info,
    _load_jsonl,
    _parse_ts,
    _recorded_ts,
    journal_day_inventory,
    list_available_journal_days,
    load_paper_trades_from_journal,
)


def load_journal_day_context(journal_dir: Path, day: date) -> Dict[str, Any]:
    """Reconstruct research context for a historical session from journals only."""
    d = day.isoformat()
    morning_path = journal_dir / f"morning_desk_{d}.json"
    key_levels_path = journal_dir / f"key_levels_{d}.json"
    morning_context: Dict[str, Any] = {}
    key_levels: Dict[str, Any] = {}
    if morning_path.exists():
        try:
            import json
            morning_context = json.loads(morning_path.read_text(encoding="utf-8"))
        except Exception:
            morning_context = {}
    if key_levels_path.exists():
        try:
            import json
            key_levels = json.loads(key_levels_path.read_text(encoding="utf-8"))
        except Exception:
            key_levels = {}

    opt_rows = [r for r in _load_jsonl(journal_dir / f"nifty_options_analytics_{d}.jsonl") if not r.get("error")]
    dec_rows = [r for r in _load_jsonl(journal_dir / f"decision_engine_{d}.jsonl") if r.get("event") == "DECISION_STATE"]
    trades = load_paper_trades_from_journal(journal_dir, day)

    levels: Dict[str, Any] = {}
    for trade in trades:
        ctx = trade.get("desk_context") or {}
        if ctx:
            levels = {
                "open": ctx.get("spot"),
                "day_high": ctx.get("day_high"),
                "day_low": ctx.get("day_low"),
                "prev_close": ctx.get("prev_close"),
                "orb_high": ctx.get("orb_high"),
                "orb_low": ctx.get("orb_low"),
            }
            if not morning_context:
                morning_context = {
                    "combined_bias": ctx.get("combined_bias"),
                    "gap_pts": None,
                }
            break

    last_opt = opt_rows[-1] if opt_rows else {}
    last_dec = dec_rows[-1] if dec_rows else {}
    spot = _as_float(last_dec.get("spot")) or _as_float(last_opt.get("spot"))
    if not spot and trades:
        spot = _as_float(trades[-1].get("spot"))

    return {
        "date": d,
        "spot": spot,
        "levels": levels,
        "key_levels": key_levels or morning_context.get("key_levels") or {},
        "morning_context": morning_context,
        "options_analytics": last_opt,
        "decision_engine": last_dec if last_dec else {},
        "trades": trades,
        "inventory": journal_day_inventory(journal_dir, day),
    }


def _pearson(xs: List[float], ys: List[float]) -> Optional[float]:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    mx = statistics.mean(xs)
    my = statistics.mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den_x = math.sqrt(sum((x - mx) ** 2 for x in xs))
    den_y = math.sqrt(sum((y - my) ** 2 for y in ys))
    if den_x == 0 or den_y == 0:
        return None
    return round(num / (den_x * den_y), 3)


def _forward_fill_series(
    master: List[Tuple[float, float]],
    journal: List[Dict[str, Any]],
    value_fn: Callable[[Dict[str, Any]], Optional[float]],
) -> List[Optional[float]]:
    if not master:
        return []
    indexed = []
    for row in journal:
        ts = _parse_ts(row)
        if ts is None:
            continue
        val = value_fn(row)
        if val is not None:
            indexed.append((ts, val))
    indexed.sort(key=lambda item: item[0])
    out: List[Optional[float]] = []
    idx = 0
    current: Optional[float] = None
    for ts, _ in master:
        while idx < len(indexed) and indexed[idx][0] <= ts:
            current = indexed[idx][1]
            idx += 1
        out.append(current)
    return out


def _spot_master(spot_history: Iterable[Tuple[float, float]], max_points: int = 360) -> List[Tuple[float, float]]:
    points = list(spot_history)[-max_points:]
    cleaned: List[Tuple[float, float]] = []
    for ts, price in points:
        p = round(float(price), 2)
        if 15_000 <= p <= 35_000:
            cleaned.append((float(ts), p))
    if len(cleaned) >= 3:
        median = statistics.median([p for _, p in cleaned])
        cleaned = [(ts, p) for ts, p in cleaned if abs(p - median) <= 400]
    return cleaned[-max_points:]


def build_market_story_timeline(
    *,
    morning_context: Optional[Dict[str, Any]],
    decision_engine: Optional[Dict[str, Any]],
    options_analytics: Optional[Dict[str, Any]],
    market_profile: Optional[Dict[str, Any]],
    liquidity_engine: Optional[Dict[str, Any]],
    behavior_events: List[Dict[str, Any]],
    spot: float,
    levels: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Narrative timeline of session evolution — research only."""
    story: List[Dict[str, Any]] = []
    mc = morning_context or {}
    gap = _as_float(mc.get("gap_pts"))
    bias = str(mc.get("combined_bias") or "NEUTRAL")
    if gap != 0 or bias != "NEUTRAL":
        gap_label = "Gap Up" if gap > 5 else ("Gap Down" if gap < -5 else "Flat Open")
        story.append({"time": "09:15", "label": gap_label, "detail": f"Bias {bias} · gap {gap:+.0f} pts"})

    chain = str((decision_engine or {}).get("direction_manager", {}).get("chain_read") or "")
    regime = str(((decision_engine or {}).get("market_regime") or {}).get("regime") or "")
    oa = options_analytics or {}
    gex_regime = str(oa.get("gex_regime") or "")
    if gex_regime:
        story.append(
            {
                "time": "Session",
                "label": f"Dealer Gamma {gex_regime.replace('_', ' ').title()}",
                "detail": f"Net GEX {_as_float(oa.get('net_gex_cr')):.1f} Cr",
            }
        )

    dominant = str(oa.get("dominant_writer_side") or "")
    if dominant:
        story.append({"time": "Session", "label": f"{dominant} Writing", "detail": "Options surface participant read"})

    if regime:
        story.append(
            {
                "time": "Now",
                "label": regime.replace("_", " ").title(),
                "detail": str(((decision_engine or {}).get("market_regime") or {}).get("note") or ""),
            }
        )

    premium = (decision_engine or {}).get("premium_evaluation") or {}
    if premium.get("verdict"):
        story.append(
            {
                "time": "Now",
                "label": f"Premium {str(premium.get('verdict')).replace('_', ' ').title()}",
                "detail": str(premium.get("note") or ""),
            }
        )

    em = (decision_engine or {}).get("expected_move_engine") or {}
    if em.get("buy_premium_discouraged"):
        story.append({"time": "Now", "label": "Decision Edge Weakening", "detail": "Premium rich vs expected move"})

    mp = market_profile or {}
    if mp.get("balance_regime"):
        story.append(
            {
                "time": "Profile",
                "label": str(mp.get("balance_regime")).replace("_", " "),
                "detail": f"POC {mp.get('poc')} · VAH {mp.get('vah')} · VAL {mp.get('val')}",
            }
        )

    grab = _liquidity_grab_info(liquidity_engine)
    if grab.get("active"):
        story.append(
            {
                "time": "Liquidity",
                "label": f"Liquidity Grab — {grab.get('direction', '')}",
                "detail": str(grab.get("source") or grab.get("label") or ""),
            }
        )

    for event in behavior_events[-8:]:
        kind = str(event.get("kind") or "")
        if kind in {"SIGNAL_ENTRY", "SIGNAL_CLOSED", "WRITER_ADD"}:
            story.append(
                {
                    "time": _fmt_time(_as_float(event.get("ts"))),
                    "label": kind.replace("_", " ").title(),
                    "detail": str(event.get("contract") or event.get("reason") or ""),
                }
            )

    lv = levels or {}
    if spot and lv.get("day_high") and abs(spot - _as_float(lv.get("day_high"))) < 15:
        story.append({"time": "Now", "label": "At Day High", "detail": f"Spot {spot:.0f}"})
    if spot and lv.get("day_low") and abs(spot - _as_float(lv.get("day_low"))) < 15:
        story.append({"time": "Now", "label": "At Day Low", "detail": f"Spot {spot:.0f}"})

    story.append({"time": "15:30", "label": "Closing Auction", "detail": "Session wind-down — review Relationships Lab EOD"})
    return story


def _combined_spot_master(
    spot_history: Iterable[Tuple[float, float]],
    journal_dir: Path,
    day: date,
    max_points: int = 480,
) -> List[Tuple[float, float]]:
    """Merge live spot ticks with journaled decision spots for full-session replay."""
    points: List[Tuple[float, float]] = []
    for ts, price in spot_history:
        p = round(float(price), 2)
        if 15_000 <= p <= 35_000:
            points.append((float(ts), p))
    for row in _load_jsonl(journal_dir / f"decision_engine_{day.isoformat()}.jsonl"):
        if row.get("event") != "DECISION_STATE":
            continue
        ts = _recorded_ts(row)
        spot = _as_float(row.get("spot"))
        if ts and spot > 0:
            points.append((ts, spot))
    for row in _load_jsonl(journal_dir / f"nifty_options_analytics_{day.isoformat()}.jsonl"):
        if row.get("error"):
            continue
        ts = _recorded_ts(row)
        spot = _as_float(row.get("spot"))
        if ts and spot > 0:
            points.append((ts, spot))
    points.sort(key=lambda item: item[0])
    deduped: List[Tuple[float, float]] = []
    last_bucket = ""
    for ts, price in points:
        bucket = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        if bucket == last_bucket and deduped:
            deduped[-1] = (ts, price)
        else:
            deduped.append((ts, price))
            last_bucket = bucket
    return deduped[-max_points:]


def build_session_replay(journal_dir: Path, day: Optional[date] = None) -> Dict[str, Any]:
    """Minute-by-minute session replay from journals — research only."""
    d = day or date.today()
    dec_rows = _load_jsonl(journal_dir / f"decision_engine_{d.isoformat()}.jsonl", limit=500)
    opt_rows = _load_jsonl(journal_dir / f"nifty_options_analytics_{d.isoformat()}.jsonl", limit=500)
    opt_by_ts = sorted(
        [(ts, row) for row in opt_rows if (ts := _recorded_ts(row)) is not None],
        key=lambda item: item[0],
    )
    trades = load_paper_trades_from_journal(journal_dir, d)

    frames: List[Dict[str, Any]] = []
    for row in dec_rows:
        if row.get("event") != "DECISION_STATE":
            continue
        ts = _recorded_ts(row)
        if ts is None:
            continue
        regime = row.get("market_regime") or {}
        direction = row.get("direction_manager") or {}
        premium = row.get("premium_evaluation") or {}
        machine = row.get("institutional_state_machine") or {}
        decision = row.get("decision") or {}
        opt_snap = {}
        for opt_ts, opt_row in opt_by_ts:
            if opt_ts <= ts:
                opt_snap = opt_row
            else:
                break
        frames.append(
            {
                "index": len(frames),
                "ts": ts,
                "time": str(row.get("recorded_at") or "")[-8:] or _fmt_time(ts),
                "spot": _as_float(row.get("spot")),
                "regime": regime.get("regime"),
                "regime_confidence": regime.get("confidence"),
                "regime_reason": regime.get("reason"),
                "direction": direction.get("mode"),
                "premium": premium.get("verdict"),
                "conviction": (row.get("conviction_memory") or {}).get("score"),
                "machine_state": machine.get("state"),
                "should_trade": decision.get("should_trade"),
                "blocks": decision.get("blocks") or [],
                "action": decision.get("action"),
                "net_gex_cr": opt_snap.get("net_gex_cr"),
                "expected_move_pts": opt_snap.get("expected_move_pts"),
                "atm_iv": opt_snap.get("atm_iv"),
            }
        )

    if not frames:
        for row in opt_rows:
            if row.get("error"):
                continue
            ts = _recorded_ts(row)
            spot = _as_float(row.get("spot"))
            if ts is None or spot <= 0:
                continue
            frames.append(
                {
                    "index": len(frames),
                    "ts": ts,
                    "time": str(row.get("recorded_at") or "")[-8:] or _fmt_time(ts),
                    "spot": spot,
                    "regime": row.get("gex_regime"),
                    "regime_confidence": None,
                    "regime_reason": row.get("dealer_positioning"),
                    "direction": None,
                    "premium": row.get("premium_vs_vix"),
                    "conviction": None,
                    "machine_state": None,
                    "should_trade": None,
                    "blocks": [],
                    "action": None,
                    "net_gex_cr": row.get("net_gex_cr"),
                    "expected_move_pts": row.get("expected_move_pts"),
                    "atm_iv": row.get("atm_iv"),
                    "source": "options_analytics",
                }
            )

    events: List[Dict[str, Any]] = []
    for trade in trades:
        events.append(
            {
                "kind": "TRADE_ENTRY",
                "time": trade.get("entry_time") or trade.get("generated_at"),
                "ts": trade.get("entry_ts"),
                "decision": trade.get("decision"),
                "strike": trade.get("strike"),
                "entry_price": trade.get("entry_price"),
                "confluence": trade.get("confluence_score"),
            }
        )
        if str(trade.get("status")) == "CLOSED":
            events.append(
                {
                    "kind": "TRADE_EXIT",
                    "time": trade.get("exit_time") or trade.get("recorded_at"),
                    "ts": trade.get("exit_ts"),
                    "decision": trade.get("decision"),
                    "strike": trade.get("strike"),
                    "exit_price": trade.get("exit_price"),
                    "pnl_pct": trade.get("pnl_pct"),
                    "exit_reason": trade.get("exit_reason"),
                }
            )
    events.sort(key=lambda item: _as_float(item.get("ts"), 0))

    return {
        "frame_count": len(frames),
        "frames": frames,
        "events": events,
        "spot_series": [{"time": f["time"], "spot": f["spot"], "regime": f["regime"]} for f in frames],
        "stats": {
            "decision_frames": sum(1 for f in frames if f.get("source") != "options_analytics"),
            "options_frames": sum(1 for f in frames if f.get("source") == "options_analytics"),
            "options_snapshots": len(opt_rows),
            "paper_trades": len(trades),
            "trade_events": len(events),
        },
        "replay_source": "decision_engine" if dec_rows else ("options_analytics" if frames else "none"),
    }


def build_relationship_charts(
    *,
    spot_history: Iterable[Tuple[float, float]],
    journal_dir: Path,
    day: Optional[date] = None,
    options_analytics: Optional[Dict[str, Any]],
    market_profile: Optional[Dict[str, Any]],
    decision_engine: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Aligned time series for relationship plots (spot vs derived variables)."""
    d = day or date.today()
    master = _combined_spot_master(spot_history, journal_dir, d)
    if len(master) < 5:
        master = _spot_master(spot_history)
    labels = [_fmt_time(ts) for ts, _ in master]
    spot_vals = [p for _, p in master]

    opt_rows = _load_jsonl(journal_dir / f"nifty_options_analytics_{d.isoformat()}.jsonl")
    dec_rows = _load_jsonl(journal_dir / f"decision_engine_{d.isoformat()}.jsonl")

    net_oi_proxy = []
    for row in opt_rows:
        ts = _parse_ts(row)
        if ts is None:
            continue
        ce = _as_float(row.get("total_ce_oi"))
        pe = _as_float(row.get("total_pe_oi"))
        if ce or pe:
            net_oi_proxy.append((ts, ce - pe))
    net_oi_series = _forward_fill_series(master, [{"ts": ts, "v": v} for ts, v in net_oi_proxy], lambda r: r.get("v"))

    series = {
        "labels": labels,
        "spot": spot_vals,
        "net_oi_proxy": net_oi_series,
        "net_gex_cr": _forward_fill_series(
            master,
            opt_rows,
            lambda r: _as_float(r.get("net_gex_cr")) if r.get("net_gex_cr") is not None else None,
        ),
        "expected_move_pts": _forward_fill_series(
            master,
            opt_rows,
            lambda r: _as_float(r.get("expected_move_pts")) if r.get("expected_move_pts") else None,
        ),
        "atm_iv": _forward_fill_series(
            master,
            opt_rows,
            lambda r: _as_float(r.get("atm_iv")) if r.get("atm_iv") else None,
        ),
        "regime_confidence": _forward_fill_series(
            master,
            dec_rows,
            lambda r: _as_float((r.get("market_regime") or {}).get("confidence")),
        ),
        "conviction": _forward_fill_series(
            master,
            dec_rows,
            lambda r: _as_float((r.get("conviction_memory") or {}).get("score")),
        ),
        "vwap_proxy": _forward_fill_series(
            master,
            [{"ts": ts, "poc": (market_profile or {}).get("poc")} for ts, _ in master[-1:]],
            lambda r: _as_float(r.get("poc")) if r.get("poc") else None,
        ),
    }

    # Current snapshot fallbacks for flat lines when journals are sparse
    oa = options_analytics or {}
    if oa and spot_vals:
        if not any(v is not None for v in series["net_gex_cr"]):
            series["net_gex_cr"] = [_as_float(oa.get("net_gex_cr"))] * len(spot_vals)
        if not any(v is not None for v in series["expected_move_pts"]):
            series["expected_move_pts"] = [_as_float(oa.get("expected_move_pts"))] * len(spot_vals)
        if not any(v is not None for v in series["atm_iv"]):
            series["atm_iv"] = [_as_float(oa.get("atm_iv"))] * len(spot_vals)

    mp = market_profile or {}
    if mp.get("poc") and spot_vals:
        series["market_profile_poc"] = [_as_float(mp.get("poc"))] * len(spot_vals)
        series["market_profile_vah"] = [_as_float(mp.get("vah"))] * len(spot_vals)
        series["market_profile_val"] = [_as_float(mp.get("val"))] * len(spot_vals)
    else:
        series["market_profile_poc"] = series.get("vwap_proxy", [])

    de = decision_engine or {}
    if de and spot_vals and not any(v is not None for v in series["regime_confidence"]):
        conf = _as_float((de.get("market_regime") or {}).get("confidence"))
        series["regime_confidence"] = [conf] * len(spot_vals)

    presets = [
        {"id": "spot_vs_oi", "label": "Spot vs OI (net CE−PE proxy)", "y_key": "net_oi_proxy", "y_label": "Net OI"},
        {"id": "spot_vs_gamma", "label": "Spot vs Dealer Gamma", "y_key": "net_gex_cr", "y_label": "Net GEX (Cr)"},
        {"id": "spot_vs_em", "label": "Spot vs Expected Move", "y_key": "expected_move_pts", "y_label": "EM (pts)"},
        {"id": "spot_vs_premium", "label": "Spot vs ATM IV (premium proxy)", "y_key": "atm_iv", "y_label": "ATM IV %"},
        {"id": "spot_vs_regime", "label": "Spot vs Regime Confidence", "y_key": "regime_confidence", "y_label": "Confidence"},
        {"id": "spot_vs_mp", "label": "Spot vs Market Profile POC", "y_key": "market_profile_poc", "y_label": "POC"},
    ]

    return {"series": series, "presets": presets, "point_count": len(labels)}


def build_multi_expiry_surface_placeholder(*, expiry: str, spot: float) -> Dict[str, Any]:
    """UI scaffold for future multi-expiry module — placeholder data only."""
    base = round(spot / 50) * 50 if spot > 0 else 24000
    expiries = [
        {"id": "W0", "label": "Current Weekly", "expiry": expiry or "W0", "status": "live"},
        {"id": "W1", "label": "Next Weekly", "expiry": "pending", "status": "placeholder"},
        {"id": "M0", "label": "Monthly", "expiry": "pending", "status": "placeholder"},
        {"id": "M1", "label": "Far Monthly", "expiry": "pending", "status": "placeholder"},
    ]
    strikes = [base + step for step in range(-200, 250, 50)]
    metrics = ["oi", "gamma", "delta", "vanna", "charm"]
    heatmap: List[Dict[str, Any]] = []
    for ex in expiries:
        for strike in strikes:
            dist = abs(strike - spot) if spot else 100
            heatmap.append(
                {
                    "expiry_id": ex["id"],
                    "strike": strike,
                    "oi": max(0, 800_000 - dist * 1200),
                    "gamma": round(max(-1, 1 - dist / 200), 3),
                    "delta": round((spot - strike) / 1000 if spot else 0, 3),
                    "vanna": round(0.5 - dist / 500, 3),
                    "charm": round(-0.2 + dist / 800, 3),
                    "rollover_pct": 0 if ex["id"] == "W0" else round(min(40, dist / 30), 1),
                }
            )
    return {
        "status": "placeholder",
        "note": "Multi-expiry chain not subscribed yet. Structure ready for W0/W1/M0/M1.",
        "expiries": expiries,
        "metrics": metrics,
        "heatmap": heatmap,
        "migration": [
            {"from": "W0", "to": "W1", "oi_shift_pct": 12.5, "note": "Simulated roll pressure"},
            {"from": "W0", "to": "M0", "oi_shift_pct": 4.2, "note": "Simulated monthly hedge"},
        ],
    }


def build_decision_timeline(journal_dir: Path, day: Optional[date] = None) -> List[Dict[str, Any]]:
    d = day or date.today()
    rows = _load_jsonl(journal_dir / f"decision_engine_{d.isoformat()}.jsonl", limit=500)
    timeline: List[Dict[str, Any]] = []
    for row in rows:
        if row.get("event") != "DECISION_STATE":
            continue
        ts = _parse_ts(row)
        regime = (row.get("market_regime") or {})
        direction = row.get("direction_manager") or {}
        premium = row.get("premium_evaluation") or {}
        machine = row.get("institutional_state_machine") or {}
        decision = row.get("decision") or {}
        timeline.append(
            {
                "time": _fmt_time(ts) if ts else str(row.get("recorded_at", ""))[-8:],
                "regime": regime.get("regime"),
                "regime_confidence": regime.get("confidence"),
                "direction": direction.get("mode"),
                "premium": premium.get("verdict"),
                "conviction": (row.get("conviction_memory") or {}).get("score"),
                "machine_state": machine.get("state"),
                "should_trade": decision.get("should_trade"),
                "blocks": decision.get("blocks") or [],
                "action": decision.get("action"),
            }
        )
    return timeline[-120:]


def _nearest_decision_frame(
    decision_timeline: List[Dict[str, Any]],
    entry_time: Optional[str],
) -> Optional[Dict[str, Any]]:
    if not decision_timeline or not entry_time:
        return decision_timeline[-1] if decision_timeline else None
    entry_hms = str(entry_time)[-8:]
    for row in reversed(decision_timeline):
        if str(row.get("time") or "") <= entry_hms:
            return row
    return decision_timeline[0]


def build_trade_replay(
    *,
    signals: List[Dict[str, Any]],
    spot_history: Iterable[Tuple[float, float]],
    decision_timeline: List[Dict[str, Any]],
    journal_dir: Path,
    day: Optional[date] = None,
) -> List[Dict[str, Any]]:
    d = day or date.today()
    master = _combined_spot_master(spot_history, journal_dir, d, max_points=720)
    journal_trades = load_paper_trades_from_journal(journal_dir, d)
    merged: Dict[str, Dict[str, Any]] = {}
    for sig in list(signals) + journal_trades:
        key = str(sig.get("signal_key") or sig.get("id") or "")
        if not key:
            continue
        if key not in merged:
            merged[key] = dict(sig)
        elif str(sig.get("status")) == "CLOSED" or sig.get("exit_price"):
            merged[key] = {**merged[key], **sig}

    replays: List[Dict[str, Any]] = []
    for sig in merged.values():
        if not sig.get("decision"):
            continue
        entry_ts = _as_float(sig.get("entry_ts")) or _recorded_ts(sig) or 0.0
        exit_ts = _as_float(sig.get("exit_ts"), 0.0)
        entry_spot = _as_float(sig.get("spot_at_entry") or sig.get("spot"))
        path = []
        for ts, price in master:
            if entry_ts and ts < entry_ts - 120:
                continue
            if exit_ts and ts > exit_ts + 180:
                break
            path.append({"time": _fmt_time(ts), "spot": price, "ts": ts})
        entry_time = sig.get("entry_time") or sig.get("generated_at") or sig.get("recorded_at")
        oi_conv = sig.get("oi_conviction")
        if isinstance(oi_conv, dict):
            oi_label = oi_conv.get("level") or oi_conv.get("read")
        else:
            oi_label = oi_conv
        replays.append(
            {
                "id": sig.get("id") or sig.get("signal_key"),
                "status": sig.get("status"),
                "decision": sig.get("decision"),
                "strike": sig.get("strike"),
                "entry": sig.get("entry_price"),
                "exit": sig.get("exit_price"),
                "pnl_pct": sig.get("pnl_pct"),
                "entry_time": entry_time,
                "exit_time": sig.get("exit_time"),
                "oi_conviction": oi_label,
                "confluence": sig.get("confluence_score") or sig.get("total_score"),
                "regime_at_entry": (sig.get("desk_context") or {}).get("regime"),
                "spot_path": path[-120:],
                "entry_spot": entry_spot,
                "decision_snapshot": _nearest_decision_frame(decision_timeline, str(entry_time or "")),
            }
        )
    replays.sort(key=lambda row: str(row.get("entry_time") or ""))
    return replays[:25]


def build_confidence_dashboard(
    *,
    decision_engine: Optional[Dict[str, Any]],
    market_profile: Optional[Dict[str, Any]],
    volatility_engine: Optional[Dict[str, Any]],
    liquidity_engine: Optional[Dict[str, Any]],
    options_analytics: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    de = decision_engine or {}
    gauges = [
        {
            "engine": "Market Regime",
            "score": _as_int((de.get("market_regime") or {}).get("confidence"), 50),
            "label": str((de.get("market_regime") or {}).get("regime") or "—"),
        },
        {
            "engine": "Direction",
            "score": _as_int((de.get("conviction_memory") or {}).get("score"), 50),
            "label": str((de.get("direction_manager") or {}).get("mode") or "—"),
        },
        {
            "engine": "Premium",
            "score": 75 if (de.get("premium_evaluation") or {}).get("verdict") == "CHEAP" else (
                35 if (de.get("premium_evaluation") or {}).get("verdict") in {"EXPENSIVE", "AVOID"} else 55
            ),
            "label": str((de.get("premium_evaluation") or {}).get("verdict") or "—"),
        },
        {
            "engine": "Liquidity",
            "score": _as_int((liquidity_engine or {}).get("confidence"), 50),
            "label": str((liquidity_engine or {}).get("read") or "—"),
        },
        {
            "engine": "Dealer Flow",
            "score": 65 if str((options_analytics or {}).get("gex_regime")) == "POSITIVE_GAMMA" else 45,
            "label": str((options_analytics or {}).get("gex_regime") or "—"),
        },
        {
            "engine": "Market Profile",
            "score": _as_int((market_profile or {}).get("confidence"), 50),
            "label": str((market_profile or {}).get("balance_regime") or "—"),
        },
        {
            "engine": "Volatility",
            "score": _as_int((volatility_engine or {}).get("confidence"), 50),
            "label": str((volatility_engine or {}).get("regime") or "—"),
        },
        {
            "engine": "Overall Decision",
            "score": 80 if (de.get("decision") or {}).get("should_trade") else 30,
            "label": "GO" if (de.get("decision") or {}).get("should_trade") else "WAIT",
        },
    ]
    for g in gauges:
        g["score"] = max(0, min(100, _as_int(g["score"], 50)))
    return gauges


def build_cause_effect_graph(
    *,
    decision_engine: Optional[Dict[str, Any]],
    options_analytics: Optional[Dict[str, Any]],
    spot_v5_delta: float,
) -> Dict[str, Any]:
    de = decision_engine or {}
    oa = options_analytics or {}
    spot_dir = "Spot Rising" if spot_v5_delta > 3 else ("Spot Falling" if spot_v5_delta < -3 else "Spot Flat")
    nodes = [
        {"id": "spot", "label": spot_dir},
        {"id": "writers", "label": f"{oa.get('dominant_writer_side', 'Mixed')} Writing"},
        {"id": "gamma", "label": f"Dealer Gamma {oa.get('gex_regime', '—')}"},
        {"id": "em", "label": f"Expected Move { _as_float((de.get('expected_move_engine') or {}).get('expected_move_pts')):.0f} pts"},
        {"id": "premium", "label": f"Premium {(de.get('premium_evaluation') or {}).get('verdict', '—')}"},
        {"id": "edge", "label": "Buying Edge Reducing" if (de.get("expected_move_engine") or {}).get("buy_premium_discouraged") else "Buying Edge OK"},
        {"id": "decision", "label": f"Decision {(de.get('decision') or {}).get('action', 'WAIT')}"},
    ]
    edges = [
        ("spot", "writers"),
        ("writers", "gamma"),
        ("gamma", "em"),
        ("em", "premium"),
        ("premium", "edge"),
        ("edge", "decision"),
    ]
    return {"nodes": nodes, "edges": [{"from": a, "to": b} for a, b in edges]}


def build_correlation_explorer(relationship_charts: Dict[str, Any]) -> Dict[str, Any]:
    series = relationship_charts.get("series") or {}
    spot = [v for v in (series.get("spot") or []) if v is not None]
    pairs = []
    for key, label in [
        ("net_oi_proxy", "Net OI"),
        ("net_gex_cr", "Net GEX"),
        ("expected_move_pts", "Expected Move"),
        ("atm_iv", "ATM IV"),
        ("regime_confidence", "Regime Confidence"),
        ("conviction", "Conviction"),
    ]:
        ys_raw = series.get(key) or []
        xs, ys = [], []
        for x, y in zip(series.get("spot") or [], ys_raw):
            if x is not None and y is not None:
                xs.append(float(x))
                ys.append(float(y))
        corr = _pearson(xs, ys)
        pairs.append({"x": "Spot", "y": label, "key": key, "correlation": corr, "n": len(xs)})
    return {"pairs": pairs, "default": "spot_vs_gamma"}


def build_heatmaps(
    *,
    paired_rows: List[Dict[str, Any]],
    options_analytics: Optional[Dict[str, Any]],
    decision_engine: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    oi_rows = []
    gamma_rows = []
    vol_rows = []
    for pair in paired_rows:
        strike = _as_int(pair.get("strike"))
        ce = pair.get("ce") or {}
        pe = pair.get("pe") or {}
        oi_rows.append({"strike": strike, "ce_oi": _as_int(ce.get("oi")), "pe_oi": _as_int(pe.get("oi"))})
        vol_rows.append(
            {
                "strike": strike,
                "ce_vol": _as_int(ce.get("volume")),
                "pe_vol": _as_int(pe.get("volume")),
            }
        )
    chain = (options_analytics or {}).get("chain") or []
    for row in chain:
        gamma_rows.append({"strike": _as_int(row.get("strike")), "gex": _as_float(row.get("gex"))})

    de = decision_engine or {}
    confidence_heat = [
        {"engine": g["engine"], "score": g["score"]}
        for g in build_confidence_dashboard(
            decision_engine=de,
            market_profile={},
            volatility_engine={},
            liquidity_engine={},
            options_analytics=options_analytics,
        )
    ]
    return {
        "strike_oi": oi_rows,
        "strike_gamma": gamma_rows,
        "strike_volume": vol_rows,
        "confidence": confidence_heat,
    }


def build_learning_journal(
    *,
    spot: float,
    levels: Optional[Dict[str, Any]],
    decision_engine: Optional[Dict[str, Any]],
    options_analytics: Optional[Dict[str, Any]],
    market_profile: Optional[Dict[str, Any]],
    liquidity_engine: Optional[Dict[str, Any]],
    signals: List[Dict[str, Any]],
    decision_timeline: List[Dict[str, Any]],
) -> List[str]:
    """Auto-generated session observations — research notes, not trade instructions."""
    obs: List[str] = []
    lv = levels or {}
    oa = options_analytics or {}
    mp = market_profile or {}
    de = decision_engine or {}

    for label, price in [("resistance", lv.get("day_high")), ("support", lv.get("day_low"))]:
        p = _as_float(price)
        if spot and p and abs(spot - p) < 20:
            obs.append(f"Spot respected session {label} near {p:.0f}.")

    flip = oa.get("gamma_flip_strike") or (oa.get("gamma_structure") or {}).get("gamma_flip_strike")
    if flip:
        obs.append(f"Gamma flip/wall area near {flip} — dealer hedging zone.")

    prem = (de.get("premium_evaluation") or {}).get("verdict")
    if prem in {"EXPENSIVE", "AVOID"}:
        obs.append("Premium stayed expensive vs expected move — long option edge reduced.")

    if mp.get("poc") and spot:
        if abs(spot - _as_float(mp.get("poc"))) < 25:
            obs.append(f"Spot accepting near POC {mp.get('poc')} — balance auction.")
        elif spot > _as_float(mp.get("vah")):
            obs.append(f"Spot extended above VAH {mp.get('vah')} — watch rejection.")

    grab = _liquidity_grab_info(liquidity_engine)
    if grab.get("active"):
        obs.append(f"Liquidity sweep detected at {grab.get('source', 'key level')}.")

    closed = [s for s in signals if str(s.get("status")) == "CLOSED"]
    if len(closed) >= 2:
        wins = sum(1 for s in closed if _as_float(s.get("pnl_pct")) > 0)
        obs.append(f"Paper book: {wins}/{len(closed)} winners today.")

    if len(decision_timeline) >= 5:
        early = decision_timeline[: len(decision_timeline) // 2]
        late = decision_timeline[len(decision_timeline) // 2:]
        early_conf = statistics.mean([_as_float(r.get("regime_confidence"), 50) for r in early])
        late_conf = statistics.mean([_as_float(r.get("regime_confidence"), 50) for r in late])
        if late_conf < early_conf - 8:
            obs.append("Decision confidence deteriorated in the second half of the session.")

    if not obs:
        obs.append("Session still developing — observations will populate as journals fill.")
    return obs


def build_relationships_lab_payload(
    *,
    spot: float,
    spot_history: Iterable[Tuple[float, float]],
    expiry: str,
    levels: Optional[Dict[str, Any]],
    morning_context: Optional[Dict[str, Any]],
    decision_engine: Optional[Dict[str, Any]],
    options_analytics: Optional[Dict[str, Any]],
    market_profile: Optional[Dict[str, Any]],
    volatility_engine: Optional[Dict[str, Any]],
    liquidity_engine: Optional[Dict[str, Any]],
    paired_rows: List[Dict[str, Any]],
    signals: List[Dict[str, Any]],
    behavior_events: List[Dict[str, Any]],
    spot_v5_delta: float = 0.0,
    journal_dir: Path = JOURNAL_DIR,
    day: Optional[date] = None,
) -> Dict[str, Any]:
    """Assemble full Relationships Lab payload — strictly read-only."""
    d = day or date.today()
    relationship_charts = build_relationship_charts(
        spot_history=spot_history,
        journal_dir=journal_dir,
        day=d,
        options_analytics=options_analytics,
        market_profile=market_profile,
        decision_engine=decision_engine,
    )
    decision_timeline = build_decision_timeline(journal_dir, d)
    session_replay = build_session_replay(journal_dir, d)
    journal_trades = load_paper_trades_from_journal(journal_dir, d)
    return {
        "mode": "research_only",
        "advisory_only": True,
        "note": "Market Relationships Lab does not affect signals, decisions, or execution.",
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "journal_stats": session_replay.get("stats") or {},
        "session_replay": session_replay,
        "market_story_timeline": build_market_story_timeline(
            morning_context=morning_context,
            decision_engine=decision_engine,
            options_analytics=options_analytics,
            market_profile=market_profile,
            liquidity_engine=liquidity_engine,
            behavior_events=behavior_events,
            spot=spot,
            levels=levels,
        ),
        "relationship_charts": relationship_charts,
        "multi_expiry_surface": build_multi_expiry_surface_placeholder(expiry=expiry, spot=spot),
        "decision_timeline": decision_timeline,
        "trade_replay": build_trade_replay(
            signals=signals + journal_trades,
            spot_history=spot_history,
            decision_timeline=decision_timeline,
            journal_dir=journal_dir,
            day=d,
        ),
        "confidence_dashboard": build_confidence_dashboard(
            decision_engine=decision_engine,
            market_profile=market_profile,
            volatility_engine=volatility_engine,
            liquidity_engine=liquidity_engine,
            options_analytics=options_analytics,
        ),
        "cause_effect": build_cause_effect_graph(
            decision_engine=decision_engine,
            options_analytics=options_analytics,
            spot_v5_delta=spot_v5_delta,
        ),
        "correlation_explorer": build_correlation_explorer(relationship_charts),
        "heatmaps": build_heatmaps(
            paired_rows=paired_rows,
            options_analytics=options_analytics,
            decision_engine=decision_engine,
        ),
        "learning_journal": build_learning_journal(
            spot=spot,
            levels=levels,
            decision_engine=decision_engine,
            options_analytics=options_analytics,
            market_profile=market_profile,
            liquidity_engine=liquidity_engine,
            signals=signals,
            decision_timeline=decision_timeline,
        ),
    }


def _selftest() -> None:
    import tempfile

    assert _pearson([1, 2, 3], [2, 4, 6]) == 1.0
    assert _pearson([1, 1, 1], [2, 4, 6]) is None  # zero variance guard
    assert _pearson([1, 2], [1, 2]) is None  # needs >= 3 points

    base = datetime(2026, 6, 19, 10, 0).timestamp()
    master = [(base, 23000.0), (base + 60, 23010.0), (base + 120, 23020.0)]
    journal = [{"ts": base, "v": 5.0}, {"ts": base + 90, "v": 8.0}]
    filled = _forward_fill_series(master, journal, lambda r: r.get("v"))
    assert filled == [5.0, 5.0, 8.0]

    spot_hist = [(1000.0, 23000.0), (1060.0, 99999.0), (1120.0, 23020.0)]  # one bad tick
    cleaned = _spot_master(spot_hist)
    assert len(cleaned) == 2  # the 99999 outlier is dropped by the range filter

    tmp = Path(tempfile.mkdtemp(prefix="relationships-lab-selftest-"))
    day = date(2026, 6, 19)

    story = build_market_story_timeline(
        morning_context={"gap_pts": 40, "combined_bias": "BULLISH"},
        decision_engine={}, options_analytics={"gex_regime": "POSITIVE_GAMMA"},
        market_profile={}, liquidity_engine=None, behavior_events=[],
        spot=23100.0, levels={"day_high": 23110.0},
    )
    assert any("Gap Up" in s["label"] for s in story)
    assert any("At Day High" in s["label"] for s in story)
    assert story[-1]["label"] == "Closing Auction"

    replay = build_session_replay(tmp, day)
    assert replay["replay_source"] == "none"
    assert replay["frame_count"] == 0

    charts = build_relationship_charts(
        spot_history=spot_hist, journal_dir=tmp, day=day,
        options_analytics={"net_gex_cr": 12.5}, market_profile=None, decision_engine=None,
    )
    assert "series" in charts and len(charts["presets"]) == 6

    surface = build_multi_expiry_surface_placeholder(expiry="2026-06-25", spot=23000.0)
    assert surface["status"] == "placeholder" and len(surface["heatmap"]) == 36  # 4 expiries x 9 strikes

    timeline = build_decision_timeline(tmp, day)
    assert timeline == []

    trades = build_trade_replay(signals=[], spot_history=spot_hist, decision_timeline=[], journal_dir=tmp, day=day)
    assert trades == []

    gauges = build_confidence_dashboard(
        decision_engine={"decision": {"should_trade": True}}, market_profile=None,
        volatility_engine=None, liquidity_engine=None, options_analytics={"gex_regime": "POSITIVE_GAMMA"},
    )
    assert len(gauges) == 8
    assert next(g["score"] for g in gauges if g["engine"] == "Overall Decision") == 80

    cause_effect = build_cause_effect_graph(decision_engine={}, options_analytics={}, spot_v5_delta=10.0)
    assert len(cause_effect["nodes"]) == 7 and len(cause_effect["edges"]) == 6

    correlation = build_correlation_explorer(charts)
    assert len(correlation["pairs"]) == 6

    heatmaps = build_heatmaps(
        paired_rows=[{"strike": 23000, "ce": {"oi": 100}, "pe": {"oi": 200}}],
        options_analytics={}, decision_engine={},
    )
    assert heatmaps["strike_oi"][0]["ce_oi"] == 100

    journal_notes = build_learning_journal(
        spot=23100.0, levels={"day_high": 23110.0}, decision_engine={}, options_analytics={},
        market_profile={}, liquidity_engine=None, signals=[], decision_timeline=[],
    )
    assert len(journal_notes) >= 1

    payload = build_relationships_lab_payload(
        spot=23100.0, spot_history=spot_hist, expiry="2026-06-25",
        levels={"day_high": 23110.0}, morning_context={}, decision_engine={},
        options_analytics={}, market_profile={}, volatility_engine={}, liquidity_engine={},
        paired_rows=[], signals=[], behavior_events=[], journal_dir=tmp, day=day,
    )
    assert payload["mode"] == "research_only" and payload["advisory_only"] is True
    assert "relationship_charts" in payload and "trade_replay" in payload

    ctx = load_journal_day_context(tmp, day)
    assert ctx["date"] == "2026-06-19" and ctx["trades"] == []

    print("[analytics.relationships_lab] selftest OK: 18 functions - stats, charts, replay, payload assembly")


if __name__ == "__main__":
    _selftest()
