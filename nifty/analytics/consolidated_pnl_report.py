#!/usr/bin/env python3
"""Consolidated four-engine paper P&L report (L1 / L2 / EV1 / EV2).

Sources:
  - journal/nifty_paper_{ENGINE}_{date}.jsonl  (per-engine ledgers)
  - journal/nifty_paper_trades_{date}.jsonl      (L1 primary fallback)

Ported faithfully from quant-desk-engine v4/ATLAS's consolidated_pnl_report.py
(mentor-authored). No logic changed. Adaptations:
- `from nifty_commission import CommissionConfig` -> nifty.core.commission.
- `from nifty_journal_store import JOURNAL_DIR, ist_now` ->
  nifty.paths.JOURNAL_DIR + nifty.core.journal.ist_now.
- `from nifty_relationships_lab import load_paper_trades_from_journal` ->
  nifty.analytics.journal_reader (relationships_lab.py just re-imports it
  from there; same function).
- `from nifty_trade_book import normalize_paper_trade_row` ->
  nifty.analytics.trade_book (already ported this session, same name).
- journal/nifty_paper_{ENGINE}_*.jsonl naming confirmed to already match
  nifty.core.journal.NiftyJournalStore.engine_paper_path's convention
  (added this session for engine_paper_book.py's L1/L2/EV1/EV2 ledgers).

Genuinely new capability: rolls up all four parallel paper books
(L1 legacy v1, L2 legacy v2 shadow, EV1/EV2 EV-model shadows) into one
desk-wide P&L report - per-engine and combined win/loss, net, open MTM,
best/worst trade, by-exit-reason/by-decision breakdowns. Distinct from
cumulative_pnl_report.py (L1-only, simpler) which the mentor kept as a
separate, smaller sibling report.

Not yet wired into the live pipeline (CLI-only, like the source; also
the L2/EV1/EV2 books it reads are still empty in this environment since
those engines' upstream scoring inputs aren't wired yet, per
engine_paper_book.py's own documented degrade-to-False contract).
Self-check: python -m nifty.analytics.consolidated_pnl_report
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from nifty.analytics.journal_reader import load_paper_trades_from_journal
from nifty.analytics.trade_book import normalize_paper_trade_row
from nifty.core.commission import CommissionConfig
from nifty.core.journal import ist_now
from nifty.paths import JOURNAL_DIR

ENGINES: Tuple[str, ...] = ("L1", "L2", "EV1", "EV2")


def _parse_day(value: str) -> date:
    return date.fromisoformat(value)


def _iter_days(from_day: date, to_day: date) -> List[date]:
    out: List[date] = []
    cur = from_day
    while cur <= to_day:
        out.append(cur)
        cur += timedelta(days=1)
    return out


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


def _restore_engine_book(path: Path, engine: str) -> Dict[int, Dict[str, Any]]:
    """Last lifecycle state per trade id from an engine journal."""
    states: Dict[int, Dict[str, Any]] = {}
    for row in _load_jsonl(path):
        sid = int(row.get("id") or 0)
        if sid <= 0:
            continue
        event = str(row.get("event") or "")
        if event == "SIGNAL_CLOSED" or row.get("status") == "CLOSED":
            states[sid] = {**states.get(sid, {}), **row, "status": "CLOSED"}
        elif event in {"SIGNAL_GENERATED", "SIGNAL_UPDATE", "SIGNAL_STACK"}:
            states[sid] = {**states.get(sid, {}), **row}
            if states[sid].get("status") != "CLOSED":
                states[sid]["status"] = "OPEN"
    for sig in states.values():
        sig.setdefault("engine", engine)
    return states


def load_engine_trades(
    journal_dir: Path,
    day: date,
    *,
    cfg: Optional[CommissionConfig] = None,
) -> List[Dict[str, Any]]:
    """All trades (open + closed) for one day across four engine books."""
    config = cfg or CommissionConfig.from_env()
    day_label = day.isoformat()
    rows: List[Dict[str, Any]] = []

    for engine in ENGINES:
        path = journal_dir / f"nifty_paper_{engine}_{day_label}.jsonl"
        for sig in _restore_engine_book(path, engine).values():
            sig["trade_date"] = day_label
            rows.append(normalize_paper_trade_row(sig, config))

    # L1 primary fallback when engine journal absent
    has_l1 = any(str(r.get("engine") or "").upper() == "L1" for r in rows)
    primary_path = journal_dir / f"nifty_paper_trades_{day_label}.jsonl"
    if not has_l1 and primary_path.exists():
        for sig in load_paper_trades_from_journal(journal_dir, day):
            payload = normalize_paper_trade_row(dict(sig), config)
            payload.setdefault("engine", "L1")
            payload.setdefault("book_role", "primary")
            payload["trade_date"] = day_label
            rows.append(payload)

    return rows


def _net(row: Dict[str, Any]) -> float:
    return float(row.get("net_pnl") or row.get("pnl_net_rupees") or 0)


def _gross(row: Dict[str, Any]) -> float:
    return float(row.get("gross_pnl") or row.get("pnl_gross_rupees") or 0)


def _charges(row: Dict[str, Any]) -> float:
    return float(row.get("charges") or row.get("pnl_commission_rupees") or 0)


def _fmt(v: float, d: int = 2) -> str:
    sign = "+" if v > 0 else ""
    return f"{sign}{v:,.{d}f}"


def _hhmm(ts: Optional[str]) -> str:
    if not ts:
        return "—"
    try:
        return datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S").strftime("%H:%M")
    except ValueError:
        return str(ts)[11:16] or "—"


def _summarize_closed(closed: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    net = sum(_net(r) for r in closed)
    gross = sum(_gross(r) for r in closed)
    comm = sum(_charges(r) for r in closed)
    wins = sum(1 for r in closed if _net(r) > 0)
    losses = sum(1 for r in closed if _net(r) < 0)
    flat = len(closed) - wins - losses
    lots = sum(int(r.get("lots") or 0) for r in closed)
    return {
        "trades": len(closed),
        "wins": wins,
        "losses": losses,
        "flat": flat,
        "win_rate_pct": round(100 * wins / len(closed), 1) if closed else 0.0,
        "lots": lots,
        "gross_rupees": round(gross, 2),
        "commission_rupees": round(comm, 2),
        "net_rupees": round(net, 2),
    }


def build_report(
    journal_dir: Path = JOURNAL_DIR,
    *,
    from_day: Optional[date] = None,
    to_day: Optional[date] = None,
    cfg: Optional[CommissionConfig] = None,
) -> Dict[str, Any]:
    config = cfg or CommissionConfig.from_env()
    end = to_day or date.today()
    start = from_day or end
    days = _iter_days(start, end)

    all_rows: List[Dict[str, Any]] = []
    daily: List[Dict[str, Any]] = []
    cumulative = 0.0

    for day in days:
        day_rows = load_engine_trades(journal_dir, day, cfg=config)
        all_rows.extend(day_rows)
        closed = [r for r in day_rows if str(r.get("status") or "") == "CLOSED"]
        day_sum = _summarize_closed(closed)
        cumulative += day_sum["net_rupees"]
        daily.append(
            {
                "date": day.isoformat(),
                **day_sum,
                "cumulative_rupees": round(cumulative, 2),
                "has_data": bool(day_rows),
            }
        )

    closed_all = [r for r in all_rows if str(r.get("status") or "") == "CLOSED"]
    open_all = [r for r in all_rows if str(r.get("status") or "") == "OPEN"]

    # Dedupe for stats — one row per (engine, id, date) prefer CLOSED
    deduped: Dict[Tuple[str, int, str], Dict[str, Any]] = {}
    for row in all_rows:
        eng = str(row.get("engine") or "").upper()
        tid = int(row.get("id") or 0)
        day_label = str(row.get("trade_date") or "")
        if tid > 0:
            key: Tuple[str, int, str] = (eng, tid, day_label)
        else:
            key = (eng, hash(str(row.get("signal_key") or "")), day_label)
        prev = deduped.get(key)
        if prev is None or (
            str(row.get("status") or "") == "CLOSED" and str(prev.get("status") or "") != "CLOSED"
        ):
            deduped[key] = row

    unique_rows = list(deduped.values())
    closed = [r for r in unique_rows if str(r.get("status") or "") == "CLOSED"]
    opens = [r for r in unique_rows if str(r.get("status") or "") == "OPEN"]

    by_engine: Dict[str, Dict[str, Any]] = {}
    for eng in ENGINES:
        eng_rows = [r for r in unique_rows if str(r.get("engine") or "").upper() == eng]
        eng_closed = [r for r in eng_rows if str(r.get("status") or "") == "CLOSED"]
        eng_opens = [r for r in eng_rows if str(r.get("status") or "") == "OPEN"]
        closed_net = sum(_net(r) for r in eng_closed)
        open_mtm = sum(_net(r) for r in eng_opens)
        wins = sum(1 for r in eng_closed if _net(r) > 0)
        losses = sum(1 for r in eng_closed if _net(r) <= 0)
        by_engine[eng] = {
            "engine": eng,
            "open": len(eng_opens),
            "closed": len(eng_closed),
            "wins": wins,
            "losses": losses,
            "closed_net_pnl_rupees": round(closed_net, 2),
            "open_mtm_rupees": round(open_mtm, 2),
            "net_pnl_rupees": round(closed_net + open_mtm, 2),
        }

    totals = _summarize_closed(closed)
    open_mtm = sum(_net(r) for r in opens)

    by_exit: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"n": 0, "net": 0.0})
    by_decision: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"n": 0, "wins": 0, "net": 0.0})
    for row in closed:
        reason = str(row.get("exit_reason") or "UNKNOWN")
        dec = str(row.get("decision") or row.get("side") or "—")
        net = _net(row)
        by_exit[reason]["n"] += 1
        by_exit[reason]["net"] += net
        by_decision[dec]["n"] += 1
        by_decision[dec]["net"] += net
        if net > 0:
            by_decision[dec]["wins"] += 1

    best = max(closed, key=_net, default=None)
    worst = min(closed, key=_net, default=None)

    trade_list = sorted(
        closed,
        key=lambda r: (str(r.get("trade_date") or ""), str(r.get("generated_at") or "")),
    )

    return {
        "generated_at": ist_now(),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "trading_days": len([d for d in daily if d["has_data"]]),
        "engines": list(ENGINES),
        "lot_size": config.lot_size,
        "summary": {
            **totals,
            "open_positions": len(opens),
            "open_mtm_rupees": round(open_mtm, 2),
            "total_including_mtm": round(totals["net_rupees"] + open_mtm, 2),
        },
        "by_engine": by_engine,
        "daily": daily,
        "by_exit_reason": dict(by_exit),
        "by_decision": dict(by_decision),
        "best_trade": best,
        "worst_trade": worst,
        "closed_trades": trade_list,
        "open_trades": opens,
    }


def render_markdown(data: Dict[str, Any]) -> str:
    s = data["summary"]
    period = data["period_start"]
    if data["period_end"] != data["period_start"]:
        period = f"{data['period_start']} → {data['period_end']}"

    lines = [
        "# Consolidated Paper P&L Report",
        "",
        f"**Generated:** {data['generated_at']}  ",
        f"**Period:** {period} ({data['trading_days']} day(s) with journal data)  ",
        f"**Books:** L1 · L2 · EV1 · EV2 (parallel paper ledgers, NIFTY lot = {data['lot_size']} qty)  ",
        f"**Source:** `journal/nifty_paper_{{ENGINE}}_*.jsonl` + L1 primary fallback",
        "",
        "## Desk totals (all engines)",
        "",
        "> Each engine runs an independent parallel book. **Desk total = sum of all four** — not one live account.",
        "",
        "| Metric | Value |",
        "|--------|------:|",
        f"| Closed trades | {s['trades']} |",
        f"| Win / loss / flat | {s['wins']} / {s['losses']} / {s['flat']} ({s['win_rate_pct']}% WR) |",
        f"| Lots traded (closed) | {s['lots']} |",
        f"| Gross P&L | ₹ {_fmt(s['gross_rupees'])} |",
        f"| Commission | ₹ {_fmt(-s['commission_rupees'])} |",
        f"| **Net realized P&L** | **₹ {_fmt(s['net_rupees'])}** |",
        f"| Open positions | {s['open_positions']} |",
        f"| Open MTM (net) | ₹ {_fmt(s['open_mtm_rupees'])} |",
        f"| **Realized + open MTM** | **₹ {_fmt(s['total_including_mtm'])}** |",
        "",
        "## By engine",
        "",
        "| Engine | Closed | Open | W/L | Gross ₹ | Comm ₹ | **Net ₹** | Open MTM ₹ |",
        "|--------|-------:|-----:|----:|--------:|-------:|----------:|-----------:|",
    ]

    desk_net = 0.0
    for eng in ENGINES:
        b = data["by_engine"].get(eng) or {}
        wl = f"{b.get('wins', 0)}/{b.get('losses', 0)}"
        closed_n = int(b.get("closed") or 0)
        if closed_n == 0 and int(b.get("open") or 0) == 0:
            lines.append(f"| {eng} | — | — | — | — | — | — | — |")
            continue
        gross = sum(_gross(r) for r in data["closed_trades"] if str(r.get("engine") or "").upper() == eng)
        comm = sum(_charges(r) for r in data["closed_trades"] if str(r.get("engine") or "").upper() == eng)
        net = float(b.get("net_pnl_rupees") or 0)
        desk_net += net
        lines.append(
            f"| {eng} | {b.get('closed', 0)} | {b.get('open', 0)} | {wl} | "
            f"{_fmt(gross)} | {_fmt(-comm)} | **{_fmt(net)}** | {_fmt(float(b.get('open_mtm_rupees') or 0))} |"
        )

    lines += [
        "",
        f"**Desk net (engines sum): ₹ {_fmt(desk_net)}**",
        "",
    ]

    if data.get("best_trade"):
        b = data["best_trade"]
        lines.append(
            f"| Best trade | {b.get('trade_date')} {b.get('engine')} id={b.get('id')} "
            f"{b.get('decision')} {b.get('strike')} → **₹ {_fmt(_net(b))}** ({b.get('exit_reason')}) |"
        )
    if data.get("worst_trade"):
        w = data["worst_trade"]
        lines.append(
            f"| Worst trade | {w.get('trade_date')} {w.get('engine')} id={w.get('id')} "
            f"{w.get('decision')} {w.get('strike')} → **₹ {_fmt(_net(w))}** ({w.get('exit_reason')}) |"
        )

    if len(data["daily"]) > 1:
        lines += [
            "",
            "## Daily breakdown",
            "",
            "| Date | Trades | W/L | Gross ₹ | Comm ₹ | Day net ₹ | Cumulative ₹ |",
            "|------|-------:|----:|--------:|-------:|----------:|-------------:|",
        ]
        for row in data["daily"]:
            if not row["has_data"]:
                continue
            wl = f"{row['wins']}/{row['losses']}" if row["trades"] else "—"
            lines.append(
                f"| {row['date']} | {row['trades']} | {wl} | {_fmt(row['gross_rupees'])} | "
                f"{_fmt(-row['commission_rupees'])} | **{_fmt(row['net_rupees'])}** | **{_fmt(row['cumulative_rupees'])}** |"
            )

    lines += [
        "",
        "## By exit reason",
        "",
        "| Exit reason | Trades | Net ₹ |",
        "|-------------|-------:|------:|",
    ]
    for reason, stats in sorted(data["by_exit_reason"].items(), key=lambda x: -abs(x[1]["net"])):
        lines.append(f"| {reason} | {stats['n']} | {_fmt(stats['net'])} |")

    lines += [
        "",
        "## By decision",
        "",
        "| Decision | Trades | Wins | Net ₹ |",
        "|----------|-------:|-----:|------:|",
    ]
    for dec, stats in sorted(data["by_decision"].items()):
        lines.append(f"| {dec} | {stats['n']} | {stats['wins']} | {_fmt(stats['net'])} |")

    if data["closed_trades"]:
        lines += [
            "",
            "## Full closed trade list",
            "",
            "| # | Date | Engine | ID | In | Out | Side | Strike | Lots | Qty | Entry | Exit | Gross ₹ | Net ₹ | Exit |",
            "|--:|------|--------|---:|----|----|------|-------:|-----:|----:|------:|-----:|--------:|------:|------|",
        ]
        for i, t in enumerate(data["closed_trades"], 1):
            lines.append(
                f"| {i} | {t.get('trade_date')} | {t.get('engine')} | {t.get('id')} | "
                f"{_hhmm(t.get('generated_at'))} | {_hhmm(t.get('exit_time'))} | {t.get('decision')} | "
                f"{t.get('strike')} | {t.get('lots')} | {t.get('quantity')} | {t.get('entry_price')} | "
                f"{t.get('exit_price')} | {_fmt(_gross(t))} | **{_fmt(_net(t))}** | {t.get('exit_reason')} |"
            )

    if data["open_trades"]:
        lines += [
            "",
            "## Open at report time",
            "",
            "| Date | Engine | ID | Side | Strike | Lots | Entry | Mark | MTM net ₹ |",
            "|------|--------|---:|------|-------:|-----:|------:|-----:|----------:|",
        ]
        for t in data["open_trades"]:
            lines.append(
                f"| {t.get('trade_date')} | {t.get('engine')} | {t.get('id')} | {t.get('decision')} | "
                f"{t.get('strike')} | {t.get('lots')} | {t.get('entry_price')} | "
                f"{t.get('mark_price') or t.get('current_price')} | {_fmt(_net(t))} |"
            )

    lines += [
        "",
        "---",
        "*Paper books only — net of round-trip commission (`nifty.core.commission`). "
        "Four parallel engine books; desk total is the sum across books.*",
    ]
    return "\n".join(lines)


def discover_engine_days(journal_dir: Path = JOURNAL_DIR) -> List[str]:
    found: set = set()
    for path in journal_dir.glob("nifty_paper_*_*.jsonl"):
        parts = path.stem.split("_")
        if len(parts) >= 4:
            day_part = parts[-1]
            if len(day_part) == 10:
                found.add(day_part)
    return sorted(found)


def main() -> None:
    parser = argparse.ArgumentParser(description="Consolidated four-engine paper P&L report")
    parser.add_argument("--date", default="", help="Single day YYYY-MM-DD (default: today)")
    parser.add_argument("--from", dest="from_date", default="", help="Range start YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", default="", help="Range end YYYY-MM-DD")
    parser.add_argument("--month", default="", help="Whole month e.g. 2026-07")
    parser.add_argument("--out", default="", help="Output markdown path")
    parser.add_argument("--print", action="store_true", help="Print markdown to stdout")
    args = parser.parse_args()

    journal_dir = JOURNAL_DIR
    if args.month:
        y, m = int(args.month[:4]), int(args.month[5:7])
        from_day = date(y, m, 1)
        to_day = date(y + 1, 1, 1) - timedelta(days=1) if m == 12 else date(y, m + 1, 1) - timedelta(days=1)
    elif args.from_date or args.to_date:
        from_day = _parse_day(args.from_date) if args.from_date else _parse_day(args.to_date)
        to_day = _parse_day(args.to_date) if args.to_date else from_day
    elif args.date:
        from_day = to_day = _parse_day(args.date)
    else:
        from_day = to_day = date.today()

    data = build_report(journal_dir, from_day=from_day, to_day=to_day)
    md = render_markdown(data)

    if args.out:
        out_path = Path(args.out)
    elif from_day == to_day:
        out_path = journal_dir / f"consolidated_pnl_report_{from_day.isoformat()}.md"
    else:
        out_path = journal_dir / f"consolidated_pnl_report_{from_day.isoformat()}_{to_day.isoformat()}.md"

    out_path.write_text(md, encoding="utf-8")
    print(f"Consolidated P&L report written: {out_path}")
    print(
        f"  Net realized: INR {data['summary']['net_rupees']:,.2f}  |  "
        f"Closed: {data['summary']['trades']}  |  WR: {data['summary']['win_rate_pct']}%"
    )
    for eng in ENGINES:
        b = data["by_engine"].get(eng) or {}
        if int(b.get("closed") or 0) or int(b.get("open") or 0):
            print(f"  {eng}: net INR {float(b.get('net_pnl_rupees') or 0):,.2f} ({b.get('closed', 0)} closed)")

    if args.print:
        print()
        print(md)


def _selftest() -> None:
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="consolidated-pnl-selftest-"))
    day = date(2026, 7, 20)
    label = day.isoformat()

    assert discover_engine_days(tmp) == []

    empty = build_report(tmp, from_day=day, to_day=day)
    assert empty["trading_days"] == 0
    assert empty["summary"]["trades"] == 0

    # L1 fallback path: only the primary paper-trades journal exists.
    (tmp / f"nifty_paper_trades_{label}.jsonl").write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {"event": "SIGNAL_GENERATED", "signal_key": "sig1", "id": 1, "decision": "BUY_CE", "strike": 25000, "entry_price": 100.0, "lot_size": 65, "generated_at": f"{label} 09:30:00"},
                {"event": "SIGNAL_CLOSED", "signal_key": "sig1", "id": 1, "decision": "BUY_CE", "strike": 25000, "entry_price": 100.0, "exit_price": 150.0, "lot_size": 65, "exit_reason": "TARGET", "exit_time": f"{label} 10:15:00"},
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    l1_rows = load_engine_trades(tmp, day)
    assert len(l1_rows) == 1
    assert l1_rows[0]["engine"] == "L1"
    assert l1_rows[0]["book_role"] == "primary"

    # L2 engine ledger present too -> both engines contribute, no L1 fallback double-count.
    (tmp / f"nifty_paper_L2_{label}.jsonl").write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {"event": "SIGNAL_GENERATED", "id": 2, "decision": "BUY_PE", "strike": 24900, "entry_price": 80.0, "lot_size": 65},
                {"event": "SIGNAL_CLOSED", "id": 2, "decision": "BUY_PE", "strike": 24900, "entry_price": 80.0, "exit_price": 60.0, "lot_size": 65, "exit_reason": "STOP"},
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert discover_engine_days(tmp) == [label]

    report = build_report(tmp, from_day=day, to_day=day)
    assert report["trading_days"] == 1
    assert report["summary"]["trades"] == 2  # one L1 (fallback), one L2
    assert "L1" in report["by_engine"] and "L2" in report["by_engine"]
    assert report["by_engine"]["L1"]["closed"] == 1
    assert report["by_engine"]["L2"]["closed"] == 1
    assert report["by_engine"]["EV1"]["closed"] == 0

    md = render_markdown(report)
    assert "Consolidated Paper P&L Report" in md
    assert "By engine" in md
    assert "Full closed trade list" in md

    print("[analytics.consolidated_pnl_report] selftest OK: L1 fallback, multi-engine dedupe, markdown render")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        main()
    else:
        _selftest()
