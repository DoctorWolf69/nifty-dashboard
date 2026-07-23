#!/usr/bin/env python3
"""Official NSE cash FII/DII (buy/sell/net) and F&O participant OI/vol summaries.

Ported faithfully from quant-desk-engine v4/ATLAS's nse_official_flows.py
(mentor-authored). No logic changed. Adaptations: BASE_DIR/RAW_EOD_DIR/
JOURNAL_DIR resolve via nifty.paths instead of bare __file__-relative
parents; imports from nifty.sources.fii_dii (this session's port) instead
of the standalone nifty_fii_dii module; the local `from desk_eod_filing
import _participant_summary` now imports from nifty.eod.filing (already
ported, same function, same nse_eod_filing_{date}.json journal naming).

Genuinely new capability: nifty-dashboard's existing FII/DII fetch
(nifty/morning/phases.py) only surfaces net values. This module adds
buy/sell/turnover gross-flow positioning labels (POSITIONING_CHURN /
DISTRIBUTION / ACCUMULATION / etc. — net-only figures miss churn/roll
activity that a large two-way book can hide) plus F&O participant-wise
(FII/DII/Pro/Client) index futures+options OI/volume net contracts,
weekly/monthly aggregation, and a PTE-vs-official-positioning divergence
check.

`participant_brief_from_filing` degrades gracefully today: it reads
nifty.eod.filing's `participant_oi_summary` (already populated) and
`participant_vol_summary` (not yet populated by that module — passed
through as an empty list, which `participant_positioning_summary` accepts
as `vol_rows=None` and simply omits the *_vol_net fields). Not a broken
dependency — a documented, honest degradation matching the null-safe
pattern used throughout this porting effort.

Not yet wired into the live pipeline.
Self-check: python -m nifty.sources.nse_official_flows
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from nifty.paths import JOURNAL_DIR, NSE_EOD_DIR
from nifty.sources.fii_dii import _as_float, fii_net_from_rows, fii_rows_trade_date

RAW_EOD_DIR = NSE_EOD_DIR / "raw"

POSITIONING_SHORT: Dict[str, str] = {
    "POSITIONING_CHURN": "Churn / roll",
    "NET_SELL_HEAVY_TWO_WAY": "Net sell, heavy buy",
    "NET_BUY_HEAVY_TWO_WAY": "Net buy, heavy sell",
    "DISTRIBUTION": "Distribution",
    "ACCUMULATION": "Accumulation",
    "MILD_NET_SELL": "Mild net sell",
    "MILD_NET_BUY": "Mild net buy",
    "BALANCED": "Balanced",
}


def positioning_label_short(label: Optional[str]) -> str:
    return POSITIONING_SHORT.get(str(label or ""), str(label or "—").replace("_", " ").title())


def previous_trading_day(start: date) -> date:
    cur = start - timedelta(days=1)
    while cur.weekday() >= 5:
        cur -= timedelta(days=1)
    return cur


def week_id(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def month_id(d: date) -> str:
    return d.strftime("%Y-%m")


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _dominant_label(labels: List[str]) -> Optional[str]:
    clean = [label for label in labels if label]
    if not clean:
        return None
    return Counter(clean).most_common(1)[0][0]


def _aggregate_fii_bucket(rows: List[Dict[str, Any]], bucket_id: str, bucket_kind: str) -> Dict[str, Any]:
    buy = sum(_as_float(r.get("fii_buy_crores")) or 0.0 for r in rows)
    sell = sum(_as_float(r.get("fii_sell_crores")) or 0.0 for r in rows)
    net = sum(_as_float(r.get("fii_net_crores")) or 0.0 for r in rows)
    dii_net = sum(_as_float(r.get("dii_net_crores")) or 0.0 for r in rows)
    daily_labels = [str(r.get("fii_positioning") or "") for r in rows if r.get("fii_positioning")]
    side = {
        "buy_crores": round(buy, 2) if buy else None,
        "sell_crores": round(sell, 2) if sell else None,
        "net_crores": round(net, 2) if net or sell or buy else None,
        "turnover_crores": round(buy + sell, 2) if buy or sell else None,
    }
    agg_label = fii_positioning_label(side) if side.get("buy_crores") and side.get("sell_crores") else _dominant_label(daily_labels)
    label_seq = " → ".join(positioning_label_short(l) for l in daily_labels)
    return {
        "bucket_id": bucket_id,
        "bucket_kind": bucket_kind,
        "sessions": len(rows),
        "start_date": min(str(r.get("trade_date") or "") for r in rows),
        "end_date": max(str(r.get("trade_date") or "") for r in rows),
        "fii_buy_crores": side.get("buy_crores"),
        "fii_sell_crores": side.get("sell_crores"),
        "fii_net_crores": side.get("net_crores"),
        "dii_net_crores": round(dii_net, 2) if dii_net or any(r.get("dii_net_crores") is not None for r in rows) else None,
        "fii_positioning": agg_label,
        "daily_labels": daily_labels,
        "label_sequence": label_seq,
        "fii_read": positioning_read(agg_label, side, "FII") if agg_label and side.get("buy_crores") else "",
    }


def aggregate_fii_weekly(daily: List[Dict[str, Any]], max_weeks: int = 4) -> List[Dict[str, Any]]:
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for row in daily:
        td = str(row.get("trade_date") or "")[:10]
        try:
            d = date.fromisoformat(td)
        except ValueError:
            continue
        buckets.setdefault(week_id(d), []).append(row)
    out: List[Dict[str, Any]] = []
    for wid in sorted(buckets.keys())[-max_weeks:]:
        out.append(_aggregate_fii_bucket(buckets[wid], wid, "week"))
    return out


def aggregate_fii_monthly(daily: List[Dict[str, Any]], max_months: int = 3) -> List[Dict[str, Any]]:
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for row in daily:
        td = str(row.get("trade_date") or "")[:10]
        try:
            d = date.fromisoformat(td)
        except ValueError:
            continue
        buckets.setdefault(month_id(d), []).append(row)
    out: List[Dict[str, Any]] = []
    for mid in sorted(buckets.keys())[-max_months:]:
        out.append(_aggregate_fii_bucket(buckets[mid], mid, "month"))
    return out


def fii_transition_narrative(weekly: List[Dict[str, Any]], daily: List[Dict[str, Any]]) -> str:
    if not weekly:
        return "Insufficient FII history for weekly transition read."
    parts: List[str] = []
    week_labels = [positioning_label_short(w.get("fii_positioning")) for w in weekly]
    if len(week_labels) >= 2:
        parts.append(f"Last {len(week_labels)} weeks: {' → '.join(week_labels)}.")
    latest = weekly[-1]
    if latest.get("fii_read"):
        parts.append(str(latest["fii_read"]))
    recent_daily = daily[-5:] if daily else []
    if len(recent_daily) >= 2:
        seq = " → ".join(positioning_label_short(r.get("fii_positioning")) for r in recent_daily if r.get("fii_positioning"))
        if seq:
            parts.append(f"Recent daily shift: {seq}.")
    return " ".join(parts) if parts else "Watch FII buy/sell turnover — net-only is insufficient."


def participant_brief_from_filing(session_date: str) -> Dict[str, Any]:
    filing = _read_json(JOURNAL_DIR / f"nse_eod_filing_{session_date}.json")
    if filing.get("participant_positioning"):
        out = dict(filing["participant_positioning"])
        out["trade_date"] = session_date
        return out
    oi = filing.get("participant_oi_summary") or []
    vol = filing.get("participant_vol_summary") or []
    if not oi:
        return {}
    summary = participant_positioning_summary(oi, vol or None)
    summary["trade_date"] = session_date
    return summary


def load_participant_daily_series(end_date: date, max_sessions: int = 22) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    cursor = end_date
    seen = 0
    attempts = 0
    while seen < max_sessions and attempts < max_sessions * 4:
        attempts += 1
        label = cursor.isoformat()
        brief = participant_brief_from_filing(label)
        if brief.get("participants"):
            out.append(brief)
            seen += 1
        cursor = previous_trading_day(cursor)
    out.reverse()
    return out


def _participant_week_snapshot(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not rows:
        return {}
    last = rows[-1]
    participants = last.get("participants") or {}
    vol_sums: Dict[str, Dict[str, int]] = {}
    for row in rows:
        for name, payload in (row.get("participants") or {}).items():
            slot = vol_sums.setdefault(name, {})
            for key in ("index_fut_vol_net", "index_call_vol_net", "index_put_vol_net"):
                val = payload.get(key)
                if val is None:
                    continue
                slot[key] = int(slot.get(key, 0) + int(val))
    return {
        "trade_date": last.get("trade_date"),
        "participants": participants,
        "vol_sums": vol_sums,
        "sessions": len(rows),
        "start_date": rows[0].get("trade_date"),
        "end_date": last.get("trade_date"),
    }


def aggregate_participant_weekly(daily: List[Dict[str, Any]], max_weeks: int = 4) -> List[Dict[str, Any]]:
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for row in daily:
        td = str(row.get("trade_date") or "")[:10]
        try:
            d = date.fromisoformat(td)
        except ValueError:
            continue
        buckets.setdefault(week_id(d), []).append(row)
    out: List[Dict[str, Any]] = []
    prev_fii_fut: Optional[int] = None
    for wid in sorted(buckets.keys())[-max_weeks:]:
        snap = _participant_week_snapshot(buckets[wid])
        fii = (snap.get("participants") or {}).get("FII") or {}
        fut = fii.get("index_fut_net_contracts")
        delta = int(fut - prev_fii_fut) if fut is not None and prev_fii_fut is not None else None
        if fut is not None:
            prev_fii_fut = int(fut)
        snap["week_id"] = wid
        snap["fii_index_fut_delta_vs_prior_week"] = delta
        out.append(snap)
    return out


def aggregate_participant_monthly(daily: List[Dict[str, Any]], max_months: int = 3) -> List[Dict[str, Any]]:
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for row in daily:
        td = str(row.get("trade_date") or "")[:10]
        try:
            d = date.fromisoformat(td)
        except ValueError:
            continue
        buckets.setdefault(month_id(d), []).append(row)
    out: List[Dict[str, Any]] = []
    prev_fii_fut: Optional[int] = None
    for mid in sorted(buckets.keys())[-max_months:]:
        snap = _participant_week_snapshot(buckets[mid])
        fii = (snap.get("participants") or {}).get("FII") or {}
        fut = fii.get("index_fut_net_contracts")
        delta = int(fut - prev_fii_fut) if fut is not None and prev_fii_fut is not None else None
        if fut is not None:
            prev_fii_fut = int(fut)
        snap["month_id"] = mid
        snap["fii_index_fut_delta_vs_prior_month"] = delta
        out.append(snap)
    return out


def build_multiframe_flows_payload(
    trade_date: date,
    fii_daily: List[Dict[str, Any]],
    *,
    participant_end: Optional[date] = None,
) -> Dict[str, Any]:
    end = participant_end or previous_trading_day(trade_date)
    participant_daily = load_participant_daily_series(end, max_sessions=22)
    fii_weekly = aggregate_fii_weekly(fii_daily, max_weeks=4)
    fii_monthly = aggregate_fii_monthly(fii_daily, max_months=3)
    participant_weekly = aggregate_participant_weekly(participant_daily, max_weeks=4)
    participant_monthly = aggregate_participant_monthly(participant_daily, max_months=3)
    return {
        "as_of": trade_date.isoformat(),
        "participant_end_session": end.isoformat(),
        "fii": {
            "daily": fii_daily,
            "weekly": fii_weekly,
            "monthly": fii_monthly,
            "transition_narrative": fii_transition_narrative(fii_weekly, fii_daily),
        },
        "participant": {
            "daily": participant_daily,
            "weekly": participant_weekly,
            "monthly": participant_monthly,
        },
    }


def load_pte_open_snapshot(trade_date: date, open_time: time = time(9, 15)) -> Dict[str, Any]:
    path = JOURNAL_DIR / f"participant_theory_set_{trade_date.isoformat()}.jsonl"
    if not path.exists():
        alt = sorted(JOURNAL_DIR.glob(f"participant_theory_set_{trade_date.isoformat()}*.jsonl"))
        path = alt[0] if alt else path
    if not path.exists():
        return {"loaded": False, "trade_date": trade_date.isoformat()}

    target = datetime.combine(trade_date, open_time)
    best: Optional[Dict[str, Any]] = None
    best_dt: Optional[datetime] = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts_raw = str(row.get("timestamp") or row.get("recorded_at") or "")
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            if ts.tzinfo is not None:
                # Values in journal are already IST-local when offset is +05:30
                if str(ts.tzinfo) in ("UTC+05:30", "+05:30") or "+05:30" in ts_raw:
                    ts = ts.replace(tzinfo=None)
                else:
                    ts = (ts - timedelta(hours=5, minutes=30)).replace(tzinfo=None)
        except ValueError:
            try:
                ts = datetime.strptime(ts_raw[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
        if ts.date() != trade_date:
            continue
        if ts < target:
            continue
        if best_dt is None or ts < best_dt:
            best = row
            best_dt = ts
    if best is None:
        return {"loaded": False, "trade_date": trade_date.isoformat(), "note": "no_pte_after_0915"}
    theories = best.get("theories") or []
    ranked = sorted(
        theories,
        key=lambda t: (_as_float(t.get("share")) or 0.0, _as_float(t.get("strength")) or 0.0),
        reverse=True,
    )
    dominant = ranked[0] if ranked else {}
    return {
        "loaded": True,
        "trade_date": trade_date.isoformat(),
        "timestamp": best.get("timestamp") or best.get("recorded_at"),
        "theories": theories,
        "dominant": {
            "catalog_id": dominant.get("catalog_id"),
            "name": dominant.get("name"),
            "share": dominant.get("share"),
            "strength": dominant.get("strength"),
            "state": dominant.get("state"),
        },
    }


def official_participant_bias(participant_brief: Dict[str, Any]) -> Dict[str, Any]:
    fii = (participant_brief.get("participants") or {}).get("FII") or {}
    fut = fii.get("index_fut_net_contracts")
    call = fii.get("index_call_oi_net")
    put = fii.get("index_put_oi_net")
    bias = "NEUTRAL"
    if fut is not None and fut < -50000:
        bias = "BEARISH_FUT"
    elif fut is not None and fut > 50000:
        bias = "BULLISH_FUT"
    if put is not None and call is not None and put > call + 50000:
        bias = "HEDGED_BEARISH" if bias == "BEARISH_FUT" else "PUT_HEAVY"
    read_parts = []
    if fut is not None:
        read_parts.append(f"FII index fut net {fut:+,}")
    if call is not None and put is not None:
        read_parts.append(f"call {call:+,} / put {put:+,}")
    return {"bias": bias, "read": "; ".join(read_parts) if read_parts else "No official participant snapshot."}


def _pte_bias(pte: Dict[str, Any]) -> Dict[str, Any]:
    dom = pte.get("dominant") or {}
    cid = str(dom.get("catalog_id") or "")
    mapping = {
        "breakout_expansion": "BULLISH",
        "trend": "BULLISH",
        "range_compression": "NEUTRAL",
        "balance": "NEUTRAL",
        "transition": "NEUTRAL",
        "premium_writing": "NEUTRAL",
    }
    return {
        "bias": mapping.get(cid, "NEUTRAL"),
        "catalog_id": cid,
        "name": dom.get("name"),
        "share": dom.get("share"),
        "strength": dom.get("strength"),
    }


def compare_pte_vs_official(
    official_participant: Dict[str, Any],
    pte_open: Dict[str, Any],
) -> Dict[str, Any]:
    official = official_participant_bias(official_participant)
    pte = _pte_bias(pte_open) if pte_open.get("loaded") else {"bias": "UNKNOWN"}
    ob = official.get("bias", "NEUTRAL")
    pb = pte.get("bias", "UNKNOWN")
    aligned = ob == pb or pb in ("NEUTRAL", "UNKNOWN") or ob == "NEUTRAL"
    divergence = "LOW" if aligned else "HIGH"
    if pb == "BULLISH" and ob in ("BEARISH_FUT", "HEDGED_BEARISH", "PUT_HEAVY"):
        divergence = "HIGH"
    read = ""
    if not pte_open.get("loaded"):
        read = "PTE open snapshot not available yet — compare after 09:15 capture."
    elif divergence == "HIGH":
        read = (
            f"PTE at open favours {pte.get('name')} ({pte.get('share')}% share) vs official EOD "
            f"{official.get('read')} — interpret as possible roll/hedge divergence, not automatic fade."
        )
    else:
        read = f"PTE ({pte.get('name')}) and official participant positioning are broadly aligned ({ob})."
    return {
        "official": official,
        "pte": pte,
        "pte_timestamp": pte_open.get("timestamp"),
        "divergence": divergence,
        "aligned": aligned,
        "read": read,
        "calibration_note": "Store divergence for future PTE calibration against NSE EOD participant OI.",
    }


def chart_series_payload(multiframe: Dict[str, Any]) -> Dict[str, Any]:
    """Compact series for embedded HTML chart tabs."""
    fii = multiframe.get("fii") or {}
    part = multiframe.get("participant") or {}

    def _fii_points(rows: List[Dict[str, Any]], label_key: str) -> List[Dict[str, Any]]:
        return [
            {
                "label": str(r.get(label_key) or r.get("end_date") or "")[-7:],
                "buy": r.get("fii_buy_crores"),
                "sell": r.get("fii_sell_crores"),
                "net": r.get("fii_net_crores"),
            }
            for r in rows
        ]

    def _part_points(rows: List[Dict[str, Any]], label_key: str) -> List[Dict[str, Any]]:
        pts = []
        for r in rows:
            fii = (r.get("participants") or {}).get("FII") or {}
            pts.append(
                {
                    "label": str(r.get(label_key) or r.get("end_date") or r.get("trade_date") or ""),
                    "fii_fut": fii.get("index_fut_net_contracts"),
                    "fii_put": fii.get("index_put_oi_net"),
                }
            )
        return pts

    daily_fii = fii.get("daily") or []
    return {
        "fii": {
            "daily": _fii_points(daily_fii[-10:], "trade_date"),
            "weekly": _fii_points(fii.get("weekly") or [], "week_id"),
            "monthly": _fii_points(fii.get("monthly") or [], "month_id"),
        },
        "participant": {
            "daily": _part_points((part.get("daily") or [])[-10:], "trade_date"),
            "weekly": _part_points(part.get("weekly") or [], "week_id"),
            "monthly": _part_points(part.get("monthly") or [], "month_id"),
        },
    }


def _side_block(row: Dict[str, Any], prefix: str) -> Dict[str, Optional[float]]:
    buy = _as_float(row.get("buyValue") or row.get("buyvalue"))
    sell = _as_float(row.get("sellValue") or row.get("sellvalue"))
    net = _as_float(row.get("netValue") or row.get("netvalue"))
    if net is None and buy is not None and sell is not None:
        net = round(buy - sell, 2)
    turnover = round(buy + sell, 2) if buy is not None and sell is not None else None
    sell_ratio = round(sell / buy, 3) if buy and buy > 0 and sell is not None else None
    net_pct_turnover = (
        round(abs(net) / turnover * 100, 1) if net is not None and turnover and turnover > 0 else None
    )
    return {
        "buy_crores": buy,
        "sell_crores": sell,
        "net_crores": net,
        "turnover_crores": turnover,
        "sell_to_buy_ratio": sell_ratio,
        "net_pct_of_turnover": net_pct_turnover,
    }


def fii_positioning_label(side: Dict[str, Optional[float]]) -> str:
    """Net-only labels miss churn/roll — classify gross flow + net together."""
    buy = side.get("buy_crores")
    sell = side.get("sell_crores")
    net = side.get("net_crores")
    turnover = side.get("turnover_crores")
    if buy is None or sell is None:
        return "UNKNOWN"
    if not turnover or turnover <= 0:
        return "NO_FLOW"
    net_abs_pct = side.get("net_pct_of_turnover") or 0
    if net_abs_pct < 12 and turnover >= 15000:
        return "POSITIONING_CHURN"
    if net is not None and net < -500:
        if buy and sell and buy >= sell * 0.65:
            return "NET_SELL_HEAVY_TWO_WAY"
        return "DISTRIBUTION"
    if net is not None and net > 500:
        if sell and buy and sell >= buy * 0.65:
            return "NET_BUY_HEAVY_TWO_WAY"
        return "ACCUMULATION"
    if net is not None and net < 0:
        return "MILD_NET_SELL"
    if net is not None and net > 0:
        return "MILD_NET_BUY"
    return "BALANCED"


def positioning_read(label: str, side: Dict[str, Optional[float]], who: str = "FII") -> str:
    buy = side.get("buy_crores")
    sell = side.get("sell_crores")
    net = side.get("net_crores")
    if label == "POSITIONING_CHURN":
        return (
            f"{who} churn — bought ₹{buy:,.0f} Cr and sold ₹{sell:,.0f} Cr; "
            f"net {net:+,.0f} Cr is small vs turnover (roll/rebalance, not pure direction)."
        )
    if label == "NET_SELL_HEAVY_TWO_WAY":
        return (
            f"{who} net sold ₹{abs(net or 0):,.0f} Cr but still bought ₹{buy:,.0f} Cr — "
            f"two-way book; do not read net-only as fully bearish."
        )
    if label == "DISTRIBUTION":
        return f"{who} distribution — sell ₹{sell:,.0f} Cr vs buy ₹{buy:,.0f} Cr (net {net:+,.0f} Cr)."
    if label == "ACCUMULATION":
        return f"{who} accumulation — buy ₹{buy:,.0f} Cr vs sell ₹{sell:,.0f} Cr (net {net:+,.0f} Cr)."
    if label == "MILD_NET_SELL":
        return f"{who} mild net sell {net:+,.0f} Cr on ₹{buy + sell:,.0f} Cr turnover."
    if label == "MILD_NET_BUY":
        return f"{who} mild net buy {net:+,.0f} Cr on ₹{buy + sell:,.0f} Cr turnover."
    return f"{who} flow {label.lower().replace('_', ' ')}."


def fii_dii_entry_from_rows(
    rows: List[Dict[str, Any]],
    *,
    trade_date: Optional[str] = None,
    source: str = "unknown",
) -> Dict[str, Any]:
    fii_row = next((r for r in rows if "FII" in str(r.get("category", "")).upper()), {})
    dii_row = next((r for r in rows if str(r.get("category", "")).upper() == "DII"), {})
    parsed = fii_rows_trade_date(rows)
    label = trade_date or (parsed.isoformat() if parsed else "")
    day_label = str(fii_row.get("date") or dii_row.get("date") or "")

    fii = _side_block(fii_row, "fii")
    dii = _side_block(dii_row, "dii")
    fii_label = fii_positioning_label(fii)
    dii_label = fii_positioning_label(dii)

    fii_net, dii_net, _ = fii_net_from_rows(rows)

    return {
        "trade_date": label,
        "fii_dii_date": day_label,
        "source": source,
        "fii_buy_crores": fii.get("buy_crores"),
        "fii_sell_crores": fii.get("sell_crores"),
        "fii_net_crores": fii.get("net_crores") if fii.get("net_crores") is not None else fii_net,
        "dii_buy_crores": dii.get("buy_crores"),
        "dii_sell_crores": dii.get("sell_crores"),
        "dii_net_crores": dii.get("net_crores") if dii.get("net_crores") is not None else dii_net,
        "fii": fii,
        "dii": dii,
        "fii_positioning": fii_label,
        "dii_positioning": dii_label,
        "fii_read": positioning_read(fii_label, fii, "FII"),
        "dii_read": positioning_read(dii_label, dii, "DII"),
    }


def rows_from_summary_entry(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Rebuild NSE-shaped rows when buy/sell exist on history entry."""
    day_label = str(entry.get("fii_dii_date") or entry.get("trade_date") or "")

    def _row(category: str, prefix: str) -> Dict[str, Any]:
        side = entry.get(prefix) or {}
        buy = entry.get(f"{prefix}_buy_crores") or side.get("buy_crores")
        sell = entry.get(f"{prefix}_sell_crores") or side.get("sell_crores")
        net = entry.get(f"{prefix}_net_crores") or side.get("net_crores")
        if buy is None and sell is None and net is not None:
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
    return rows


