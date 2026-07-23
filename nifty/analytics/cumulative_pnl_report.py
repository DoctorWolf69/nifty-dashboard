#!/usr/bin/env python3
"""Cumulative paper-trade PnL report from journal/nifty_paper_trades_*.jsonl.

Ported faithfully from quant-desk-engine v4/ATLAS's cumulative_pnl_report.py
(mentor-authored). No logic changed. Adaptations:
- `from nifty_commission import net_pnl_rupees` -> nifty.core.commission
  (already ported/updated this session, same signature incl. decision=).
- `from nifty_journal_store import ist_now` -> nifty.core.journal.
- `from nifty_relationships_lab import load_paper_trades_from_journal,
  JOURNAL_DIR` -> load_paper_trades_from_journal actually lives in
  nifty.analytics.journal_reader (relationships_lab.py just re-imports
  it); JOURNAL_DIR -> nifty.paths.

Genuinely new capability: a markdown cumulative-PnL report over the
system's primary (L1) paper trade journal - daily win rate/net, best/worst
trade, by-decision and by-exit-reason breakdowns, open-position MTM.
Distinct from nifty.analytics.paper_book_performance (equity curve/Sharpe/
drawdown/MAE-MFE analysis) and from cumulative_pnl_report's sibling
consolidated_pnl_report.py (the four-engine L1/L2/EV1/EV2 book) - this one
is specifically the single-engine markdown summary report the mentor also
kept as a separate, simpler artifact.

Not yet wired into the live pipeline (CLI-only, like the source).
Self-check: python -m nifty.analytics.cumulative_pnl_report
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from nifty.analytics.journal_reader import load_paper_trades_from_journal
from nifty.core.commission import net_pnl_rupees
from nifty.core.journal import ist_now
from nifty.paths import JOURNAL_DIR


def _trade_net_rupees(trade: Dict[str, Any]) -> float:
    net = trade.get("pnl_net_rupees")
    if net is not None:
        return float(net)
    entry = trade.get("entry_price")
    exit_ = trade.get("exit_price")
    lot = int(trade.get("lot_size") or 65)
    if entry is not None and exit_ is not None:
        return float(
            net_pnl_rupees(
                float(entry),
                float(exit_),
                lot,
                decision=str(trade.get("decision") or ""),
            )["net_rupees"]
        )
    return 0.0


def _mtm_net_rupees(trade: Dict[str, Any]) -> float:
    entry = trade.get("entry_price")
    current = trade.get("current_price") or entry
    lot = int(trade.get("lot_size") or 65)
    if entry is None or current is None:
        return 0.0
    return float(
        net_pnl_rupees(
            float(entry),
            float(current),
            lot,
            decision=str(trade.get("decision") or ""),
        )["net_rupees"]
    )


def _fmt(v: float, d: int = 2) -> str:
    return f"{v:,.{d}f}"


def _hhmm(ts: Optional[str]) -> str:
    if not ts:
        return "—"
    try:
        return datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S").strftime("%H:%M")
    except ValueError:
        return str(ts)[11:16] or "—"


def collect_paper_trade_days(journal_dir: Path) -> List[str]:
    return sorted(
        p.stem.replace("nifty_paper_trades_", "")
        for p in journal_dir.glob("nifty_paper_trades_*.jsonl")
    )


def build_report(journal_dir: Path = JOURNAL_DIR) -> Dict[str, Any]:
    days = collect_paper_trade_days(journal_dir)
    daily: List[Dict[str, Any]] = []
    closed_trades: List[Dict[str, Any]] = []
    open_trades: List[Dict[str, Any]] = []
    cumulative = 0.0

    for day_label in days:
        day = date.fromisoformat(day_label)
        trades = load_paper_trades_from_journal(journal_dir, day)
        day_net = 0.0
        day_closed = 0
        day_wins = 0
        for trade in trades:
            trade = dict(trade)
            trade["trade_date"] = day_label
            status = str(trade.get("status") or "")
            if status == "CLOSED":
                net = _trade_net_rupees(trade)
                trade["net_rupees"] = net
                closed_trades.append(trade)
                day_closed += 1
                day_net += net
                if net > 0:
                    day_wins += 1
            elif status == "OPEN":
                trade["mtm_net_rupees"] = _mtm_net_rupees(trade)
                open_trades.append(trade)

        cumulative += day_net
        daily.append(
            {
                "date": day_label,
                "closed": day_closed,
                "wins": day_wins,
                "win_rate_pct": round(100 * day_wins / day_closed, 1) if day_closed else 0.0,
                "net_rupees": round(day_net, 2),
                "cumulative_rupees": round(cumulative, 2),
            }
        )

    wins = sum(1 for t in closed_trades if t["net_rupees"] > 0)
    total_closed = len(closed_trades)
    by_decision: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"n": 0, "wins": 0, "net": 0.0})
    by_exit: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"n": 0, "net": 0.0})

    for t in closed_trades:
        dec = str(t.get("decision") or "—")
        reason = str(t.get("exit_reason") or "UNKNOWN")
        net = t["net_rupees"]
        by_decision[dec]["n"] += 1
        by_decision[dec]["net"] += net
        if net > 0:
            by_decision[dec]["wins"] += 1
        by_exit[reason]["n"] += 1
        by_exit[reason]["net"] += net

    open_mtm = sum(t.get("mtm_net_rupees") or 0 for t in open_trades)
    best = max(closed_trades, key=lambda x: x["net_rupees"], default=None)
    worst = min(closed_trades, key=lambda x: x["net_rupees"], default=None)

    return {
        "generated_at": ist_now(),
        "period_start": days[0] if days else None,
        "period_end": days[-1] if days else None,
        "trading_days": len(days),
        "closed_trades": total_closed,
        "open_trades": len(open_trades),
        "wins": wins,
        "losses": total_closed - wins,
        "win_rate_pct": round(100 * wins / total_closed, 1) if total_closed else 0.0,
        "realized_net_rupees": round(cumulative, 2),
        "open_mtm_net_rupees": round(open_mtm, 2),
        "mark_to_market_total_rupees": round(cumulative + open_mtm, 2),
        "daily": daily,
        "by_decision": dict(by_decision),
        "by_exit_reason": dict(by_exit),
        "best_trade": best,
        "worst_trade": worst,
        "open_trades_detail": open_trades,
        "recent_closed": sorted(
            closed_trades,
            key=lambda x: (x.get("trade_date", ""), x.get("exit_time") or ""),
            reverse=True,
        )[:15],
    }


def render_markdown(data: Dict[str, Any]) -> str:
    lines = [
        f"# Cumulative Paper PnL Report",
        "",
        f"**Generated:** {data['generated_at']}  ",
        f"**Period:** {data['period_start']} → {data['period_end']} ({data['trading_days']} journal days)  ",
        f"**Source:** `journal/nifty_paper_trades_*.jsonl` (system paper book, net of round-trip commission)",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Closed trades | {data['closed_trades']} |",
        f"| Win / loss | {data['wins']} / {data['losses']} ({data['win_rate_pct']}% WR) |",
        f"| **Realized net PnL** | **₹ {_fmt(data['realized_net_rupees'])}** |",
        f"| Open positions | {data['open_trades']} |",
        f"| Open MTM (net) | ₹ {_fmt(data['open_mtm_net_rupees'])} |",
        f"| **Realized + open MTM** | **₹ {_fmt(data['mark_to_market_total_rupees'])}** |",
        "",
    ]

    if data.get("best_trade"):
        b = data["best_trade"]
        lines += [
            f"| Best closed trade | {b.get('trade_date')} id={b.get('id')} {b.get('decision')} {b.get('strike')} → ₹ {_fmt(b['net_rupees'])} ({b.get('exit_reason')}) |",
        ]
    if data.get("worst_trade"):
        w = data["worst_trade"]
        lines += [
            f"| Worst closed trade | {w.get('trade_date')} id={w.get('id')} {w.get('decision')} {w.get('strike')} → ₹ {_fmt(w['net_rupees'])} ({w.get('exit_reason')}) |",
        ]

    lines += ["", "## Daily cumulative", "", "| Date | Closed | W/L | Day net ₹ | Cumulative ₹ |", "|------|--------|-----|-----------|----------------|"]
    for row in data["daily"]:
        wl = f"{row['wins']}/{row['closed'] - row['wins']}" if row["closed"] else "—"
        lines.append(
            f"| {row['date']} | {row['closed']} | {wl} | {_fmt(row['net_rupees'])} | **{_fmt(row['cumulative_rupees'])}** |"
        )

    lines += ["", "## By decision (closed)", "", "| Decision | Trades | Wins | Net ₹ |", "|----------|--------|------|-------|"]
    for dec, stats in sorted(data["by_decision"].items()):
        lines.append(
            f"| {dec} | {stats['n']} | {stats['wins']} | {_fmt(stats['net'])} |"
        )

    lines += ["", "## By exit reason (closed)", "", "| Exit reason | Trades | Net ₹ |", "|-------------|--------|-------|"]
    for reason, stats in sorted(data["by_exit_reason"].items(), key=lambda x: -abs(x[1]["net"])):
        lines.append(f"| {reason} | {stats['n']} | {_fmt(stats['net'])} |")

    if data["open_trades_detail"]:
        lines += ["", "## Open positions", "", "| Date | ID | Side | Strike | Entry | Mark | MTM net ₹ | PnL % |", "|------|----|------|--------|-------|------|-----------|-------|"]
        today = date.today()
        for t in data["open_trades_detail"]:
            td = date.fromisoformat(t["trade_date"])
            stale = (today - td).days >= 5
            flag = " ⚠️ stale" if stale else ""
            lines.append(
                f"| {t['trade_date']}{flag} | {t.get('id')} | {t.get('decision')} | {t.get('strike')} | "
                f"{t.get('entry_price')} | {t.get('current_price')} | {_fmt(t.get('mtm_net_rupees') or 0)} | {t.get('pnl_pct') or '—'} |"
            )

    lines += ["", "## Recent closed (last 15)", "", "| Date | Time | ID | Decision | Strike | Net ₹ | Exit |", "|------|------|----|----------|--------|-------|------|"]
    for t in data["recent_closed"]:
        lines.append(
            f"| {t.get('trade_date')} | {_hhmm(t.get('exit_time'))} | {t.get('id')} | {t.get('decision')} | "
            f"{t.get('strike')} | {_fmt(t['net_rupees'])} | {t.get('exit_reason')} |"
        )

    lines += [
        "",
        "---",
        "*Paper book only — not live Kite execution. Net figures use desk commission model (`nifty.core.commission.net_pnl_rupees`).*",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Cumulative paper-trade PnL report")
    parser.add_argument("--print", action="store_true", help="Print markdown to stdout")
    parser.add_argument(
        "--out",
        default="",
        help="Output markdown path (default: journal/cumulative_pnl_report.md)",
    )
    args = parser.parse_args()

    data = build_report()
    md = render_markdown(data)
    out_path = Path(args.out) if args.out else JOURNAL_DIR / "cumulative_pnl_report.md"
    out_path.write_text(md, encoding="utf-8")
    print(f"Cumulative PnL report written: {out_path}")
    print(
        f"  Realized net: INR {data['realized_net_rupees']:,.2f}  |  "
        f"Closed: {data['closed_trades']}  |  WR: {data['win_rate_pct']}%"
    )
    if args.print:
        print()
        print(md)


def _selftest() -> None:
    import json
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="cumulative-pnl-selftest-"))

    assert collect_paper_trade_days(tmp) == []
    empty_report = build_report(tmp)
    assert empty_report["trading_days"] == 0
    assert empty_report["realized_net_rupees"] == 0.0

    day1 = tmp / "nifty_paper_trades_2026-07-20.jsonl"
    day1.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {
                    "event": "SIGNAL_GENERATED",
                    "signal_key": "sig1",
                    "id": 1,
                    "decision": "BUY_CE",
                    "strike": 25000,
                    "entry_price": 100.0,
                    "lot_size": 65,
                    "generated_at": "2026-07-20 09:30:00",
                },
                {
                    "event": "SIGNAL_CLOSED",
                    "signal_key": "sig1",
                    "id": 1,
                    "decision": "BUY_CE",
                    "strike": 25000,
                    "entry_price": 100.0,
                    "exit_price": 150.0,
                    "lot_size": 65,
                    "exit_reason": "TARGET",
                    "exit_time": "2026-07-20 10:15:00",
                },
                {
                    "event": "SIGNAL_GENERATED",
                    "signal_key": "sig2",
                    "id": 2,
                    "decision": "BUY_PE",
                    "strike": 24900,
                    "entry_price": 80.0,
                    "lot_size": 65,
                    "generated_at": "2026-07-20 09:45:00",
                },
                {
                    "event": "SIGNAL_CLOSED",
                    "signal_key": "sig2",
                    "id": 2,
                    "decision": "BUY_PE",
                    "strike": 24900,
                    "entry_price": 80.0,
                    "exit_price": 60.0,
                    "lot_size": 65,
                    "exit_reason": "STOP",
                    "exit_time": "2026-07-20 11:30:00",
                },
                {
                    "event": "SIGNAL_GENERATED",
                    "signal_key": "sig3",
                    "id": 3,
                    "decision": "BUY_CE",
                    "strike": 25100,
                    "entry_price": 90.0,
                    "current_price": 95.0,
                    "lot_size": 65,
                    "generated_at": "2026-07-20 12:00:00",
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert collect_paper_trade_days(tmp) == ["2026-07-20"]

    report = build_report(tmp)
    assert report["trading_days"] == 1
    assert report["closed_trades"] == 2
    assert report["open_trades"] == 1
    assert report["wins"] == 1 and report["losses"] == 1
    assert report["realized_net_rupees"] != 0.0
    assert report["best_trade"]["id"] == 1
    assert report["worst_trade"]["id"] == 2
    assert "BUY_CE" in report["by_decision"]
    assert "TARGET" in report["by_exit_reason"]
    assert len(report["open_trades_detail"]) == 1
    assert report["open_trades_detail"][0]["mtm_net_rupees"] != 0.0

    md = render_markdown(report)
    assert "Cumulative Paper PnL Report" in md
    assert "Best closed trade" in md
    assert "Open positions" in md

    print("[analytics.cumulative_pnl_report] selftest OK: day collection, report build, markdown render")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        main()
    else:
        _selftest()
