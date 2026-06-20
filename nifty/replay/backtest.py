"""Score the engine's replayed signals against the actual stored prices.

Uses the cached replay timeline (built once per day). The engine already marks
each paper trade to the real stored ltp and exits on the actual target/stop/OI
break, so realized P&L is a true backtest. We add, from the same ticks, each
trade's max-favorable / max-adverse excursion (MFE/MAE) and render it onto the
standard TradingView-style report.
"""

from __future__ import annotations

import json
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Dict, List

import nifty.eod.session_report as sr
from nifty.replay import loader
from nifty.replay.session import REPLAY_OUT, ReplayTimeline


def _excursions(day: str, signal: Dict[str, Any], end_ts: str) -> Dict[str, Any]:
    contract = signal.get("entry_contract")
    entry = signal.get("entry_price")
    gen = str(signal.get("generated_at") or "")
    exit_ts = str(signal.get("exit_time") or end_ts)
    blank = {"mfe_pct": None, "mae_pct": None, "actual_high": None, "actual_low": None}
    if not contract or not entry or not gen:
        return blank
    path = loader.option_price_path(day, contract, gen, exit_ts)
    if not path:
        return blank
    prices = [p for _, p in path]
    hi, lo = max(prices), min(prices)
    return {
        "mfe_pct": round((hi - entry) / entry * 100, 1),
        "mae_pct": round((lo - entry) / entry * 100, 1),
        "actual_high": round(hi, 2),
        "actual_low": round(lo, 2),
    }


def _mfe_mae_table(day: str, signals: List[Dict[str, Any]], end_ts: str) -> str:
    rows = []
    for sig in signals:
        ex = _excursions(day, sig, end_ts)
        rows.append(
            f"<tr><td>{sig.get('id')}</td><td>{str(sig.get('generated_at'))[-8:]}</td>"
            f"<td>{sig.get('decision')}</td><td>{sig.get('strike')}</td>"
            f"<td>{sr._fmt(sig.get('entry_price'))}</td>"
            f"<td>{sr._fmt(sig.get('exit_price') or sig.get('current_price'))}</td>"
            f"<td class='good'>{sr._fmt(ex['actual_high'])} (+{sr._fmt(ex['mfe_pct'],1)}%)</td>"
            f"<td class='bad'>{sr._fmt(ex['actual_low'])} ({sr._fmt(ex['mae_pct'],1)}%)</td>"
            f"<td class='{sr._pnl_class(sig.get('pnl_net_rupees'))}'>{sr._fmt(sig.get('pnl_net_rupees'),0)}</td>"
            f"<td class='muted'>{sig.get('exit_reason') or ('OPEN' if not sig.get('exit_time') else '-')}</td></tr>"
        )
    body = "".join(rows) or "<tr><td colspan='10' class='muted'>No signals generated.</td></tr>"
    return (
        "<h2 class='section'>Signals vs actual prices (MFE / MAE)</h2>"
        "<div class='card'><table><thead><tr>"
        "<th>ID</th><th>In</th><th>Side</th><th>Strike</th><th>Entry</th><th>Exit</th>"
        "<th>Actual high (MFE)</th><th>Actual low (MAE)</th><th>Net &#8377;</th><th>Exit</th>"
        "</tr></thead><tbody>" + body + "</tbody></table></div>"
    )


def _write_temp_journal(day: str, signals: List[Dict[str, Any]]) -> Path:
    """Write the replayed signals as paper-trade events so the report renderer reads them."""
    tmp = Path(tempfile.mkdtemp(prefix=f"nifty-bt-{day}-"))
    path = tmp / f"nifty_paper_trades_{day}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for sig in signals:
            fh.write(json.dumps({"event": "SIGNAL_GENERATED", **sig}, default=str) + "\n")
            if sig.get("exit_time"):
                fh.write(json.dumps({"event": "SIGNAL_CLOSED", **sig}, default=str) + "\n")
    return tmp


def run_backtest(day: str, rebuild: bool = False) -> Dict[str, Any]:
    """Build/load the day's timeline, then render the MFE/MAE backtest report."""
    REPLAY_OUT.mkdir(parents=True, exist_ok=True)
    timeline = ReplayTimeline(day, rebuild=rebuild)
    signals = timeline.signals
    end_ts = timeline.data.get("end") or f"{day} 15:30:00"
    trade_date = date.fromisoformat(day)

    tmp = _write_temp_journal(day, signals)
    old_journal, old_legacy = sr.JOURNAL_DIR, sr.LEGACY_SIGNALS
    sr.JOURNAL_DIR = tmp
    sr.LEGACY_SIGNALS = tmp / "nifty_oi_signals.jsonl"
    try:
        summary = sr.build_performance_summary(trade_date)
        report_html = sr.render_report_html(trade_date, summary)
    finally:
        sr.JOURNAL_DIR, sr.LEGACY_SIGNALS = old_journal, old_legacy

    banner = (
        f"<p class='sub'>BACKTEST &mdash; engine re-run on archived ticks for {day}, "
        f"signals marked to actual stored prices.</p>"
    )
    report_html = report_html.replace("</h1>", "</h1>" + banner, 1)
    report_html = report_html.replace("</body>", _mfe_mae_table(day, signals, end_ts) + "</body>", 1)

    out = REPLAY_OUT / f"report_{day}.html"
    out.write_text(report_html, encoding="utf-8")
    return {
        "day": day,
        "report_path": str(out),
        "signals": len(signals),
        "net_total": summary.get("net_total"),
        "wins": summary.get("wins"),
        "losses": summary.get("losses"),
        "win_rate": summary.get("win_rate"),
        "profit_factor": summary.get("profit_factor"),
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Backtest the engine over an archived day")
    parser.add_argument("day", help="Trade date YYYY-MM-DD (must have an archived tick DB)")
    parser.add_argument("--rebuild", action="store_true", help="Force rebuild the timeline cache")
    args = parser.parse_args()
    print(run_backtest(args.day, rebuild=args.rebuild))


if __name__ == "__main__":
    main()