def _participant_row(participant_rows: List[Dict[str, Any]], name: str) -> Dict[str, Any]:
    return next((r for r in participant_rows if str(r.get("participant", "")).upper() == name.upper()), {})


def _net_contracts(row: Dict[str, Any], long_key: str, short_key: str) -> Optional[int]:
    long_v = row.get(long_key)
    short_v = row.get(short_key)
    try:
        if long_v is None and short_v is None:
            return None
        return int(long_v or 0) - int(short_v or 0)
    except (TypeError, ValueError):
        return None


def participant_positioning_summary(
    oi_rows: List[Dict[str, Any]],
    vol_rows: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Macro participant positioning for morning/EOD briefs — index F&O + key option legs."""
    if not oi_rows:
        return {"error": "no_participant_oi"}

    out: Dict[str, Any] = {"participants": {}, "reads": []}
    for name in ("FII", "DII", "Pro", "Client"):
        row = _participant_row(oi_rows, name)
        if not row:
            continue
        index_fut_net = _net_contracts(row, "Future Index Long", "Future Index Short")
        index_call_net = _net_contracts(row, "Option Index Call Long", "Option Index Call Short")
        index_put_net = _net_contracts(row, "Option Index Put Long", "Option Index Put Short")
        total_long = row.get("Total Long Contracts")
        total_short = row.get("Total Short Contracts")
        payload = {
            "index_fut_net_contracts": index_fut_net,
            "index_call_oi_net": index_call_net,
            "index_put_oi_net": index_put_net,
            "total_long_contracts": total_long,
            "total_short_contracts": total_short,
            "net_total_contracts": (
                int(total_long or 0) - int(total_short or 0)
                if total_long is not None or total_short is not None
                else None
            ),
        }
        if vol_rows:
            vrow = _participant_row(vol_rows, name)
            if vrow:
                payload["index_fut_vol_net"] = _net_contracts(vrow, "Future Index Long", "Future Index Short")
                payload["index_call_vol_net"] = _net_contracts(
                    vrow, "Option Index Call Long", "Option Index Call Short"
                )
                payload["index_put_vol_net"] = _net_contracts(
                    vrow, "Option Index Put Long", "Option Index Put Short"
                )
        out["participants"][name] = payload

    fii = out["participants"].get("FII") or {}
    if fii.get("index_fut_net_contracts") is not None:
        fut = fii["index_fut_net_contracts"]
        call = fii.get("index_call_oi_net")
        put = fii.get("index_put_oi_net")
        fut_word = "long" if fut > 0 else "short" if fut < 0 else "flat"
        parts = [f"FII net {fut_word} {abs(fut):,} index fut contracts (EOD OI)"]
        if call is not None and put is not None:
            parts.append(f"index options call net {call:+,} / put net {put:+,}")
        out["reads"].append("; ".join(parts) + ".")
    return out


def load_participant_files(trade_date: str) -> Dict[str, Any]:
    """Load raw participant OI/vol CSV summaries for a session date."""
    from nifty.eod.filing import _participant_summary

    day = trade_date[:10]
    folder = RAW_EOD_DIR / day
    ddmmyyyy = datetime.strptime(day, "%Y-%m-%d").strftime("%d%m%Y")
    oi_path = folder / f"fao_participant_oi_{ddmmyyyy}.csv"
    vol_path = folder / f"fao_participant_vol_{ddmmyyyy}.csv"
    oi_rows = _participant_summary(oi_path)
    vol_rows = _participant_summary(vol_path) if vol_path.exists() else []
    summary = participant_positioning_summary(oi_rows, vol_rows or None)
    summary["trade_date"] = day
    summary["source_files"] = {
        "participant_oi": str(oi_path) if oi_path.exists() else None,
        "participant_vol": str(vol_path) if vol_path.exists() else None,
    }
    return summary


def _selftest() -> None:
    import tempfile

    assert positioning_label_short("DISTRIBUTION") == "Distribution"
    assert positioning_label_short("UNKNOWN_LABEL") == "Unknown Label"

    assert previous_trading_day(date(2026, 7, 21)) == date(2026, 7, 20)  # Tuesday -> Monday
    assert previous_trading_day(date(2026, 7, 20)) == date(2026, 7, 17)  # Monday -> Friday (skip weekend)

    assert week_id(date(2026, 7, 21)) == "2026-W30"
    assert month_id(date(2026, 7, 21)) == "2026-07"

    # fii_positioning_label: churn (heavy two-way turnover, tiny net) vs clean accumulation.
    churn_side = {"buy_crores": 10000.0, "sell_crores": 9500.0, "net_crores": 500.0, "turnover_crores": 19500.0, "net_pct_of_turnover": round(500 / 19500 * 100, 1)}
    assert fii_positioning_label(churn_side) == "POSITIONING_CHURN"

    accum_side = {"buy_crores": 2000.0, "sell_crores": 500.0, "net_crores": 1500.0, "turnover_crores": 2500.0, "net_pct_of_turnover": 60.0}
    assert fii_positioning_label(accum_side) == "ACCUMULATION"

    unknown_side = {"buy_crores": None, "sell_crores": None, "net_crores": None, "turnover_crores": None}
    assert fii_positioning_label(unknown_side) == "UNKNOWN"

    read = positioning_read("ACCUMULATION", accum_side, "FII")
    assert "accumulation" in read.lower()

    rows = [
        {"category": "FII/FPI", "date": "21-Jul-2026", "buyValue": "2000", "sellValue": "500", "netValue": "1500"},
        {"category": "DII", "date": "21-Jul-2026", "buyValue": "800", "sellValue": "1200", "netValue": "-400"},
    ]
    entry = fii_dii_entry_from_rows(rows, source="nse_live_api")
    assert entry["fii_positioning"] == "ACCUMULATION"
    assert entry["fii_buy_crores"] == 2000.0
    assert entry["dii_net_crores"] == -400.0
    assert "fii_read" in entry and "dii_read" in entry

    daily = [
        {"trade_date": "2026-07-20", "fii_buy_crores": 2000.0, "fii_sell_crores": 500.0, "fii_net_crores": 1500.0, "dii_net_crores": -400.0, "fii_positioning": "ACCUMULATION"},
        {"trade_date": "2026-07-21", "fii_buy_crores": 1800.0, "fii_sell_crores": 600.0, "fii_net_crores": 1200.0, "dii_net_crores": -300.0, "fii_positioning": "ACCUMULATION"},
    ]
    weekly = aggregate_fii_weekly(daily, max_weeks=4)
    assert len(weekly) == 1
    assert weekly[0]["sessions"] == 2

    # Only 1 week bucket -> no "Last N weeks" prefix (needs >= 2), but the
    # latest week's fii_read and the >=2-day "Recent daily shift" line still fire.
    narrative = fii_transition_narrative(weekly, daily)
    assert "accumulation" in narrative.lower()
    assert "recent daily shift" in narrative.lower()
    assert fii_transition_narrative([], daily) == "Insufficient FII history for weekly transition read."

    # participant_positioning_summary: builds FII/DII/Pro/Client blocks from raw CSV-shaped rows.
    oi_rows = [
        {"participant": "FII", "Future Index Long": "80000", "Future Index Short": "50000",
         "Option Index Call Long": "30000", "Option Index Call Short": "20000",
         "Option Index Put Long": "40000", "Option Index Put Short": "15000",
         "Total Long Contracts": "150000", "Total Short Contracts": "85000"},
    ]
    summary = participant_positioning_summary(oi_rows)
    fii = summary["participants"]["FII"]
    assert fii["index_fut_net_contracts"] == 30000
    assert fii["index_put_oi_net"] == 25000
    assert len(summary["reads"]) == 1

    assert participant_positioning_summary([]) == {"error": "no_participant_oi"}

    # official_participant_bias / compare_pte_vs_official: bullish PTE vs bearish official -> HIGH divergence.
    bias = official_participant_bias({"participants": {"FII": {"index_fut_net_contracts": -80000, "index_call_oi_net": 10000, "index_put_oi_net": 70000}}})
    assert bias["bias"] in {"BEARISH_FUT", "HEDGED_BEARISH"}

    pte_snapshot = {"loaded": True, "timestamp": "2026-07-21 09:15:05", "dominant": {"catalog_id": "breakout_expansion", "name": "Breakout Expansion", "share": 45, "strength": 70}}
    comparison = compare_pte_vs_official({"participants": {"FII": {"index_fut_net_contracts": -80000, "index_call_oi_net": 10000, "index_put_oi_net": 70000}}}, pte_snapshot)
    assert comparison["divergence"] == "HIGH"
    assert comparison["pte"]["bias"] == "BULLISH"

    not_loaded = compare_pte_vs_official({}, {"loaded": False})
    assert "not available" in not_loaded["read"]

    # participant_brief_from_filing: no journal file -> {}, never raises.
    tmp = Path(tempfile.mkdtemp(prefix="nse-official-flows-selftest-"))
    global JOURNAL_DIR
    original_journal_dir = JOURNAL_DIR
    try:
        JOURNAL_DIR = tmp
        assert participant_brief_from_filing("2026-07-21") == {}

        # A filing journal with participant_oi_summary resolves via the fallback path
        # (participant_vol_summary absent -> vol_rows=None, degrades gracefully).
        filing_path = tmp / "nse_eod_filing_2026-07-21.json"
        filing_path.write_text(json.dumps({"participant_oi_summary": oi_rows}), encoding="utf-8")
        brief = participant_brief_from_filing("2026-07-21")
        assert brief.get("participants", {}).get("FII", {}).get("index_fut_net_contracts") == 30000
    finally:
        JOURNAL_DIR = original_journal_dir

    print("[sources.nse_official_flows] selftest OK: positioning labels, FII/DII entries, participant summary, PTE divergence")


if __name__ == "__main__":
    _selftest()
