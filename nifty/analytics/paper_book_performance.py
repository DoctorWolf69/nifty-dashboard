#!/usr/bin/env python3
"""
TV-style (TradingView-like) paper book performance metrics.

Ported faithfully from quant-desk-engine v4/ATLAS's paper_book_performance.py
(mentor-authored). No logic changed. Only adaptation: JOURNAL_DIR now
resolves via nifty.paths.JOURNAL_DIR instead of a bare __file__-relative
parent.

Used by /api/paper-book-eod-report to render a report only after the book
closes. Computations are journal-only:
  - closed trades: SIGNAL_CLOSED rows from engine journals (the
    nifty_paper_{ENGINE}_{date}.jsonl files this session's
    engine_paper_book.py port writes via nifty.core.journal's
    append_engine_paper)
  - open capital peak (approx): peak concurrent open premium from engine journals

Not yet wired into the live pipeline.
Self-check: python -m nifty.analytics.paper_book_performance
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from nifty.paths import JOURNAL_DIR

ENGINES: Tuple[str, ...] = ("L1", "L2", "EV1", "EV2")

LOT_FALLBACK = 65


def _parse_ts(s: Any) -> Optional[datetime]:
    if s is None:
        return None
    try:
        t = str(s)
        if "T" in t:
            t = t[:19].replace("T", " ")
        else:
            t = t[:19]
        return datetime.strptime(t, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _as_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def _as_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


def list_available_journal_days(journal_dir: Path = JOURNAL_DIR) -> List[str]:
    """Sorted ISO dates that have at least one engine journal file."""
    return [d.isoformat() for d in _list_journal_days(journal_dir)]


def _list_journal_days(journal_dir: Path) -> List[date]:
    days: set[date] = set()
    for eng in ENGINES:
        for p in journal_dir.glob(f"nifty_paper_{eng}_*.jsonl"):
            day_str = p.stem.split("_")[-1]
            try:
                days.add(date.fromisoformat(day_str))
            except Exception:
                continue
    return sorted(days)


def _load_jsonl_lines(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return out


def _peak_open_capital_from_path(path: Path) -> Tuple[float, Optional[datetime]]:
    """
    Peak concurrent open premium approximation for one engine journal.
    """
    open_trades: Dict[int, float] = {}
    events: List[Tuple[datetime, float]] = []

    for row in _load_jsonl_lines(path):
        ev = str(row.get("event") or "")
        tid = _as_int(row.get("id"), 0)
        if tid <= 0:
            continue

        if ev == "SIGNAL_GENERATED":
            ts = _parse_ts(row.get("generated_at") or row.get("recorded_at"))
            if not ts:
                continue
            cap = _as_float(row.get("entry_price")) * _as_int(
                row.get("quantity") or row.get("lot_size") or LOT_FALLBACK
            )
            open_trades[tid] = cap
            events.append((ts, cap))
        elif ev == "SIGNAL_STACK":
            raw_ts = row.get("last_stack_at") or row.get("generated_at") or row.get("recorded_at")
            ts = _parse_ts(raw_ts)
            if not ts:
                continue
            px = _as_float(row.get("entry_price"))
            qty = _as_int(row.get("quantity") or LOT_FALLBACK)
            new_cap = px * qty
            old = open_trades.get(tid, 0.0)
            events.append((ts, new_cap - old))
            open_trades[tid] = new_cap
        elif ev == "SIGNAL_CLOSED":
            ts = _parse_ts(row.get("exit_time") or row.get("generated_at") or row.get("recorded_at"))
            if not ts:
                continue
            cap = open_trades.pop(tid, 0.0)
            if cap:
                events.append((ts, -cap))

    events.sort(key=lambda x: x[0])
    open_cap = 0.0
    peak = 0.0
    peak_ts: Optional[datetime] = None
    for ts, delta in events:
        open_cap += delta
        if open_cap > peak:
            peak = open_cap
            peak_ts = ts
    return peak, peak_ts


def _trade_story(mae_pct: float, mfe_pct: float, final_pct: float) -> str:
    """Plain-English per-trade lifecycle."""
    if mfe_pct > 3 and final_pct < 0:
        return f"Went UP to +{mfe_pct:.1f}% in profit, then closed RED at {final_pct:+.1f}%"
    if mfe_pct > 3 and final_pct >= 0 and final_pct < mfe_pct - 5:
        return f"Went UP to +{mfe_pct:.1f}%, gave back profit, closed +{final_pct:.1f}%"
    if mae_pct < -3 and final_pct > 0:
        return f"Dipped to {mae_pct:.1f}%, recovered, closed GREEN +{final_pct:.1f}%"
    if mfe_pct <= 0 and final_pct < 0:
        return f"Never went green — worst {mae_pct:.1f}%, closed {final_pct:.1f}%"
    if mfe_pct <= 0 and final_pct >= 0:
        return f"Small move — closed +{final_pct:.1f}%"
    return f"In trade: worst {mae_pct:.1f}% to best +{mfe_pct:.1f}%, closed {final_pct:+.1f}%"


def _replay_journal_excursions(journal_path: Path) -> List[Dict[str, Any]]:
    """Replay one engine journal -> per-trade MAE/MFE/final story."""
    if not journal_path.exists():
        return []

    open_state: Dict[int, Dict[str, Any]] = {}
    paths: Dict[int, List[Dict[str, Any]]] = {}
    mae: Dict[int, float] = {}
    mfe: Dict[int, float] = {}
    closed: Dict[int, Dict[str, Any]] = {}

    for row in _load_jsonl_lines(journal_path):
        ev = str(row.get("event") or "")
        tid = _as_int(row.get("id"), 0)
        if tid <= 0:
            continue

        if ev == "SIGNAL_GENERATED":
            entry_ts = str(row.get("generated_at") or row.get("recorded_at") or "")[:19]
            open_state[tid] = {
                "engine": str(row.get("engine") or journal_path.stem.split("_")[2] if "_" in journal_path.stem else ""),
                "decision": row.get("decision"),
                "strike": row.get("strike"),
                "entry_time": entry_ts,
                "entry_price": _as_float(row.get("entry_price")),
            }
            mae[tid] = 0.0
            mfe[tid] = 0.0
            paths[tid] = [{"time": entry_ts, "pnl_pct": 0.0, "event": "OPEN"}]

        elif ev in ("SIGNAL_UPDATE", "SIGNAL_STACK", "SIGNAL_CLOSED") and tid in open_state:
            pct = row.get("pnl_pct")
            if pct is None:
                ep = _as_float(open_state[tid].get("entry_price"))
                px = _as_float(row.get("current_price") or row.get("exit_price") or row.get("last_stack_price"))
                if ep > 0 and px > 0:
                    pct = ((px - ep) / ep) * 100.0
                else:
                    pct = 0.0
            pct_f = _as_float(pct)
            ts = str(
                row.get("recorded_at")
                or row.get("exit_time")
                or row.get("last_stack_at")
                or row.get("generated_at")
                or ""
            )[:19]
            if ts:
                paths.setdefault(tid, []).append(
                    {
                        "time": ts,
                        "pnl_pct": round(pct_f, 2),
                        "event": "STACK" if ev == "SIGNAL_STACK" else ("CLOSE" if ev == "SIGNAL_CLOSED" else "UPDATE"),
                    }
                )
            mae[tid] = min(mae.get(tid, pct_f), pct_f)
            mfe[tid] = max(mfe.get(tid, pct_f), pct_f)
            if ev == "SIGNAL_CLOSED":
                closed[tid] = row

    out: List[Dict[str, Any]] = []
    for tid, close_row in closed.items():
        st = open_state.get(tid, {})
        final = _as_float(close_row.get("pnl_pct"))
        if not final and st.get("entry_price"):
            ep = _as_float(st.get("entry_price"))
            xp = _as_float(close_row.get("exit_price") or close_row.get("current_price"))
            if ep > 0 and xp > 0:
                final = ((xp - ep) / ep) * 100.0
        mae_v = mae.get(tid, final)
        mfe_v = mfe.get(tid, final)
        out.append(
            {
                "trade_id": tid,
                "engine": str(st.get("engine") or close_row.get("engine") or ""),
                "decision": str(st.get("decision") or close_row.get("decision") or ""),
                "strike": st.get("strike") or close_row.get("strike"),
                "entry_time": str(st.get("entry_time") or close_row.get("generated_at") or "")[:19],
                "exit_time": str(close_row.get("exit_time") or close_row.get("recorded_at") or "")[:19],
                "exit_reason": str(close_row.get("exit_reason") or ""),
                "mae_pct": round(mae_v, 2),
                "mfe_pct": round(mfe_v, 2),
                "final_pnl_pct": round(final, 2),
                "pnl_net_rupees": round(_as_float(close_row.get("pnl_net_rupees")), 2),
                "story": _trade_story(mae_v, mfe_v, final),
                "reversal": mfe_v > 5 and final < 0,
                "path": paths.get(tid, []),
                "path_points": len(paths.get(tid, [])),
            }
        )
    out.sort(key=lambda x: x.get("entry_time") or "")
    return out


def replay_day_trade_stories(journal_dir: Path, day: date) -> List[Dict[str, Any]]:
    """All closed trades with MAE/MFE story for one session day (all engines)."""
    rows: List[Dict[str, Any]] = []
    for eng in ENGINES:
        path = journal_dir / f"nifty_paper_{eng}_{day.isoformat()}.jsonl"
        rows.extend(_replay_journal_excursions(path))
    rows.sort(key=lambda x: (x.get("entry_time") or "", x.get("engine") or "", x.get("trade_id") or 0))
    return rows


def get_trade_path(
    journal_dir: Path,
    day: date,
    engine: str,
    trade_id: int,
) -> Dict[str, Any]:
    """Minute-by-minute PnL path for one closed trade (reads engine journal from disk)."""
    eng = str(engine or "").strip().upper()
    if eng not in ENGINES:
        return {"status": "error", "error": f"Unknown engine: {engine}"}
    if trade_id <= 0:
        return {"status": "error", "error": "Invalid trade_id"}

    journal_path = journal_dir / f"nifty_paper_{eng}_{day.isoformat()}.jsonl"
    for story in _replay_journal_excursions(journal_path):
        if int(story.get("trade_id") or 0) == trade_id:
            path = story.get("path") or []
            return {
                "status": "ok",
                "day": day.isoformat(),
                "engine": eng,
                "trade_id": trade_id,
                "path": path,
                "path_points": len(path),
            }
    return {
        "status": "not_found",
        "day": day.isoformat(),
        "engine": eng,
        "trade_id": trade_id,
        "path": [],
        "path_points": 0,
    }


def _compute_max_drawdown(equity: List[float], initial_capital: float) -> Tuple[float, float]:
    """
    Returns: (max_dd_rupees, max_dd_pct)
    """
    if not equity:
        return 0.0, 0.0
    peak = equity[0]
    max_dd = 0.0
    max_dd_pct = 0.0
    for x in equity:
        if x > peak:
            peak = x
        dd = x - peak
        if dd < max_dd:
            max_dd = dd
            max_dd_pct = (dd / initial_capital) * 100.0 if initial_capital > 0 else 0.0
    return max_dd, max_dd_pct


def build_tv_style_performance(
    journal_dir: Path = JOURNAL_DIR,
    *,
    from_day: Optional[date] = None,
    to_day: Optional[date] = None,
) -> Dict[str, Any]:
    """
    Build a TV-like performance summary for paper trades.
    """
    available = _list_journal_days(journal_dir)
    available_labels = [d.isoformat() for d in available]

    end = to_day or (available[-1] if available else date.today())
    start = from_day or (available[0] if available else end)
    daily_mode = start == end
    period_days = [start] if daily_mode else [d for d in available if start <= d <= end]
    if not period_days and daily_mode:
        period_days = [start]

    if not period_days:
        return {
            "status": "no_data",
            "note": "No journal days in requested range.",
            "mode": "daily" if daily_mode else "range",
            "available_days": available_labels,
            "selected_day": start.isoformat() if daily_mode else None,
        }

    # Peak open capital (approx): max across engines, then *1.5 buffer.
    engine_peaks: Dict[str, float] = {}
    for eng in ENGINES:
        mx = 0.0
        for d in period_days:
            p = journal_dir / f"nifty_paper_{eng}_{d.isoformat()}.jsonl"
            pk, _ = _peak_open_capital_from_path(p)
            mx = max(mx, pk)
        engine_peaks[eng] = mx

    account_size_required = max(engine_peaks.values()) * 1.5
    if account_size_required <= 0:
        account_size_required = 1.0

    # Closed trades equity curve (trade-exit granularity).
    closed_events: List[Dict[str, Any]] = []
    for d in period_days:
        for eng in ENGINES:
            p = journal_dir / f"nifty_paper_{eng}_{d.isoformat()}.jsonl"
            for row in _load_jsonl_lines(p):
                if str(row.get("event") or "") != "SIGNAL_CLOSED":
                    continue
                ts = _parse_ts(row.get("exit_time") or row.get("generated_at") or row.get("recorded_at"))
                if not ts:
                    continue
                closed_events.append(
                    {
                        "engine": eng,
                        "trade_date": d.isoformat(),
                        "exit_ts": ts,
                        "exit_hhmm": ts.strftime("%H:%M"),
                        "decision": row.get("decision"),
                        "strike": row.get("strike"),
                        "id": _as_int(row.get("id"), 0),
                        "pnl_net_rupees": _as_float(row.get("pnl_net_rupees")),
                        "pnl_pct": _as_float(row.get("pnl_pct")),
                        "exit_reason": row.get("exit_reason"),
                    }
                )

    closed_events.sort(key=lambda x: x["exit_ts"])
    if not closed_events:
        return {
            "status": "no_closed_trades",
            "note": f"No closed trades for {period_days[0].isoformat()}." if daily_mode else "No SIGNAL_CLOSED events in period.",
            "mode": "daily" if daily_mode else "range",
            "available_days": available_labels,
            "selected_day": period_days[0].isoformat() if daily_mode else None,
            "period_start": period_days[0].isoformat(),
            "period_end": period_days[-1].isoformat(),
        }

    total_pnl = sum(x["pnl_net_rupees"] for x in closed_events)
    trades = len(closed_events)
    winners = [x for x in closed_events if x["pnl_net_rupees"] > 0]
    losers = [x for x in closed_events if x["pnl_net_rupees"] < 0]
    breakevens = [x for x in closed_events if x["pnl_net_rupees"] == 0]

    wins_n = len(winners)
    loss_n = len(losers)
    breakeven_n = len(breakevens)
    win_rate_pct = (wins_n / trades) * 100.0 if trades else 0.0

    gross_wins = sum(x["pnl_net_rupees"] for x in winners)
    gross_losses_abs = abs(sum(x["pnl_net_rupees"] for x in losers))
    if gross_losses_abs > 0:
        profit_factor: float = gross_wins / gross_losses_abs
    else:
        # JSON-safe: avoid Infinity
        profit_factor = 999.0 if gross_wins > 0 else 0.0

    avg_pnl = total_pnl / trades if trades else 0.0
    largest_win = max((x["pnl_net_rupees"] for x in winners), default=0.0)
    largest_loss = min((x["pnl_net_rupees"] for x in losers), default=0.0)

    pnl_pct_values = [x["pnl_pct"] for x in closed_events]
    pnl_pct_avg = sum(pnl_pct_values) / trades if trades else 0.0

    # Equity curve
    equity: List[float] = []
    trade_labels: List[str] = []
    cum = 0.0
    for x in closed_events:
        cum += x["pnl_net_rupees"]
        equity.append(cum)
        trade_labels.append(f"{x['trade_date']} {x['exit_hhmm']}")

    max_dd_rupees, max_dd_pct = _compute_max_drawdown(equity, account_size_required)

    # Daily equity series
    daily_net: Dict[str, float] = defaultdict(float)
    for x in closed_events:
        daily_net[x["trade_date"]] += x["pnl_net_rupees"]
    daily_dates = [d.isoformat() for d in period_days]
    daily_cum: List[float] = []
    daily_net_list: List[float] = []
    cum2 = 0.0
    for dd in daily_dates:
        day_net = daily_net.get(dd, 0.0)
        daily_net_list.append(day_net)
        cum2 += day_net
        daily_cum.append(cum2)

    # Daily drawdown vs running peak (percentage of initial account size).
    peak_c = 0.0
    drawdown_pct_daily: List[float] = []
    for c in daily_cum:
        if c > peak_c:
            peak_c = c
        dday = c - peak_c
        drawdown_pct_daily.append((dday / account_size_required) * 100.0 if account_size_required > 0 else 0.0)

    # Histogram bins
    lo = max(-100.0, min(pnl_pct_values))
    hi = min(100.0, max(pnl_pct_values))
    if hi - lo < 1e-6:
        lo -= 1.0
        hi += 1.0
    bins_n = 16
    step = (hi - lo) / bins_n
    bins: List[Dict[str, Any]] = []
    counts = [0] * bins_n
    for v in pnl_pct_values:
        idx = int((v - lo) / step) if step > 0 else 0
        idx = max(0, min(bins_n - 1, idx))
        counts[idx] += 1
    for i in range(bins_n):
        b0 = lo + i * step
        b1 = b0 + step
        bins.append({"range": [b0, b1], "count": counts[i]})

    # Run-up / drawdown duration (approx event counts)
    run_lengths: List[int] = []
    dd_lengths: List[int] = []
    cur_peak = equity[0]
    peak_idx = 0
    trough_idx = 0
    for i in range(1, len(equity)):
        if equity[i] > cur_peak:
            dd_lengths.append(trough_idx - peak_idx)
            run_lengths.append(peak_idx - trough_idx)
            cur_peak = equity[i]
            peak_idx = i
            trough_idx = i
        elif equity[i] < equity[trough_idx]:
            trough_idx = i
    avg_runup = sum(run_lengths) / len(run_lengths) if run_lengths else 0.0
    avg_drawdown = sum(dd_lengths) / len(dd_lengths) if dd_lengths else 0.0

    # Trade-level curve (intraday when daily_mode).
    trade_dd_pct: List[float] = []
    peak_t = 0.0
    for v in equity:
        if v > peak_t:
            peak_t = v
        dd_t = v - peak_t
        trade_dd_pct.append((dd_t / account_size_required) * 100.0 if account_size_required > 0 else 0.0)

    # Capital efficiency
    ret_pct = (total_pnl / account_size_required) * 100.0 if account_size_required > 0 else 0.0
    if daily_mode:
        cagr_pct = ret_pct
    else:
        years = max(1e-6, (period_days[-1] - period_days[0]).days / 365.25)
        cagr_pct = ret_pct / years if years > 0 else ret_pct

    # Sharpe on daily net returns (rupees normalized by account size).
    daily_returns = [
        (x / account_size_required) for x in daily_net_list
    ] if account_size_required > 0 else []
    r_mean = sum(daily_returns) / len(daily_returns) if daily_returns else 0.0
    r_var = 0.0
    if len(daily_returns) > 1:
        r_var = sum((r - r_mean) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
    r_std = math.sqrt(r_var) if r_var > 0 else 0.0
    if r_std > 0:
        sharpe_ratio = (r_mean / r_std) * math.sqrt(252.0)
    else:
        sharpe_ratio = 0.0

    trade_stories: List[Dict[str, Any]] = []
    if daily_mode:
        trade_stories = replay_day_trade_stories(journal_dir, period_days[0])
    reversal_count = sum(1 for t in trade_stories if t.get("reversal"))

    return {
        "status": "ok",
        "mode": "daily" if daily_mode else "range",
        "selected_day": period_days[0].isoformat() if daily_mode else None,
        "available_days": available_labels,
        "period_start": period_days[0].isoformat(),
        "period_end": period_days[-1].isoformat(),
        "as_of": datetime.now().astimezone().isoformat(timespec="seconds"),
        "account_size_required": account_size_required,
        "capital_efficiency": {
            "cagr_pct": cagr_pct,
            "return_on_initial_capital_pct": ret_pct,
            "margin_calls": 0,
        },
        "key_stats": {
            "total_pnl_rupees": total_pnl,
            "total_trades": trades,
            "win_rate_pct": win_rate_pct,
            "profit_factor": profit_factor,
            "max_drawdown_rupees": max_dd_rupees,
            "max_drawdown_pct": max_dd_pct,
            "largest_win_rupees": largest_win,
            "largest_loss_rupees": largest_loss,
        },
        "equity": {
            "daily_dates": daily_dates,
            "daily_cumulative_pnl": daily_cum,
            "daily_net": daily_net_list,
            "drawdown_pct_daily": drawdown_pct_daily,
            "trade_labels": trade_labels,
            "trade_cumulative_pnl": equity,
            "trade_drawdown_pct": trade_dd_pct,
        },
        "trades_analysis": {
            "avg_pnl_rupees": avg_pnl,
            "avg_pnl_pct": pnl_pct_avg,
            "winners": wins_n,
            "losers": loss_n,
            "breakevens": breakeven_n,
            "returns_histogram": bins,
        },
        "return_details": {
            "expected_payoff_rupees": avg_pnl,
            "sharpe_ratio": sharpe_ratio,
        },
        "runup_drawdown": {
            "avg_runup_events": avg_runup,
            "avg_drawdown_events": avg_drawdown,
        },
        "trade_stories": trade_stories,
        "reversal_count": reversal_count,
    }


def _selftest() -> None:
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="paper-book-perf-selftest-"))
    day = date(2026, 7, 23)

    # No journals at all, default args (daily_mode always falls back to
    # [today]) -> no_closed_trades, never raises.
    empty = build_tv_style_performance(tmp)
    assert empty["status"] == "no_closed_trades"

    # No journals at all, explicit range with no data in it -> no_data.
    empty_range = build_tv_style_performance(tmp, from_day=date(2020, 1, 1), to_day=date(2020, 1, 5))
    assert empty_range["status"] == "no_data"

    l1_path = tmp / f"nifty_paper_L1_{day.isoformat()}.jsonl"
    rows = [
        {"event": "SIGNAL_GENERATED", "id": 1, "generated_at": "2026-07-23 10:00:00",
         "entry_price": 100.0, "quantity": 65, "decision": "BUY_CE", "strike": 23000, "engine": "L1"},
        {"event": "SIGNAL_UPDATE", "id": 1, "recorded_at": "2026-07-23 10:15:00", "pnl_pct": 15.0, "current_price": 115.0},
        {"event": "SIGNAL_CLOSED", "id": 1, "exit_time": "2026-07-23 10:30:00", "exit_reason": "TARGET_HIT",
         "pnl_pct": 30.0, "pnl_net_rupees": 1800.0, "exit_price": 130.0, "decision": "BUY_CE", "strike": 23000},
        {"event": "SIGNAL_GENERATED", "id": 2, "generated_at": "2026-07-23 11:00:00",
         "entry_price": 80.0, "quantity": 65, "decision": "BUY_PE", "strike": 22900, "engine": "L1"},
        {"event": "SIGNAL_CLOSED", "id": 2, "exit_time": "2026-07-23 11:20:00", "exit_reason": "STOP_HIT",
         "pnl_pct": -30.0, "pnl_net_rupees": -1600.0, "exit_price": 56.0, "decision": "BUY_PE", "strike": 22900},
    ]
    with l1_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")

    days = list_available_journal_days(tmp)
    assert days == ["2026-07-23"]

    peak, _ = _peak_open_capital_from_path(l1_path)
    assert peak == 100.0 * 65  # first trade's entry capital, before the second opens

    stories = replay_day_trade_stories(tmp, day)
    assert len(stories) == 2
    winner = next(s for s in stories if s["trade_id"] == 1)
    assert winner["mfe_pct"] == 30.0 and winner["final_pnl_pct"] == 30.0
    loser = next(s for s in stories if s["trade_id"] == 2)
    assert loser["final_pnl_pct"] == -30.0
    assert "closed" in loser["story"].lower() or "worst" in loser["story"].lower()

    path_info = get_trade_path(tmp, day, "L1", 1)
    assert path_info["status"] == "ok"
    assert path_info["path_points"] >= 2

    missing = get_trade_path(tmp, day, "L1", 999)
    assert missing["status"] == "not_found"
    bad_engine = get_trade_path(tmp, day, "NOPE", 1)
    assert bad_engine["status"] == "error"

    dd_rupees, dd_pct = _compute_max_drawdown([1000.0, 1500.0, 800.0, 1200.0], initial_capital=1000.0)
    assert dd_rupees == -700.0
    assert dd_pct == -70.0

    perf = build_tv_style_performance(tmp, from_day=day, to_day=day)
    assert perf["status"] == "ok"
    assert perf["key_stats"]["total_trades"] == 2
    assert perf["key_stats"]["total_pnl_rupees"] == 200.0  # 1800 - 1600
    assert perf["trades_analysis"]["winners"] == 1
    assert perf["trades_analysis"]["losers"] == 1
    assert perf["mode"] == "daily"
    assert len(perf["equity"]["trade_cumulative_pnl"]) == 2
    assert perf["reversal_count"] >= 0

    # A day with journal files but zero SIGNAL_CLOSED rows -> no_closed_trades, not an error.
    other_day = date(2026, 7, 24)
    (tmp / f"nifty_paper_L1_{other_day.isoformat()}.jsonl").write_text(
        json.dumps({"event": "SIGNAL_GENERATED", "id": 3, "generated_at": "2026-07-24 10:00:00", "entry_price": 50.0}) + "\n",
        encoding="utf-8",
    )
    empty_day = build_tv_style_performance(tmp, from_day=other_day, to_day=other_day)
    assert empty_day["status"] == "no_closed_trades"

    print("[analytics.paper_book_performance] selftest OK: journal replay, MAE/MFE stories, TV-style stats")


if __name__ == "__main__":
    _selftest()
