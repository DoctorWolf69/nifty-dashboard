#!/usr/bin/env python3
"""
Market Intelligence Lab — institutional research workspace (read-only).

Ported faithfully from quant-desk-engine's nifty_market_intelligence_lab.py
(mentor-authored). No formula/logic changed. Only adaptation: the original
imports everything from a single sibling module (nifty_relationships_lab);
here those names are split across nifty.paths (JOURNAL_DIR),
nifty.analytics.journal_reader (the low-level journal helpers), and
nifty.analytics.relationships_lab (the build_* graph functions +
load_journal_day_context) — matching how this porting effort already split
that source file. Some imported names (e.g. _recorded_ts) are not called
directly in this file's own body, same as in the mentor's original; kept for
faithfulness rather than pruned.

Twenty workspaces for relationship discovery and visual understanding.
Never affects signal generation, decision execution, or trade management —
this file only assembles read-only research payloads on top of
relationships_lab.py's base payload.

Not yet wired into the live pipeline.
Self-check: python -m nifty.analytics.market_intelligence_lab
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from nifty.paths import JOURNAL_DIR
from nifty.analytics.journal_reader import (
    _as_float,
    _as_int,
    _fmt_time,
    _liquidity_grab_info,
    _load_jsonl,
    _recorded_ts,
    journal_day_inventory,
    list_available_journal_days,
    load_paper_trades_from_journal,
)
from nifty.analytics.relationships_lab import (
    build_cause_effect_graph,
    build_confidence_dashboard,
    build_correlation_explorer,
    build_decision_timeline,
    build_heatmaps,
    build_learning_journal,
    build_market_story_timeline,
    build_multi_expiry_surface_placeholder,
    build_relationship_charts,
    build_relationships_lab_payload,
    build_session_replay,
    build_trade_replay,
    load_journal_day_context,
)

WORKSPACE_CATALOG = [
    {"id": "market_structure", "num": 1, "title": "Market Structure"},
    {"id": "participant", "num": 2, "title": "Participant Intelligence"},
    {"id": "dealer", "num": 3, "title": "Dealer Intelligence"},
    {"id": "multi_expiry", "num": 4, "title": "Multi-Expiry Surface"},
    {"id": "volatility", "num": 5, "title": "Volatility Intelligence"},
    {"id": "liquidity", "num": 6, "title": "Liquidity Intelligence"},
    {"id": "futures", "num": 7, "title": "Futures Intelligence"},
    {"id": "time", "num": 8, "title": "Time Intelligence"},
    {"id": "strategy", "num": 9, "title": "Strategy Intelligence"},
    {"id": "decision", "num": 10, "title": "Decision Intelligence"},
    {"id": "trade", "num": 11, "title": "Trade Intelligence"},
    {"id": "learning", "num": 12, "title": "Learning Journal"},
    {"id": "correlation", "num": 13, "title": "Correlation Explorer"},
    {"id": "cause_effect", "num": 14, "title": "Cause & Effect"},
    {"id": "multi_timeframe", "num": 15, "title": "Multi-Timeframe"},
    {"id": "market_replay", "num": 16, "title": "Market Replay"},
    {"id": "opportunity", "num": 17, "title": "Opportunity Explorer"},
    {"id": "counterfactual", "num": 18, "title": "Counterfactual Explorer"},
    {"id": "discovery", "num": 19, "title": "Relationship Discovery"},
    {"id": "executive", "num": 20, "title": "Institutional Dashboard"},
]


def _bucket_ohlc(
    spot_history: Iterable[Tuple[float, float]],
    bucket_sec: int,
) -> List[Dict[str, Any]]:
    buckets: Dict[int, List[float]] = {}
    for ts, price in spot_history:
        p = round(float(price), 2)
        if p < 15_000 or p > 35_000:
            continue
        key = int(float(ts) // bucket_sec)
        buckets.setdefault(key, []).append(p)
    rows: List[Dict[str, Any]] = []
    for key in sorted(buckets):
        vals = buckets[key]
        if not vals:
            continue
        rows.append(
            {
                "time": _fmt_time(key * bucket_sec),
                "open": vals[0],
                "high": max(vals),
                "low": min(vals),
                "close": vals[-1],
            }
        )
    return rows[-120:]


def _structure_verdict(
    *,
    spot: float,
    market_profile: Dict[str, Any],
    levels: Dict[str, Any],
) -> Dict[str, str]:
    mp = market_profile or {}
    poc = _as_float(mp.get("poc"))
    vah = _as_float(mp.get("vah"))
    val = _as_float(mp.get("val"))
    answers: Dict[str, str] = {}
    balance = str(mp.get("balance_state") or "—")
    answers["trending"] = "YES" if balance == "IMBALANCED_EXPANSION" else "NO"
    answers["balancing"] = "YES" if balance in {"BALANCED_ROTATION", "BALANCED"} else "NO"
    if spot and poc:
        if spot > vah:
            answers["acceptance"] = "Above value — watch rejection"
            answers["rejection"] = "Possible above VAH"
        elif spot < val:
            answers["acceptance"] = "Below value — watch rejection"
            answers["rejection"] = "Possible below VAL"
        else:
            answers["acceptance"] = "Inside value area"
            answers["rejection"] = "NO"
    else:
        answers["acceptance"] = "—"
        answers["rejection"] = "—"
    dh = _as_float(levels.get("day_high"))
    dl = _as_float(levels.get("day_low"))
    if dh and dl and spot:
        span = dh - dl
        pos = (spot - dl) / span if span else 0.5
        answers["auction_complete"] = "YES" if pos > 0.85 or pos < 0.15 else "INCOMPLETE"
    else:
        answers["auction_complete"] = "—"
    answers["auction_incomplete"] = "NO" if answers.get("auction_complete") == "YES" else "YES"
    return answers


def build_workspace_market_structure(
    *,
    spot: float,
    levels: Dict[str, Any],
    market_profile: Dict[str, Any],
    key_levels: Dict[str, Any],
    liquidity_engine: Dict[str, Any],
    relationship_charts: Dict[str, Any],
) -> Dict[str, Any]:
    kl = key_levels or {}
    period = kl.get("period_extremes") or {}
    mp = market_profile or {}
    structure_levels = [
        {"label": "Spot", "price": spot, "role": "live"},
        {"label": "POC", "price": mp.get("poc"), "role": "profile"},
        {"label": "VAH", "price": mp.get("vah"), "role": "profile"},
        {"label": "VAL", "price": mp.get("val"), "role": "profile"},
        {"label": "ORB High", "price": levels.get("orb_high"), "role": "session"},
        {"label": "ORB Low", "price": levels.get("orb_low"), "role": "session"},
        {"label": "Day High", "price": levels.get("day_high"), "role": "session"},
        {"label": "Day Low", "price": levels.get("day_low"), "role": "session"},
        {"label": "Prev Close", "price": levels.get("prev_close"), "role": "reference"},
        {"label": "Prev Day High", "price": period.get("prev_day_high"), "role": "reference"},
        {"label": "Prev Day Low", "price": period.get("prev_day_low"), "role": "reference"},
        {"label": "Week High", "price": period.get("previous_week_high"), "role": "liquidity"},
        {"label": "Week Low", "price": period.get("previous_week_low"), "role": "liquidity"},
        {"label": "Month High", "price": period.get("previous_month_high"), "role": "liquidity"},
        {"label": "Month Low", "price": period.get("previous_month_low"), "role": "liquidity"},
    ]
    pools = (liquidity_engine or {}).get("liquidity_pools") or []
    return {
        "levels": [row for row in structure_levels if row.get("price")],
        "liquidity_pools": pools[:12],
        "volume_profile_proxy": {
            "poc": mp.get("poc"),
            "vah": mp.get("vah"),
            "val": mp.get("val"),
            "bins": mp.get("profile_bins"),
        },
        "questions": _structure_verdict(spot=spot, market_profile=mp, levels=levels or {}),
        "chart": relationship_charts,
    }


def build_workspace_participant(
    *,
    paired_rows: List[Dict[str, Any]],
    chain_bias: Dict[str, Any],
    options_analytics: Dict[str, Any],
    spot: float,
) -> Dict[str, Any]:
    ce_oi = pe_oi = 0
    ce_vel = pe_vel = 0
    for pair in paired_rows:
        ce = pair.get("ce") or {}
        pe = pair.get("pe") or {}
        ce_oi += _as_int(ce.get("oi"))
        pe_oi += _as_int(pe.get("oi"))
        ce_vel += _as_int((ce.get("velocity_5m") or {}).get("delta"))
        pe_vel += _as_int((pe.get("velocity_5m") or {}).get("delta"))
    label = str((chain_bias or {}).get("label") or "NEUTRAL")
    control = "CE writers" if label == "CE_DOMINANT_CHAIN" else (
        "PE writers" if label == "PE_DOMINANT_CHAIN" else "Mixed / chop"
    )
    oa = options_analytics or {}
    return {
        "chain_bias": chain_bias,
        "totals": {"ce_oi": ce_oi, "pe_oi": pe_oi, "ce_oi_5m": ce_vel, "pe_oi_5m": pe_vel},
        "dominant_writer": oa.get("dominant_writer_side"),
        "participant_control": control,
        "questions": {
            "who_in_control": control,
            "writer_buildup": "YES" if max(ce_vel, pe_vel) > 50_000 else "NO",
            "writer_exit": "YES" if min(ce_vel, pe_vel) < -50_000 else "NO",
            "fresh_longs": "Proxy: PE add + spot up" if label == "PE_DOMINANT_CHAIN" else "—",
            "fresh_shorts": "Proxy: CE add + spot flat/down" if label == "CE_DOMINANT_CHAIN" else "—",
        },
        "strike_migration": [
            {
                "strike": pair.get("strike"),
                "ce_oi_5m": _as_int((pair.get("ce") or {}).get("velocity_5m", {}).get("delta")),
                "pe_oi_5m": _as_int((pair.get("pe") or {}).get("velocity_5m", {}).get("delta")),
                "dist": abs(_as_float(pair.get("strike")) - spot) if spot else None,
            }
            for pair in sorted(paired_rows, key=lambda r: abs(_as_float(r.get("strike")) - spot))[:15]
        ],
    }


def build_workspace_dealer(
    *,
    options_analytics: Dict[str, Any],
    relationship_charts: Dict[str, Any],
) -> Dict[str, Any]:
    oa = options_analytics or {}
    gamma = oa.get("gamma_structure") or {}
    chain = oa.get("chain") or []
    walls = sorted(chain, key=lambda r: abs(_as_float(r.get("gex"))), reverse=True)[:8]
    return {
        "net_dealer_delta": oa.get("net_dealer_delta"),
        "net_gex_cr": oa.get("net_gex_cr"),
        "gex_regime": oa.get("gex_regime"),
        "gamma_flip": gamma.get("gamma_flip_strike") or oa.get("gamma_flip_strike"),
        "call_wall": gamma.get("call_wall_strike"),
        "put_wall": gamma.get("put_wall_strike"),
        "vanna_net": oa.get("net_vanna"),
        "charm_net": oa.get("net_charm"),
        "gamma_walls": walls,
        "dealer_hedge_flow": oa.get("dealer_hedge_flow"),
        "questions": {
            "where_defending": str(gamma.get("call_wall_strike") or gamma.get("put_wall_strike") or "—"),
            "gamma_strongest": walls[0].get("strike") if walls else None,
            "pressure_changing": oa.get("gex_regime"),
        },
        "chart": {
            "labels": [str(r.get("strike")) for r in walls],
            "gex": [_as_float(r.get("gex")) for r in walls],
        },
    }


def build_workspace_volatility(
    *,
    volatility_engine: Dict[str, Any],
    options_analytics: Dict[str, Any],
    decision_engine: Dict[str, Any],
    relationship_charts: Dict[str, Any],
) -> Dict[str, Any]:
    ve = volatility_engine or {}
    oa = options_analytics or {}
    em = (decision_engine or {}).get("expected_move_engine") or {}
    prem = (decision_engine or {}).get("premium_evaluation") or {}
    iv = _as_float(oa.get("atm_iv"))
    rv = _as_float(ve.get("realized_vol_pct") or ve.get("short_realized_sigma"))
    return {
        "atm_iv": iv,
        "realized_vol": rv,
        "historical_vol": ve.get("hv20_pct"),
        "atr14_pts": ve.get("atr14_pts"),
        "atr_percentile": ve.get("atr_percentile"),
        "expected_move_pts": oa.get("expected_move_pts") or em.get("expected_move_pts"),
        "iv_rank": ve.get("iv_rank"),
        "iv_percentile": ve.get("iv_percentile"),
        "vol_regime": ve.get("regime"),
        "premium_verdict": prem.get("verdict"),
        "questions": {
            "premium_expensive": "YES" if prem.get("verdict") in {"EXPENSIVE", "AVOID"} else "NO",
            "premium_cheap": "YES" if prem.get("verdict") == "CHEAP" else "NO",
            "implied_above_realized": "YES" if iv and rv and iv > rv else "UNKNOWN",
            "buy_options": "YES" if prem.get("verdict") in {"CHEAP", "BUY_PREMIUM", "FAIR"} else "CAUTION",
            "sell_options": "YES" if prem.get("verdict") in {"EXPENSIVE", "AVOID"} else "NO",
        },
        "chart": relationship_charts,
    }


def build_workspace_liquidity(
    *,
    liquidity_engine: Dict[str, Any],
    key_levels: Dict[str, Any],
) -> Dict[str, Any]:
    liq = liquidity_engine or {}
    grab = _liquidity_grab_info(liq)
    return {
        "pools": liq.get("liquidity_pools") or [],
        "equal_highs": liq.get("equal_highs") or [],
        "equal_lows": liq.get("equal_lows") or [],
        "swing_highs": liq.get("swing_highs") or [],
        "swing_lows": liq.get("swing_lows") or [],
        "grab": grab,
        "prev_week": {
            "high": (key_levels.get("period_extremes") or {}).get("previous_week_high"),
            "low": (key_levels.get("period_extremes") or {}).get("previous_week_low"),
        },
        "prev_month": {
            "high": (key_levels.get("period_extremes") or {}).get("previous_month_high"),
            "low": (key_levels.get("period_extremes") or {}).get("previous_month_low"),
        },
        "questions": {
            "where_liquidity": grab.get("source") or "See pools / equal highs-lows",
            "likely_move": grab.get("direction") or liq.get("read") or "—",
            "liquidity_consumed": "YES" if grab.get("active") else "NO",
        },
    }


def build_workspace_futures(
    *,
    futures_layer: Dict[str, Any],
    spot: float,
) -> Dict[str, Any]:
    fl = futures_layer or {}
    front = (fl.get("contracts") or [{}])[0] if fl.get("contracts") else {}
    return {
        "macro_read": fl.get("macro_read"),
        "front_behavior": fl.get("front_behavior"),
        "basis_pts": front.get("basis"),
        "oi_5m": front.get("oi_velocity_5m"),
        "contracts": fl.get("contracts") or [],
        "questions": {
            "futures_confirming": fl.get("alignment") or fl.get("front_behavior") or "—",
            "institutions_rolling": "See next-month OI delta" if len(fl.get("contracts") or []) > 1 else "—",
            "basis_expanding": "YES" if abs(_as_float(front.get("basis"))) > 30 else "NO",
        },
    }


def build_workspace_time(
    *,
    session_context: Dict[str, Any],
    market_story: List[Dict[str, Any]],
) -> Dict[str, Any]:
    sessions = (session_context or {}).get("sessions") or []
    phases = [
        {"phase": "Pre-market", "time": "08:00–09:15", "status": "PASSED"},
        {"phase": "Open", "time": "09:15", "status": "PASSED"},
        {"phase": "ORB", "time": "09:15–09:30", "status": "PASSED"},
        {"phase": "Initial Balance", "time": "09:15–10:15", "status": "ACTIVE"},
        {"phase": "Morning Trend", "time": "10:15–12:00", "status": "UPCOMING"},
        {"phase": "Lunch", "time": "12:00–13:30", "status": "UPCOMING"},
        {"phase": "Afternoon", "time": "13:30–15:00", "status": "UPCOMING"},
        {"phase": "Power Hour", "time": "15:00–15:20", "status": "UPCOMING"},
        {"phase": "Closing Auction", "time": "15:30", "status": "UPCOMING"},
    ]
    for row in sessions:
        label = str(row.get("label") or "").lower()
        for phase in phases:
            if phase["phase"].lower() in label or label in phase["phase"].lower():
                phase["status"] = row.get("status") or phase["status"]
    return {"phases": phases, "story": market_story, "sessions": sessions}


def build_workspace_strategy(
    *,
    decision_engine: Dict[str, Any],
    decision_timeline: List[Dict[str, Any]],
) -> Dict[str, Any]:
    de = decision_engine or {}
    strat = de.get("strategy_selector") or {}
    strategies: Dict[str, int] = {}
    for row in decision_timeline:
        key = str(row.get("regime") or "—")
        strategies[key] = strategies.get(key, 0) + 1
    return {
        "current": strat,
        "regime_counts": strategies,
        "comparison": [
            {"strategy": "BUY_PREMIUM", "active": strat.get("execution_path", "").startswith("BUY")},
            {"strategy": "SELL_PREMIUM", "active": False, "note": "Not enabled in live engine"},
            {"strategy": "SPREADS", "active": False, "note": "Research only"},
            {"strategy": "NO_TRADE", "active": strat.get("strategy") == "NO_TRADE"},
        ],
        "flow": [
            {"from": "Regime", "to": str((de.get("market_regime") or {}).get("regime"))},
            {"from": "Premium", "to": str((de.get("premium_evaluation") or {}).get("verdict"))},
            {"from": "Strategy", "to": str(strat.get("strategy"))},
        ],
    }


def build_workspace_opportunity(
    journal_dir: Path,
    day: date,
    signals: List[Dict[str, Any]],
) -> Dict[str, Any]:
    rows = _load_jsonl(journal_dir / f"nifty_signal_candidates_{day.isoformat()}.jsonl", limit=3000)
    seen: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("signal_key") or "")
        if key:
            seen[key] = row
    candidates = list(seen.values())
    executed_keys = {str(s.get("signal_key")) for s in signals}
    buckets: Dict[str, List[Dict[str, Any]]] = {"generated": [], "blocked": [], "eligible": [], "executed": []}
    for cand in candidates:
        entry = {
            "time": cand.get("recorded_at") or cand.get("generated_at"),
            "decision": cand.get("decision"),
            "strike": cand.get("strike"),
            "score": cand.get("total_score"),
            "blockers": cand.get("blockers") or [],
        }
        buckets["generated"].append(entry)
        if cand.get("paper_eligible"):
            buckets["eligible"].append(entry)
        else:
            buckets["blocked"].append(entry)
        if str(cand.get("signal_key")) in executed_keys:
            buckets["executed"].append(entry)
    return {
        "counts": {k: len(v) for k, v in buckets.items()},
        "samples": {k: v[-15:] for k, v in buckets.items()},
    }


def build_workspace_counterfactual(
    *,
    decision_engine: Dict[str, Any],
    journal_trades: List[Dict[str, Any]],
) -> Dict[str, Any]:
    plan = (decision_engine or {}).get("counterfactual_engine") or {}
    closed = [t for t in journal_trades if str(t.get("status")) == "CLOSED"]
    actual_pnl = sum(_as_float(t.get("pnl_pct")) for t in closed)
    scenarios = [
        {"id": "actual", "label": "Actual book", "pnl_pct_sum": round(actual_pnl, 2), "trades": len(closed)},
        {
            "id": "ce_only",
            "label": "Only BUY_CE",
            "pnl_pct_sum": round(sum(_as_float(t.get("pnl_pct")) for t in closed if t.get("decision") == "BUY_CE"), 2),
            "trades": sum(1 for t in closed if t.get("decision") == "BUY_CE"),
        },
        {
            "id": "pe_only",
            "label": "Only BUY_PE",
            "pnl_pct_sum": round(sum(_as_float(t.get("pnl_pct")) for t in closed if t.get("decision") == "BUY_PE"), 2),
            "trades": sum(1 for t in closed if t.get("decision") == "BUY_PE"),
        },
        {"id": "decision_only", "label": "Decision-eligible proxy", "note": "Requires full counterfactual batch", "pnl_pct_sum": None},
    ]
    return {
        "status": "research_only",
        "plan": plan,
        "scenarios": scenarios,
        "note": "Visual P&L proxies from paper journal — not execution paths.",
    }


def build_workspace_discovery(
    correlation_explorer: Dict[str, Any],
    journal_stats: Dict[str, Any],
) -> Dict[str, Any]:
    pairs = (correlation_explorer or {}).get("pairs") or []
    leading = [p for p in pairs if p.get("correlation") is not None and abs(p["correlation"]) >= 0.5]
    return {
        "status": "v1_rule_based",
        "note": "Future: statistical discovery across hundreds of sessions (Workspace 19).",
        "hypotheses": [
            {"relationship": p["x"] + " ↔ " + p["y"], "correlation": p["correlation"], "n": p.get("n")}
            for p in sorted(leading, key=lambda x: abs(_as_float(x.get("correlation"))), reverse=True)[:6]
        ],
        "sessions_analyzed": 1,
        "journal_depth": journal_stats,
    }


def build_workspace_executive(
    *,
    decision_engine: Dict[str, Any],
    options_analytics: Dict[str, Any],
    chain_bias: Dict[str, Any],
    liquidity_engine: Dict[str, Any],
    volatility_engine: Dict[str, Any],
    confidence_dashboard: List[Dict[str, Any]],
) -> Dict[str, Any]:
    de = decision_engine or {}
    oa = options_analytics or {}
    return {
        "regime": (de.get("market_regime") or {}).get("regime"),
        "direction": (de.get("direction_manager") or {}).get("mode"),
        "participant_control": (chain_bias or {}).get("label"),
        "dealer_position": oa.get("gex_regime"),
        "premium_status": (de.get("premium_evaluation") or {}).get("verdict"),
        "liquidity_status": (liquidity_engine or {}).get("read") or _liquidity_grab_info(liquidity_engine).get("direction"),
        "expected_move_pts": oa.get("expected_move_pts") or (de.get("expected_move_engine") or {}).get("expected_move_pts"),
        "best_strategy": (de.get("strategy_selector") or {}).get("strategy"),
        "decision_confidence": next((g["score"] for g in confidence_dashboard if g["engine"] == "Overall Decision"), 50),
        "gauges": confidence_dashboard,
    }


def build_market_intelligence_lab_payload(
    *,
    spot: float,
    spot_history: Iterable[Tuple[float, float]],
    expiry: str,
    levels: Optional[Dict[str, Any]],
    key_levels: Optional[Dict[str, Any]],
    morning_context: Optional[Dict[str, Any]],
    session_context: Optional[Dict[str, Any]],
    decision_engine: Optional[Dict[str, Any]],
    options_analytics: Optional[Dict[str, Any]],
    market_profile: Optional[Dict[str, Any]],
    volatility_engine: Optional[Dict[str, Any]],
    liquidity_engine: Optional[Dict[str, Any]],
    futures_layer: Optional[Dict[str, Any]],
    chain_bias: Optional[Dict[str, Any]],
    paired_rows: List[Dict[str, Any]],
    signals: List[Dict[str, Any]],
    behavior_events: List[Dict[str, Any]],
    spot_v5_delta: float = 0.0,
    journal_dir: Path = JOURNAL_DIR,
    day: Optional[date] = None,
) -> Dict[str, Any]:
    """Full Market Intelligence Lab — strictly read-only."""
    d = day or date.today()
    base = build_relationships_lab_payload(
        spot=spot,
        spot_history=spot_history,
        expiry=expiry,
        levels=levels,
        morning_context=morning_context,
        decision_engine=decision_engine,
        options_analytics=options_analytics,
        market_profile=market_profile,
        volatility_engine=volatility_engine,
        liquidity_engine=liquidity_engine,
        paired_rows=paired_rows,
        signals=signals,
        behavior_events=behavior_events,
        spot_v5_delta=spot_v5_delta,
        journal_dir=journal_dir,
        day=d,
    )
    decision_timeline = base.get("decision_timeline") or []
    relationship_charts = base.get("relationship_charts") or {}
    journal_trades = load_paper_trades_from_journal(journal_dir, d)
    confidence = base.get("confidence_dashboard") or []
    correlation = base.get("correlation_explorer") or {}

    workspaces = {
        "market_structure": build_workspace_market_structure(
            spot=spot,
            levels=levels or {},
            market_profile=market_profile or {},
            key_levels=key_levels or {},
            liquidity_engine=liquidity_engine or {},
            relationship_charts=relationship_charts,
        ),
        "participant": build_workspace_participant(
            paired_rows=paired_rows,
            chain_bias=chain_bias or {},
            options_analytics=options_analytics or {},
            spot=spot,
        ),
        "dealer": build_workspace_dealer(
            options_analytics=options_analytics or {},
            relationship_charts=relationship_charts,
        ),
        "multi_expiry": base.get("multi_expiry_surface") or {},
        "volatility": build_workspace_volatility(
            volatility_engine=volatility_engine or {},
            options_analytics=options_analytics or {},
            decision_engine=decision_engine or {},
            relationship_charts=relationship_charts,
        ),
        "liquidity": build_workspace_liquidity(
            liquidity_engine=liquidity_engine or {},
            key_levels=key_levels or {},
        ),
        "futures": build_workspace_futures(futures_layer=futures_layer or {}, spot=spot),
        "time": build_workspace_time(
            session_context=session_context or {},
            market_story=base.get("market_story_timeline") or [],
        ),
        "strategy": build_workspace_strategy(
            decision_engine=decision_engine or {},
            decision_timeline=decision_timeline,
        ),
        "decision": {
            "timeline": decision_timeline,
            "current": decision_engine or {},
        },
        "trade": {"replays": base.get("trade_replay") or []},
        "learning": {"observations": base.get("learning_journal") or []},
        "correlation": correlation,
        "cause_effect": base.get("cause_effect") or {},
        "multi_timeframe": {
            "m1": _bucket_ohlc(spot_history, 60),
            "m5": _bucket_ohlc(spot_history, 300),
            "m15": _bucket_ohlc(spot_history, 900),
            "m30": _bucket_ohlc(spot_history, 1800),
            "h1": _bucket_ohlc(spot_history, 3600),
        },
        "market_replay": base.get("session_replay") or {},
        "opportunity": build_workspace_opportunity(journal_dir, d, signals + journal_trades),
        "counterfactual": build_workspace_counterfactual(
            decision_engine=decision_engine or {},
            journal_trades=journal_trades,
        ),
        "discovery": build_workspace_discovery(
            correlation_explorer=correlation,
            journal_stats=base.get("journal_stats") or {},
        ),
        "executive": build_workspace_executive(
            decision_engine=decision_engine or {},
            options_analytics=options_analytics or {},
            chain_bias=chain_bias or {},
            liquidity_engine=liquidity_engine or {},
            volatility_engine=volatility_engine or {},
            confidence_dashboard=confidence,
        ),
    }

    return {
        **base,
        "name": "Market Intelligence Lab",
        "replay_day": d.isoformat(),
        "note": "Research, visualization and learning only — never affects signals, decisions, or execution.",
        "workspace_catalog": WORKSPACE_CATALOG,
        "workspaces": workspaces,
        "heatmaps": base.get("heatmaps") or {},
    }


def build_intelligence_lab_with_calendar(
    *,
    journal_dir: Path,
    replay_day: Optional[date] = None,
    live_kwargs: Dict[str, Any],
) -> Dict[str, Any]:
    """Build lab payload for a specific day, with available-day calendar."""
    d = replay_day or date.today()
    available = list_available_journal_days(journal_dir)
    if d.isoformat() not in available and available:
        d = date.fromisoformat(available[0])

    if d == date.today() and live_kwargs.get("use_live", True):
        payload = build_market_intelligence_lab_payload(**{k: v for k, v in live_kwargs.items() if k != "use_live"}, day=d)
    else:
        hist = load_journal_day_context(journal_dir, d)
        oa = hist.get("options_analytics") or {}
        payload = build_market_intelligence_lab_payload(
            spot=_as_float(hist.get("spot")),
            spot_history=[],
            expiry=str(oa.get("expiry") or ""),
            levels=hist.get("levels") or {},
            key_levels=hist.get("key_levels") or {},
            morning_context=hist.get("morning_context") or {},
            session_context={},
            decision_engine=hist.get("decision_engine") or {},
            options_analytics=oa,
            market_profile={},
            volatility_engine={"regime": oa.get("premium_vs_vix"), "atr14_pts": None},
            liquidity_engine={},
            futures_layer={},
            chain_bias={"label": "HISTORICAL_JOURNAL"},
            paired_rows=[],
            signals=hist.get("trades") or [],
            behavior_events=[],
            spot_v5_delta=0.0,
            journal_dir=journal_dir,
            day=d,
        )
        payload["historical_mode"] = True
        payload["day_inventory"] = hist.get("inventory") or {}

    payload["replay_day"] = d.isoformat()
    payload["available_days"] = [journal_day_inventory(journal_dir, date.fromisoformat(ds)) for ds in available]
    return payload


def _selftest() -> None:
    import tempfile

    assert len(WORKSPACE_CATALOG) == 20
    assert WORKSPACE_CATALOG[0]["id"] == "market_structure"
    assert WORKSPACE_CATALOG[-1]["id"] == "executive"

    spot_hist = [(1000.0 + i * 60, 23000.0 + i * 2) for i in range(20)]
    m1 = _bucket_ohlc(spot_hist, 60)
    assert len(m1) == 20 and m1[0]["open"] == 23000.0

    verdict = _structure_verdict(spot=23100.0, market_profile={"poc": 23000, "vah": 23050, "val": 22950, "balance_state": "IMBALANCED_EXPANSION"}, levels={})
    assert verdict["trending"] == "YES"
    assert verdict["acceptance"] == "Above value — watch rejection"

    structure = build_workspace_market_structure(
        spot=23100.0, levels={"day_high": 23150}, market_profile={"poc": 23000},
        key_levels={}, liquidity_engine={}, relationship_charts={},
    )
    assert any(row["label"] == "Spot" for row in structure["levels"])

    participant = build_workspace_participant(
        paired_rows=[{"strike": 23000, "ce": {"oi": 100, "velocity_5m": {"delta": 60000}}, "pe": {"oi": 200, "velocity_5m": {"delta": -10000}}}],
        chain_bias={"label": "CE_DOMINANT_CHAIN"}, options_analytics={}, spot=23050.0,
    )
    assert participant["totals"]["ce_oi"] == 100
    assert participant["questions"]["writer_buildup"] == "YES"

    dealer = build_workspace_dealer(options_analytics={"chain": [{"strike": 23000, "gex": 5.0}, {"strike": 23100, "gex": -8.0}]}, relationship_charts={})
    assert dealer["gamma_walls"][0]["strike"] == 23100  # largest abs(gex) first

    vol = build_workspace_volatility(volatility_engine={}, options_analytics={"atm_iv": 15.0}, decision_engine={"premium_evaluation": {"verdict": "CHEAP"}}, relationship_charts={})
    assert vol["questions"]["premium_cheap"] == "YES"

    liq = build_workspace_liquidity(liquidity_engine={"liquidity_grab": "UPSIDE_LIQUIDITY_GRAB"}, key_levels={})
    assert liq["questions"]["liquidity_consumed"] == "YES"

    fut = build_workspace_futures(futures_layer={"contracts": [{"basis": 45.0}]}, spot=23000.0)
    assert fut["questions"]["basis_expanding"] == "YES"

    time_ws = build_workspace_time(session_context={}, market_story=[])
    assert len(time_ws["phases"]) == 9

    strat = build_workspace_strategy(decision_engine={"strategy_selector": {"strategy": "NO_TRADE"}}, decision_timeline=[{"regime": "TREND"}, {"regime": "TREND"}])
    assert strat["regime_counts"]["TREND"] == 2
    assert any(c["strategy"] == "NO_TRADE" and c["active"] for c in strat["comparison"])

    tmp = Path(tempfile.mkdtemp(prefix="mil-selftest-"))
    day = date(2026, 6, 19)
    opp = build_workspace_opportunity(tmp, day, [])
    assert opp["counts"]["generated"] == 0

    cf = build_workspace_counterfactual(decision_engine={}, journal_trades=[{"status": "CLOSED", "decision": "BUY_CE", "pnl_pct": 10.0}])
    assert cf["scenarios"][0]["pnl_pct_sum"] == 10.0
    assert cf["scenarios"][1]["trades"] == 1  # ce_only

    disc = build_workspace_discovery(correlation_explorer={"pairs": [{"x": "Spot", "y": "GEX", "correlation": 0.8, "n": 10}]}, journal_stats={})
    assert len(disc["hypotheses"]) == 1

    exec_ws = build_workspace_executive(
        decision_engine={"market_regime": {"regime": "TREND"}}, options_analytics={},
        chain_bias={}, liquidity_engine={}, volatility_engine={},
        confidence_dashboard=[{"engine": "Overall Decision", "score": 80}],
    )
    assert exec_ws["decision_confidence"] == 80

    payload = build_market_intelligence_lab_payload(
        spot=23100.0, spot_history=spot_hist, expiry="2026-06-25",
        levels={}, key_levels={}, morning_context={}, session_context={},
        decision_engine={}, options_analytics={}, market_profile={},
        volatility_engine={}, liquidity_engine={}, futures_layer={},
        chain_bias={}, paired_rows=[], signals=[], behavior_events=[],
        journal_dir=tmp, day=day,
    )
    assert payload["name"] == "Market Intelligence Lab"
    assert len(payload["workspace_catalog"]) == 20
    assert set(payload["workspaces"].keys()) == {w["id"] for w in WORKSPACE_CATALOG}

    calendar_payload = build_intelligence_lab_with_calendar(
        journal_dir=tmp, replay_day=day,
        live_kwargs={
            "spot": 23100.0, "spot_history": spot_hist, "expiry": "2026-06-25",
            "levels": {}, "key_levels": {}, "morning_context": {}, "session_context": {},
            "decision_engine": {}, "options_analytics": {}, "market_profile": {},
            "volatility_engine": {}, "liquidity_engine": {}, "futures_layer": {},
            "chain_bias": {}, "paired_rows": [], "signals": [], "behavior_events": [],
            "use_live": False,
        },
    )
    assert calendar_payload["historical_mode"] is True
    assert calendar_payload["replay_day"] == "2026-06-19"

    print("[analytics.market_intelligence_lab] selftest OK: 20 workspaces, catalog, payload + calendar assembly")


if __name__ == "__main__":
    _selftest()
