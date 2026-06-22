#!/usr/bin/env python3
"""Build intraday EOD session report from desk journals (+ NSE filing when available)."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from nifty.core.journal import NiftyJournalStore, ist_now, today_str

from nifty.paths import PROJECT_ROOT as BASE_DIR
JOURNAL_DIR = BASE_DIR / "journal"
LEGACY_SIGNALS = JOURNAL_DIR / "nifty_oi_signals.jsonl"
LIVE_OI_DIR = BASE_DIR / "data" / "live_nifty_oi"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="File EOD session report from desk journals")
    parser.add_argument("--date", default="", help="Trade date YYYY-MM-DD (default: today)")
    parser.add_argument("--skip-nse", action="store_true", help="Do not attempt NSE EOD filing")
    return parser.parse_args()


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            rows.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return rows


def _paper_events(trade_date: str) -> List[Dict[str, Any]]:
    seen: set[str] = set()
    events: List[Dict[str, Any]] = []
    paths = [
        JOURNAL_DIR / f"nifty_paper_trades_{trade_date}.jsonl",
        LEGACY_SIGNALS,
    ]
    for path in paths:
        for row in _load_jsonl(path):
            ts = str(
                row.get("recorded_at")
                or row.get("generated_at")
                or row.get("exit_time")
                or ""
            )
            if trade_date not in ts:
                continue
            fp = "|".join(
                [
                    str(row.get("event") or ""),
                    str(row.get("id") or ""),
                    str(row.get("generated_at") or ""),
                    str(row.get("exit_time") or ""),
                ]
            )
            if fp in seen:
                continue
            seen.add(fp)
            events.append(row)
    return events


def _reconstruct_trades(events: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    by_id: Dict[int, Dict[str, Any]] = {}
    for row in events:
        sid = int(row.get("id") or 0)
        if sid <= 0:
            continue
        event_name = str(row.get("event") or "")
        if event_name == "SIGNAL_GENERATED":
            by_id[sid] = {**by_id.get(sid, {}), **row, "status": "OPEN"}
        elif event_name == "SIGNAL_CLOSED":
            by_id[sid] = {**by_id.get(sid, {}), **row, "status": "CLOSED"}
        elif event_name == "SIGNAL_UPDATE":
            by_id[sid] = {**by_id.get(sid, {}), **row}

    closed = [row for row in by_id.values() if row.get("status") == "CLOSED"]
    open_rows = [row for row in by_id.values() if row.get("status") == "OPEN"]
    closed.sort(key=lambda row: str(row.get("exit_time") or row.get("generated_at") or ""))
    open_rows.sort(key=lambda row: str(row.get("generated_at") or ""))
    if open_rows:
        open_rows = [open_rows[-1]]
    return closed, open_rows


def _spot_from_sqlite(trade_date: str) -> Dict[str, Any]:
    db = LIVE_OI_DIR / f"nifty_oi_ticks_{trade_date}.sqlite"
    if not db.exists():
        return {}
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            """
            SELECT MIN(ltp), MAX(ltp),
                   (SELECT ltp FROM spot_ticks ORDER BY ts DESC LIMIT 1),
                   (SELECT open FROM spot_ticks WHERE open > 0 LIMIT 1),
                   (SELECT prev_close FROM spot_ticks WHERE prev_close > 0 LIMIT 1),
                   COUNT(*)
            FROM spot_ticks
            """
        ).fetchone()
    if not row:
        return {}
    return {
        "day_low": row[0],
        "day_high": row[1],
        "last": row[2],
        "open": row[3],
        "prev_close": row[4],
        "tick_count": row[5],
    }


def _fmt(v: Any, digits: int = 2) -> str:
    try:
        num = float(v)
        return f"{num:,.{digits}f}"
    except (TypeError, ValueError):
        return str(v) if v not in (None, "") else "-"


def _try_nse_filing(trade_date: date) -> Optional[Dict[str, Any]]:
    manifest = BASE_DIR / "data" / "nse_eod" / "raw" / trade_date.isoformat() / "manifest.json"
    filing = JOURNAL_DIR / f"nse_eod_filing_{today_str(trade_date)}.json"
    if filing.exists():
        return _load_json(filing)
    if not manifest.exists():
        return None
    try:
        from nifty.eod.filing import build_eod_filing

        payload = build_eod_filing(trade_date)
        store = NiftyJournalStore()
        store.write_json_snapshot(filing, payload)
        next_oi = payload.get("nifty_oi_maps", {}).get("next_weekly")
        if next_oi and not next_oi.get("error"):
            store.write_json_snapshot(
                JOURNAL_DIR / f"oi_map_eod_{today_str(trade_date)}.json",
                {
                    "trade_date": today_str(trade_date),
                    "recorded_at": ist_now(),
                    "source": "desk_eod_session_report.py",
                    "note": "Post-EOD FO bhavcopy — next weekly series",
                    **next_oi,
                },
            )
        return payload
    except Exception as exc:
        return {"error": str(exc), "trade_date": trade_date.isoformat()}


def _collect_all_trades(trade_date: str) -> Tuple[List[Dict[str, Any]], float]:
    paper_events = _paper_events(trade_date)
    closed_trades, open_trades = _reconstruct_trades(paper_events)
    for row in paper_events:
        if str(row.get("event")) != "SIGNAL_GENERATED" or str(row.get("status")) != "CLOSED":
            continue
        sid = int(row.get("id") or 0)
        if sid <= 0 or any(int(cl.get("id") or 0) == sid for cl in closed_trades):
            continue
        closed_trades.append({**row, "status": "CLOSED"})
    closed_trades.sort(key=lambda row: str(row.get("exit_time") or row.get("generated_at") or ""))
    open_trades = [
        row
        for row in open_trades
        if not any(int(cl.get("id") or 0) == int(row.get("id") or 0) for cl in closed_trades)
    ]
    all_trades = closed_trades + open_trades
    all_trades.sort(key=lambda row: str(row.get("generated_at") or ""))
    closed_net = sum(float(row.get("pnl_net_rupees") or 0) for row in all_trades)
    return all_trades, closed_net


def _pnl_class(value: Any) -> str:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "info"
    if num > 0:
        return "good"
    if num < 0:
        return "bad"
    return "info"


def _parse_ts(value: Any) -> Optional[datetime]:
    """Parse a journal timestamp ('YYYY-MM-DD HH:MM:SS[...]') to a naive datetime."""
    text = str(value or "").strip()
    if len(text) < 19:
        return None
    try:
        return datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _hold_minutes(row: Dict[str, Any]) -> Optional[float]:
    start = _parse_ts(row.get("generated_at"))
    end = _parse_ts(row.get("exit_time"))
    if not start or not end:
        return None
    delta = (end - start).total_seconds() / 60.0
    return round(delta, 1) if delta >= 0 else None


def build_performance_summary(trade_date: date) -> Dict[str, Any]:
    """TradingView-style performance metrics over the day's paper trades.

    Pure read over the journal (reuses _collect_all_trades). Powers both the static
    report HTML and the live /report route — no engine state involved.
    """
    label = trade_date.isoformat()
    all_trades, net_total = _collect_all_trades(label)
    closed = [row for row in all_trades if row.get("exit_time")]
    open_trades = [row for row in all_trades if not row.get("exit_time")]
    closed.sort(key=lambda row: str(row.get("exit_time") or ""))

    def _net(row: Dict[str, Any]) -> float:
        try:
            return float(row.get("pnl_net_rupees") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    wins = [row for row in closed if _net(row) > 0]
    losses = [row for row in closed if _net(row) < 0]
    gross_profit = sum(_net(row) for row in wins)
    gross_loss = abs(sum(_net(row) for row in losses))
    holds = [m for m in (_hold_minutes(row) for row in closed) if m is not None]
    holds_sorted = sorted(holds)

    # Equity curve + max drawdown over closed trades in exit order.
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    curve: List[Dict[str, Any]] = [{"label": "Start", "equity": 0.0, "id": None}]
    for row in closed:
        equity += _net(row)
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        curve.append(
            {
                "label": str(row.get("exit_time") or "")[11:19] or f"#{row.get('id')}",
                "equity": round(equity, 2),
                "id": row.get("id"),
            }
        )

    closed_n = len(closed)
    win_rate = round(len(wins) / closed_n * 100, 1) if closed_n else None
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (
        None if gross_profit == 0 else float("inf")
    )
    median_hold = (
        holds_sorted[len(holds_sorted) // 2]
        if len(holds_sorted) % 2
        else (holds_sorted[len(holds_sorted) // 2 - 1] + holds_sorted[len(holds_sorted) // 2]) / 2
    ) if holds_sorted else None

    return {
        "trade_date": label,
        "trades": all_trades,
        "closed_trades": closed,
        "open_trades": open_trades,
        "total_trades": len(all_trades),
        "closed_count": closed_n,
        "open_count": len(open_trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "net_total": round(net_total, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": profit_factor,
        "avg_win": round(gross_profit / len(wins), 2) if wins else None,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else None,
        "avg_trade": round(net_total / closed_n, 2) if closed_n else None,
        "avg_hold_min": round(sum(holds) / len(holds), 1) if holds else None,
        "median_hold_min": median_hold,
        "max_drawdown": round(max_dd, 2),
        "equity_curve": curve,
    }


def _nse_filing_block(label: str) -> str:
    """FII/DII + India VIX + next-week max-pain card, only if the filing exists."""
    filing = _load_json(JOURNAL_DIR / f"nse_eod_filing_{label}.json")
    if not filing or filing.get("error"):
        return ""
    fii = filing.get("fii_dii") or {}
    vix = filing.get("india_vix") or {}
    official = filing.get("nifty_official") or {}
    next_map = (filing.get("nifty_oi_maps") or {}).get("next_weekly") or {}
    fii_net = fii.get("fii_net_crores")
    dii_net = fii.get("dii_net_crores")
    return f"""
    <h2 class="section">NSE Official EOD</h2>
    <div class="stats stats-3">
      <div class="stat"><div class="label">FII net (Cr)</div><div class="value {_pnl_class(fii_net)}">{_fmt(fii_net, 0)}</div></div>
      <div class="stat"><div class="label">DII net (Cr)</div><div class="value {_pnl_class(dii_net)}">{_fmt(dii_net, 0)}</div></div>
      <div class="stat"><div class="label">India VIX</div><div class="value">{_fmt(vix.get('last'))}</div></div>
      <div class="stat"><div class="label">NIFTY close</div><div class="value">{_fmt(official.get('close'))}</div></div>
      <div class="stat"><div class="label">Next max pain</div><div class="value">{_fmt(next_map.get('max_pain'), 0)}</div></div>
      <div class="stat"><div class="label">PCR (OI)</div><div class="value">{_fmt(next_map.get('pcr_oi'))}</div></div>
    </div>"""


def _grader_comparison_block(trades: List[Dict[str, Any]]) -> str:
    """Normal vs signed grader: agreement + which separated winners from losers better."""
    closed = [t for t in trades if t.get("exit_time") and t.get("pnl_net_rupees") is not None]
    scored = [t for t in closed if t.get("confluence_score") is not None and t.get("confluence_score_signed") is not None]
    if not scored:
        return ""

    def _avg(vals: List[float]) -> Optional[float]:
        return round(sum(vals) / len(vals), 1) if vals else None

    agree = sum(1 for t in scored if t.get("confluence_grade") == t.get("confluence_grade_signed"))
    wins = [t for t in scored if float(t.get("pnl_net_rupees") or 0) > 0]
    losses = [t for t in scored if float(t.get("pnl_net_rupees") or 0) < 0]
    n_win = [float(t["confluence_score"]) for t in wins]
    n_loss = [float(t["confluence_score"]) for t in losses]
    s_win = [float(t["confluence_score_signed"]) for t in wins]
    s_loss = [float(t["confluence_score_signed"]) for t in losses]
    sep_norm = (_avg(n_win) or 0) - (_avg(n_loss) or 0) if (n_win and n_loss) else None
    sep_signed = (_avg(s_win) or 0) - (_avg(s_loss) or 0) if (s_win and s_loss) else None
    better = "—"
    if sep_norm is not None and sep_signed is not None:
        better = "Signed" if abs(sep_signed) > abs(sep_norm) else "Normal" if abs(sep_norm) > abs(sep_signed) else "Tie"

    def _c(v):
        return _fmt(v, 1) if v is not None else "—"

    return f"""
    <h2 class="section">Grader comparison — normal (0..100) vs signed (-100..+100, shadow)</h2>
    <div class="stats">
      <div class="stat"><div class="label">Grade agreement</div><div class="value">{agree}/{len(scored)}</div></div>
      <div class="stat"><div class="label">Avg score · win / loss</div><div class="value"><span class="good">{_c(_avg(n_win))}</span> / <span class="bad">{_c(_avg(n_loss))}</span></div></div>
      <div class="stat"><div class="label">Avg signed · win / loss</div><div class="value"><span class="good">{_c(_avg(s_win))}</span> / <span class="bad">{_c(_avg(s_loss))}</span></div></div>
      <div class="stat"><div class="label">Better separator</div><div class="value">{better}</div></div>
    </div>
    <p class="muted">Separation (win−loss): normal {_c(sep_norm)} · signed {_c(sep_signed)}. Larger = the grader's score discriminated winners from losers better on this day. Signed grader is shadow-only — it does not affect which trades were taken.</p>"""


def render_report_html(trade_date: date, summary: Optional[Dict[str, Any]] = None) -> str:
    """Full TradingView-style performance report (cards + equity curve + trade table).

    Shared by the static file writer and the live /report route. Pass a precomputed
    `summary` (from build_performance_summary) or it will be computed here.
    """
    label = trade_date.isoformat()
    if summary is None:
        summary = build_performance_summary(trade_date)
    brief = _load_json(JOURNAL_DIR / f"desk_brief_{label}.json")
    levels = _load_json(JOURNAL_DIR / f"daily_levels_{label}.json")
    session_close = levels.get("session_close") or {}

    all_trades = summary["trades"]
    net_total = summary["net_total"]
    net_class = _pnl_class(net_total)

    def _fmt_opt(value: Any, digits: int = 2, suffix: str = "") -> str:
        if value is None:
            return "—"
        if value == float("inf"):
            return "∞"
        return f"{_fmt(value, digits)}{suffix}"

    rows_html: List[str] = []
    for idx, row in enumerate(all_trades, start=1):
        pnl_pct = row.get("pnl_pct")
        net = row.get("pnl_net_rupees")
        side = str(row.get("decision") or "")
        side_class = "good" if side == "BUY_CE" else "bad" if side == "BUY_PE" else "info"
        tin = str(row.get("generated_at") or "")[:19]
        tout = str(row.get("exit_time") or "OPEN")[:19]
        hold = _hold_minutes(row)
        rows_html.append(
            f"""<tr>
  <td>{idx}</td>
  <td>{row.get('id')}</td>
  <td>{tin[-8:] if len(tin) >= 8 else tin}</td>
  <td>{tout[-8:] if len(tout) >= 8 and tout != 'OPEN' else tout}</td>
  <td>{_fmt(hold, 0) if hold is not None else '—'}</td>
  <td class="{side_class}">{side}</td>
  <td>{row.get('strike')}</td>
  <td class="mono">{row.get('entry_contract')}</td>
  <td>{_fmt(row.get('entry_price'))}</td>
  <td>{_fmt(row.get('exit_price') or row.get('current_price'))}</td>
  <td class="{_pnl_class(pnl_pct)}">{_fmt(pnl_pct)}%</td>
  <td class="{_pnl_class(net)}">{_fmt(net, 0)}</td>
  <td>{row.get('confluence_grade') or '-'} {row.get('confluence_score') if row.get('confluence_score') is not None else ''}</td>
  <td class="{_pnl_class(row.get('confluence_score_signed'))}">{row.get('confluence_grade_signed') or '-'} {f"{row.get('confluence_score_signed'):+d}" if isinstance(row.get('confluence_score_signed'), int) else ''}</td>
  <td class="muted">{row.get('exit_reason') or ('OPEN' if not row.get('exit_time') else '-')}</td>
</tr>"""
        )

    curve_labels = json.dumps([pt["label"] for pt in summary["equity_curve"]])
    curve_values = json.dumps([pt["equity"] for pt in summary["equity_curve"]])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>NIFTY Desk Report — {label}</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  <style>
    :root {{ --bg:#0f1117; --card:#181b24; --text:#e5e7eb; --muted:#9ca3af; --good:#22c55e; --bad:#ef4444; --warn:#f59e0b; --line:#2a2f3a; --info:#60a5fa; }}
    * {{ box-sizing: border-box; }}
    body {{ font-family: Segoe UI, system-ui, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 24px; line-height: 1.45; }}
    .wrap {{ max-width: 1200px; margin: 0 auto; }}
    h1 {{ font-size: 1.55rem; margin: 0 0 8px; }}
    h2.section {{ font-size: 1.05rem; margin: 22px 0 10px; color: var(--muted); font-weight: 600; }}
    .sub {{ color: var(--muted); font-size: 0.9rem; margin-bottom: 20px; }}
    .stats {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 16px; }}
    .stats-3 {{ grid-template-columns: repeat(6, 1fr); }}
    .stat {{ background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 14px; }}
    .stat .label {{ color: var(--muted); font-size: 0.74rem; text-transform: uppercase; letter-spacing: .04em; }}
    .stat .value {{ font-size: 1.3rem; font-weight: 600; margin-top: 4px; }}
    .card {{ background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 14px; margin-bottom: 16px; overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; min-width: 1040px; }}
    th, td {{ border-bottom: 1px solid var(--line); padding: 8px 6px; text-align: left; vertical-align: top; }}
    th {{ color: var(--muted); font-weight: 600; position: sticky; top: 0; background: var(--card); }}
    .good {{ color: var(--good); font-weight: 600; }}
    .bad {{ color: var(--bad); font-weight: 600; }}
    .info {{ color: var(--info); }}
    .muted {{ color: var(--muted); font-size: 0.78rem; }}
    .mono {{ font-family: Consolas, monospace; font-size: 0.76rem; }}
    .tag {{ display: inline-block; background: #1e293b; border: 1px solid var(--line); padding: 2px 8px; border-radius: 999px; font-size: 0.75rem; margin-right: 6px; }}
    @media (max-width: 900px) {{ .stats, .stats-3 {{ grid-template-columns: 1fr 1fr; }} table {{ min-width: 920px; }} }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>NIFTY Desk Report — {label}</h1>
    <p class="sub">NIFTY F&O paper desk · Filed {ist_now()} IST · Morning bias {brief.get('combined_bias') or '—'} · <span class="tag">report_{label}.html</span></p>

    <div class="stats">
      <div class="stat"><div class="label">Net P&L ₹</div><div class="value {net_class}">{_fmt(net_total, 0)}</div></div>
      <div class="stat"><div class="label">Trades (closed)</div><div class="value">{summary['total_trades']} ({summary['closed_count']})</div></div>
      <div class="stat"><div class="label">Win rate</div><div class="value">{_fmt_opt(summary['win_rate'], 1, '%')}</div></div>
      <div class="stat"><div class="label">Profit factor</div><div class="value">{_fmt_opt(summary['profit_factor'])}</div></div>
    </div>
    <div class="stats">
      <div class="stat"><div class="label">Wins / Losses</div><div class="value">{summary['wins']} / {summary['losses']}</div></div>
      <div class="stat"><div class="label">Avg win / loss ₹</div><div class="value"><span class="good">{_fmt_opt(summary['avg_win'], 0)}</span> / <span class="bad">{_fmt_opt(summary['avg_loss'], 0)}</span></div></div>
      <div class="stat"><div class="label">Max drawdown ₹</div><div class="value bad">{_fmt(summary['max_drawdown'], 0)}</div></div>
      <div class="stat"><div class="label">Avg hold (min)</div><div class="value">{_fmt_opt(summary['avg_hold_min'], 0)}</div></div>
    </div>

    <h2 class="section">Equity curve (cumulative net ₹)</h2>
    <div class="card"><div style="height:300px"><canvas id="equityChart"></canvas></div></div>

    <h2 class="section">Trades</h2>
    <div class="card">
      <table>
        <thead>
          <tr>
            <th>#</th><th>ID</th><th>In</th><th>Out</th><th>Hold</th><th>Side</th><th>Strike</th>
            <th>Contract</th><th>Entry</th><th>Exit</th><th>P&L %</th><th>Net ₹</th>
            <th>Grade</th><th>Signed</th><th>Exit reason</th>
          </tr>
        </thead>
        <tbody>
          {''.join(rows_html) or '<tr><td colspan="15" class="muted">No paper trades recorded for this day.</td></tr>'}
        </tbody>
      </table>
    </div>
{_grader_comparison_block(all_trades)}
{_nse_filing_block(label)}
    <p class="muted">Spot close ~{_fmt(session_close.get('spot'))} · Open {_fmt(session_close.get('open'))} · Range {_fmt(session_close.get('day_low'))} – {_fmt(session_close.get('day_high'))}</p>
    <p class="muted">Source: journal/nifty_paper_trades_{label}.jsonl · journal/nse_eod_filing_{label}.json</p>
  </div>
  <script>
    new Chart(document.getElementById('equityChart').getContext('2d'), {{
      type: 'line',
      data: {{
        labels: {curve_labels},
        datasets: [{{
          label: 'Cumulative net ₹',
          data: {curve_values},
          borderColor: '#60a5fa',
          backgroundColor: 'rgba(96,165,250,0.15)',
          fill: true, tension: 0.2, pointRadius: 3,
        }}]
      }},
      options: {{
        responsive: true, maintainAspectRatio: false,
        plugins: {{ legend: {{ labels: {{ color: '#9ca3af' }} }} }},
        scales: {{
          x: {{ ticks: {{ color: '#9ca3af' }}, grid: {{ color: '#2a2f3a' }} }},
          y: {{ ticks: {{ color: '#9ca3af' }}, grid: {{ color: '#2a2f3a' }} }}
        }}
      }}
    }});
  </script>
</body>
</html>"""


def build_trade_list_html(trade_date: date) -> str:
    """Back-compat wrapper — the trade list is now the full performance report."""
    return render_report_html(trade_date)


def _load_signal_candidates(trade_date: str) -> List[Dict[str, Any]]:
    return _load_jsonl(JOURNAL_DIR / f"nifty_signal_candidates_{trade_date}.jsonl")


def _load_paper_signals(trade_date: str) -> List[Dict[str, Any]]:
    seen: set[str] = set()
    rows: List[Dict[str, Any]] = []
    for path in (JOURNAL_DIR / f"nifty_paper_trades_{trade_date}.jsonl", LEGACY_SIGNALS):
        if not path.exists():
            continue
        for row in _load_jsonl(path):
            if str(row.get("event")) != "SIGNAL_GENERATED":
                continue
            ts = str(row.get("generated_at") or "")
            if trade_date not in ts:
                continue
            fp = f"{row.get('id')}|{ts}|{row.get('status')}"
            if fp in seen:
                continue
            seen.add(fp)
            rows.append(row)
    rows.sort(key=lambda row: str(row.get("generated_at") or ""))
    return rows


def _all_generated_signals(trade_date: str) -> List[Dict[str, Any]]:
    unified: List[Dict[str, Any]] = []
    for row in _load_paper_signals(trade_date):
        unified.append(
            {
                "sort_at": str(row.get("generated_at") or ""),
                "type": "PAPER_EXECUTED",
                "time": str(row.get("generated_at") or "")[:19],
                "signal_key": row.get("signal_key"),
                "decision": row.get("decision"),
                "strike": row.get("strike"),
                "writer_contract": row.get("writer_contract"),
                "entry_contract": row.get("entry_contract"),
                "entry_price": row.get("entry_price"),
                "grade": row.get("confluence_grade") or "-",
                "score": row.get("confluence_score"),
                "max_score": 100,
                "paper": "YES",
                "blockers": "-",
                "status": row.get("status"),
                "exit_reason": row.get("exit_reason") or "-",
                "pnl_pct": row.get("pnl_pct"),
                "thesis": row.get("thesis") or "",
            }
        )
    for row in _load_signal_candidates(trade_date):
        event = str(row.get("event") or "SIGNAL_CANDIDATE")
        if event == "SIGNAL_CANDIDATE_TAKEN":
            row_type = "PAPER_FROM_SCORE"
        else:
            row_type = "CONFLUENCE_SCORE"
        unified.append(
            {
                "sort_at": str(row.get("evaluated_at") or row.get("recorded_at") or ""),
                "type": row_type,
                "time": str(row.get("evaluated_at") or row.get("recorded_at") or "")[:19],
                "signal_key": row.get("signal_key"),
                "decision": row.get("decision"),
                "strike": row.get("strike"),
                "writer_contract": row.get("writer_contract"),
                "entry_contract": row.get("entry_contract"),
                "entry_price": row.get("entry_price"),
                "grade": row.get("grade"),
                "score": row.get("total_score"),
                "max_score": row.get("max_score"),
                "paper": "YES" if row.get("paper_eligible") else "NO",
                "blockers": ", ".join(row.get("blockers") or []) or "-",
                "status": event,
                "exit_reason": "-",
                "pnl_pct": None,
                "thesis": (row.get("source_alert") or {}).get("reason") or "",
            }
        )
    unified.sort(key=lambda row: row.get("sort_at") or "")
    return unified


def build_signal_list_html(trade_date: date) -> str:
    label = trade_date.isoformat()
    brief = _load_json(JOURNAL_DIR / f"desk_brief_{label}.json")
    signals = _all_generated_signals(label)
    paper_count = sum(1 for row in signals if row.get("type") == "PAPER_EXECUTED")
    scored_count = sum(1 for row in signals if row.get("type") == "CONFLUENCE_SCORE")
    paper_yes = sum(1 for row in signals if row.get("paper") == "YES" and row.get("type") == "CONFLUENCE_SCORE")
    unique_keys = len({str(row.get("signal_key")) for row in signals if row.get("signal_key")})

    rows_html: List[str] = []
    for idx, row in enumerate(signals, start=1):
        side = str(row.get("decision") or "")
        side_class = "good" if side == "BUY_CE" else "bad" if side == "BUY_PE" else "info"
        type_class = "warn" if row.get("type") == "PAPER_EXECUTED" else "info"
        score_txt = (
            f"{row.get('score')}/{row.get('max_score')}"
            if row.get("score") is not None
            else "-"
        )
        pnl = row.get("pnl_pct")
        pnl_txt = f"{_fmt(pnl)}%" if pnl is not None else "-"
        pnl_class = _pnl_class(pnl) if pnl is not None else "muted"
        rows_html.append(
            f"""<tr data-type="{row.get('type')}">
  <td>{idx}</td>
  <td class="{type_class}">{row.get('type')}</td>
  <td>{str(row.get('time') or '')[11:19] if len(str(row.get('time') or '')) > 11 else row.get('time')}</td>
  <td class="mono">{row.get('signal_key')}</td>
  <td class="{side_class}">{side}</td>
  <td>{row.get('strike')}</td>
  <td class="mono">{row.get('writer_contract') or '-'}</td>
  <td>{_fmt(row.get('entry_price'))}</td>
  <td class="{ 'good' if row.get('grade') in ('A','B') else 'warn' if row.get('grade')=='C' else 'muted' }">{row.get('grade')}</td>
  <td>{score_txt}</td>
  <td class="{ 'good' if row.get('paper')=='YES' else 'warn' }">{row.get('paper')}</td>
  <td class="muted">{row.get('blockers')}</td>
  <td class="muted">{row.get('status')}</td>
  <td class="{pnl_class}">{pnl_txt}</td>
</tr>"""
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>All Signals Generated — {label}</title>
  <style>
    :root {{ --bg:#0f1117; --card:#181b24; --text:#e5e7eb; --muted:#9ca3af; --good:#22c55e; --bad:#ef4444; --warn:#f59e0b; --line:#2a2f3a; --info:#60a5fa; }}
    * {{ box-sizing: border-box; }}
    body {{ font-family: Segoe UI, system-ui, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 24px; line-height: 1.45; }}
    .wrap {{ max-width: 1400px; margin: 0 auto; }}
    h1 {{ font-size: 1.55rem; margin: 0 0 8px; }}
    .sub {{ color: var(--muted); font-size: 0.9rem; margin-bottom: 16px; }}
    .stats {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-bottom: 16px; }}
    .stat {{ background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 14px; }}
    .stat .label {{ color: var(--muted); font-size: 0.78rem; text-transform: uppercase; letter-spacing: .04em; }}
    .stat .value {{ font-size: 1.25rem; font-weight: 600; margin-top: 4px; }}
    .filters {{ margin-bottom: 12px; }}
    .filters button {{ background: var(--card); color: var(--text); border: 1px solid var(--line); border-radius: 6px; padding: 8px 12px; margin-right: 8px; cursor: pointer; }}
    .filters button.active {{ border-color: var(--info); color: var(--info); }}
    .card {{ background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 14px; overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.8rem; min-width: 1200px; }}
    th, td {{ border-bottom: 1px solid var(--line); padding: 7px 6px; text-align: left; vertical-align: top; }}
    th {{ color: var(--muted); font-weight: 600; position: sticky; top: 0; background: var(--card); }}
    .good {{ color: var(--good); font-weight: 600; }}
    .bad {{ color: var(--bad); font-weight: 600; }}
    .warn {{ color: var(--warn); font-weight: 600; }}
    .info {{ color: var(--info); }}
    .muted {{ color: var(--muted); font-size: 0.76rem; }}
    .mono {{ font-family: Consolas, monospace; font-size: 0.74rem; }}
    .note {{ background: #1a2332; border: 1px solid var(--line); border-radius: 8px; padding: 12px; margin-bottom: 16px; font-size: 0.88rem; }}
    @media (max-width: 900px) {{ .stats {{ grid-template-columns: 1fr 1fr; }} }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>All Signals Generated — {label}</h1>
    <p class="sub">Every engine signal: paper executions + confluence scoreboard · Morning bias {brief.get('combined_bias') or '—'}</p>

    <div class="note">
      <strong>PAPER_EXECUTED</strong> = engine opened paper trade ·
      <strong>CONFLUENCE_SCORE</strong> = writer alert scored on scoreboard (journal review) ·
      Paper column = eligible for paper at that moment (not always executed).
    </div>

    <div class="stats">
      <div class="stat"><div class="label">Total rows</div><div class="value">{len(signals)}</div></div>
      <div class="stat"><div class="label">Paper executed</div><div class="value">{paper_count}</div></div>
      <div class="stat"><div class="label">Confluence scored</div><div class="value">{scored_count}</div></div>
      <div class="stat"><div class="label">Paper-eligible scores</div><div class="value">{paper_yes}</div></div>
      <div class="stat"><div class="label">Unique keys</div><div class="value">{unique_keys}</div></div>
    </div>

    <div class="filters">
      <button class="active" data-filter="ALL">All</button>
      <button data-filter="PAPER_EXECUTED">Paper executed only</button>
      <button data-filter="CONFLUENCE_SCORE">Confluence scored only</button>
    </div>

    <div class="card">
      <table id="signalTable">
        <thead>
          <tr>
            <th>#</th><th>Type</th><th>Time</th><th>Signal key</th><th>Side</th><th>Strike</th>
            <th>Writer</th><th>Entry</th><th>Grade</th><th>Score</th><th>Paper</th>
            <th>Blockers</th><th>Status</th><th>P&L %</th>
          </tr>
        </thead>
        <tbody>
          {''.join(rows_html)}
        </tbody>
      </table>
    </div>
    <p class="muted">Source: journal/nifty_signal_candidates_{label}.jsonl · journal/nifty_oi_signals.jsonl</p>
  </div>
  <script>
    const buttons = document.querySelectorAll('.filters button');
    const rows = document.querySelectorAll('#signalTable tbody tr');
    buttons.forEach(btn => btn.addEventListener('click', () => {{
      buttons.forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      const f = btn.dataset.filter;
      rows.forEach(row => {{
        row.style.display = (f === 'ALL' || row.dataset.type === f) ? '' : 'none';
      }});
    }}));
  </script>
</body>
</html>"""


def build_session_report(trade_date: date, *, skip_nse: bool = False) -> str:
    label = trade_date.isoformat()
    store = NiftyJournalStore()
    summary = store.journal_day_summary(trade_date)

    brief = _load_json(JOURNAL_DIR / f"desk_brief_{label}.json")
    morning = _load_json(JOURNAL_DIR / f"morning_desk_{label}.json")
    oi_map = _load_json(JOURNAL_DIR / f"oi_map_{label}.json")
    levels = _load_json(JOURNAL_DIR / f"daily_levels_{label}.json")
    session_close = levels.get("session_close") or {}
    orb_close = levels.get("orb_close") or {}

    spot_sql = _spot_from_sqlite(label)
    spot = session_close.get("spot") or spot_sql.get("last")
    day_open = session_close.get("open") or orb_close.get("open") or spot_sql.get("open")
    day_high = session_close.get("day_high") or spot_sql.get("day_high")
    day_low = session_close.get("day_low") or spot_sql.get("day_low")
    prev_close = session_close.get("prev_close") or brief.get("prev_close") or spot_sql.get("prev_close")
    orb_high = session_close.get("orb_high") or orb_close.get("orb_high")
    orb_low = session_close.get("orb_low") or orb_close.get("orb_low")

    if day_high is None and spot_sql:
        day_high = spot_sql.get("day_high")
    if day_low is None and spot_sql:
        day_low = spot_sql.get("day_low")

    change_pts = round(float(spot) - float(prev_close), 2) if spot and prev_close else None
    gap_pts = round(float(day_open) - float(prev_close), 2) if day_open and prev_close else None

    all_trades, closed_net = _collect_all_trades(label)
    closed_trades = [row for row in all_trades if row.get("exit_time")]
    open_trades = [row for row in all_trades if not row.get("exit_time")]

    candidates = _load_jsonl(JOURNAL_DIR / f"nifty_signal_candidates_{label}.jsonl")
    grade_counts = Counter(str(row.get("grade") or "?") for row in candidates)
    paper_yes = sum(1 for row in candidates if row.get("paper_eligible"))
    blocker_counts = Counter(
        blocker
        for row in candidates
        for blocker in (row.get("blockers") or [])
    )

    playbook_rows = _load_jsonl(JOURNAL_DIR / f"nifty_playbook_{label}.jsonl")
    phases = [str(row.get("phase") or "") for row in playbook_rows if row.get("event") == "PLAYBOOK_PHASE"]
    phase_first = phases[0] if phases else "-"
    phase_last = phases[-1] if phases else "-"

    nse = None if skip_nse else _try_nse_filing(trade_date)
    nse_pending = nse is None or nse.get("error") or not (nse.get("data_complete") or {}).get("fo_reports")

    lines: List[str] = [
        "# EOD Journal — NIFTY F&O Desk",
        "",
        f"Date: {label}  ",
        f"Instrument: NIFTY weekly options (front expiry **{session_close.get('expiry') or oi_map.get('expiry') or '16-Jun-2026'}**)  ",
        f"Desk status: **Session closed** · filed {ist_now()} IST  ",
        "",
        "## Day Conclusion — Thesis vs Tape",
        "",
    ]

    combined_bias = brief.get("combined_bias") or morning.get("combined_bias") or "UNKNOWN"
    narrative = brief.get("narrative_one_line") or morning.get("narrative_one_line") or ""
    if narrative:
        lines.append(f"**Morning:** {narrative}")
        lines.append("")

    tape_read = "FLAT / mild fade" if change_pts is not None and abs(change_pts) < 30 else "Directional"
    if change_pts is not None and change_pts > 30:
        tape_read = "Gap-up session that faded into close"
    elif change_pts is not None and change_pts < -30:
        tape_read = "Gap-down / weak close"

    change_txt = f"{change_pts:+.1f} pts vs prev {_fmt(prev_close)}" if change_pts is not None else "no cash data"
    lines.extend(
        [
            f"**Verdict:** Morning bias **{combined_bias}**. Cash {tape_read} — "
            f"open {_fmt(day_open)} → close {_fmt(spot)} ({change_txt}). "
            f"Participant OI velocity active; paper book churned on OI conviction exits.",
            "",
            "### What we knew at open",
            "",
            "| Layer | Read |",
            "|-------|------|",
            f"| Combined bias | **{combined_bias}** |",
            f"| Max pain (16-Jun) | **{_fmt(oi_map.get('max_pain'), 0)}** |",
            f"| Prev close | {_fmt(prev_close)} |",
            f"| Cash open gap | **{gap_pts:+.1f} pts** vs prev close |" if gap_pts is not None else "| Cash open gap | — |",
            f"| ORB (restored) | {_fmt(orb_high)} / {_fmt(orb_low)} |",
            "",
            "### What the tape actually did",
            "",
            "| | Value |",
            "|--|-------|",
            f"| Open | {_fmt(day_open)} |",
            f"| Day high | {_fmt(day_high)} |",
            f"| Day low | {_fmt(day_low)} |",
            f"| Close (live mark ~15:25) | {_fmt(spot)} |",
            f"| vs prev close | **{change_pts:+.1f} pts** |" if change_pts is not None else "| vs prev close | — |",
            f"| Playbook phase | {phase_first} → {phase_last} |",
            "",
            "> **Bias is context. Participant action is truth.** Journal every candidate; paper only when confluence + zero blockers.",
            "",
            "---",
            "",
            "## Intraday Engine Stats",
            "",
            "| Metric | Count |",
            "|--------|------:|",
            f"| Writer alerts journaled | {summary['files']['alerts']['lines']} |",
            f"| Signal candidates scored | {summary['files']['signal_candidates']['lines']} |",
            f"| Paper-eligible candidates (any tick) | {paper_yes} |",
            f"| Paper lifecycle events | {summary['files']['paper_trades']['lines']} |",
            f"| Spot ticks archived | {spot_sql.get('tick_count', '—')} |",
            "",
        ]
    )

    if grade_counts:
        lines.append("**Candidate grades:** " + ", ".join(f"{g}: {n}" for g, n in sorted(grade_counts.items())) + "  ")
        lines.append("")
    if blocker_counts:
        top_blockers = blocker_counts.most_common(5)
        lines.append("**Top paper blockers:** " + ", ".join(f"{k} ({v})" for k, v in top_blockers) + "  ")
        lines.append("")

    lines.extend(
        [
            f"**Total paper trades given today: {len(all_trades)}** (all from engine — pre-confluence path before scoreboard gate fully blocked stacking).  ",
            "",
            "## Full Trade List (chronological)",
            "",
            "| # | ID | Time in | Time out | Side | Strike | Contract | Entry | Exit | P&L % | Net ₹ | Exit |",
            "|---|-----|---------|----------|------|--------|----------|-------|------|-------|-------|------|",
        ]
    )
    for idx, row in enumerate(all_trades, start=1):
        tin = str(row.get("generated_at") or "")[-8:]
        tout = str(row.get("exit_time") or "OPEN")[-8:] if row.get("exit_time") else "OPEN"
        lines.append(
            f"| {idx} | {row.get('id')} | {tin} | {tout} | {row.get('decision')} | {row.get('strike')} | "
            f"{row.get('entry_contract')} | {_fmt(row.get('entry_price'))} | {_fmt(row.get('exit_price') or row.get('current_price'))} | "
            f"{_fmt(row.get('pnl_pct'))}% | {_fmt(row.get('pnl_net_rupees'), 0)} | {row.get('exit_reason') or '-'} |"
        )
    lines.extend(["", f"**Session net (all trades, after commission): ₹{_fmt(closed_net, 0)}**", ""])

    lines.extend(["## Paper Book — Closed Trades (summary)", ""])
    if closed_trades:
        lines.extend(
            [
                "| ID | In | Out | Decision | Strike | Entry | Exit | P&L % | Net ₹ | Exit reason |",
                "|----|----|-----|----------|--------|-------|------|-------|-------|-------------|",
            ]
        )
        for row in closed_trades:
            lines.append(
                f"| {row.get('id')} | {str(row.get('generated_at') or '')[-8:]} | "
                f"{str(row.get('exit_time') or '')[-8:]} | {row.get('decision')} | {row.get('strike')} | "
                f"{_fmt(row.get('entry_price'))} | {_fmt(row.get('exit_price'))} | "
                f"{_fmt(row.get('pnl_pct'))}% | {_fmt(row.get('pnl_net_rupees'), 0)} | {row.get('exit_reason')} |"
            )
        lines.extend(["", f"**Closed net (after commission): ₹{_fmt(closed_net, 0)}**", ""])
    else:
        lines.append("*No closed paper trades today.*")
        lines.append("")

    lines.extend(["## Open at Session Close", ""])
    if open_trades:
        lines.extend(
            [
                "| ID | Decision | Strike | Entry | Mark | P&L % | Opened |",
                "|----|----------|--------|-------|------|-------|--------|",
            ]
        )
        for row in open_trades:
            lines.append(
                f"| {row.get('id')} | {row.get('decision')} | {row.get('strike')} | "
                f"{_fmt(row.get('entry_price'))} | {_fmt(row.get('current_price') or row.get('exit_price'))} | "
                f"{_fmt(row.get('pnl_pct'))}% | {row.get('generated_at')} |"
            )
        lines.append("")
    else:
        lines.append("*Flat at close — no open paper positions.*")
        lines.append("")

    lines.extend(["---", "", "## NSE Official EOD", ""])
    if nse_pending:
        lines.extend(
            [
                "**Status: PENDING** — NSE FO bhavcopy / participant files typically land **18:00–19:30 IST**.",
                "",
                "Run after packages arrive:",
                "```powershell",
                f"python nse_eod_downloader.py --date {label} --retry-missing",
                f"python desk_eod_filing.py --date {label}",
                "```",
                "",
            ]
        )
    else:
        fii = nse.get("fii_dii") or {}
        vix = nse.get("india_vix") or {}
        official = nse.get("nifty_official") or {}
        lines.extend(
            [
                f"Source: `journal/nse_eod_filing_{label}.json`",
                "",
                "| | Value |",
                "|--|-------|",
                f"| FII net | **{fii.get('fii_net_crores')} Cr** |",
                f"| DII net | **{fii.get('dii_net_crores')} Cr** |",
                f"| India VIX | **{vix.get('last')}** |",
                f"| NIFTY official close | **{_fmt(official.get('close'))}** |",
                "",
            ]
        )
        next_map = (nse.get("nifty_oi_maps") or {}).get("next_weekly") or {}
        if next_map.get("max_pain"):
            lines.append(
                f"Next weekly max pain ({next_map.get('expiry')}): **{_fmt(next_map.get('max_pain'), 0)}**  "
            )
            lines.append("")

    lines.extend(
        [
            "---",
            "",
            "## Journal Files (today)",
            "",
            "| Archive | Path | Lines |",
            "|---------|------|------:|",
        ]
    )
    for key, meta in summary.get("files", {}).items():
        lines.append(f"| {key.replace('_', ' ').title()} | `{meta.get('path')}` | {meta.get('lines')} |")
    legacy = summary.get("legacy_paper_trades") or {}
    lines.append(f"| Legacy paper log | `{legacy.get('path')}` | {legacy.get('lines')} |")
    lines.extend(
        [
            "",
            f"**EOD report:** `journal/eod_{label}_nifty.md`",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    trade_date = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else date.today()
    write_report_files(trade_date, skip_nse=args.skip_nse)


def write_report_files(trade_date: date, *, skip_nse: bool = False) -> Dict[str, Path]:
    """Render and persist all EOD report artifacts. Shared by session-report + email jobs."""
    label = trade_date.isoformat()
    report = build_session_report(trade_date, skip_nse=skip_nse)
    md_path = JOURNAL_DIR / f"eod_{label}_nifty.md"
    md_path.write_text(report, encoding="utf-8")

    report_html = render_report_html(trade_date)
    report_path = JOURNAL_DIR / f"report_{label}.html"
    report_path.write_text(report_html, encoding="utf-8")
    # Static "latest" copy so nginx can serve a stable URL (report_latest.html).
    latest_path = JOURNAL_DIR / "report_latest.html"
    latest_path.write_text(report_html, encoding="utf-8")
    # Back-compat filename.
    trade_list_path = JOURNAL_DIR / f"trade_list_{label}.html"
    trade_list_path.write_text(report_html, encoding="utf-8")

    signal_html_path = JOURNAL_DIR / f"signal_list_{label}.html"
    signal_html_path.write_text(build_signal_list_html(trade_date), encoding="utf-8")

    paths = {
        "eod_md": md_path,
        "report_html": report_path,
        "report_latest": latest_path,
        "trade_list": trade_list_path,
        "signal_list": signal_html_path,
    }
    for label_key, path in paths.items():
        print(f"{label_key}: {path}")
    return paths


if __name__ == "__main__":
    main()
