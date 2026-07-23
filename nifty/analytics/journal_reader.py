#!/usr/bin/env python3
"""Shared journal-reading utilities for the mentor's research/learning modules.

Extracted from quant-desk-engine's nifty_relationships_lab.py (mentor-authored)
— these are the foundational journal-parsing helpers, split out from that
file's actual graph-building logic (ported separately, see the porting todo
list) so nifty_model_learning.py's port doesn't duplicate them. No logic
changed.

Not yet wired into the live pipeline.
Self-check: python -m nifty.analytics.journal_reader
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from nifty.paths import JOURNAL_DIR

_JOURNAL_MARKERS = (
    "decision_engine",
    "nifty_options_analytics",
    "nifty_paper_trades",
    "nifty_signal_candidates",
    "nifty_alerts",
    "desk_brief",
    "morning_desk",
)


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _load_jsonl(path: Path, limit: int = 2000) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows[-limit:]


def _parse_ts(row: Dict[str, Any]) -> Optional[float]:
    for key in ("as_of_epoch", "ts", "timestamp"):
        val = row.get(key)
        if val is not None:
            ts = _as_float(val, -1)
            if ts > 1_000_000_000:
                return ts
    recorded = str(row.get("recorded_at") or "")
    if len(recorded) >= 19:
        try:
            return datetime.strptime(recorded[:19], "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            pass
    return None


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def _recorded_ts(row: Dict[str, Any]) -> Optional[float]:
    ts = _parse_ts(row)
    if ts is not None:
        return ts
    recorded = str(row.get("recorded_at") or row.get("generated_at") or "")
    if len(recorded) >= 19:
        try:
            return datetime.strptime(recorded[:19], "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            pass
    return None


def _liquidity_grab_info(liquidity_engine: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    liq = liquidity_engine or {}
    grab = liq.get("liquidity_grab")
    if isinstance(grab, dict):
        return grab
    label = str(grab or liq.get("liquidity_grab_source") or "").strip()
    if label and label.upper() not in {"", "NONE", "—"}:
        return {"active": True, "direction": label, "source": liq.get("liquidity_grab_source") or label}
    return {}


def load_paper_trades_from_journal(journal_dir: Path, day: Optional[date] = None) -> List[Dict[str, Any]]:
    """Reconstruct paper trade book from journal lifecycle rows.

    Matches nifty-dashboard's own paper-trade journal exactly: events
    SIGNAL_GENERATED/SIGNAL_UPDATE/SIGNAL_CLOSED, keyed by signal_key
    (state.py's _append_signal_journal / _maybe_take_paper_signal).
    """
    d = day or date.today()
    rows = _load_jsonl(journal_dir / f"nifty_paper_trades_{d.isoformat()}.jsonl", limit=5000)
    book: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        event = str(row.get("event") or "")
        key = str(row.get("signal_key") or row.get("id") or "")
        if not key:
            continue
        if event == "SIGNAL_GENERATED":
            book[key] = dict(row)
            book[key]["status"] = "OPEN"
            book[key]["entry_time"] = row.get("generated_at") or row.get("recorded_at")
            book[key]["entry_ts"] = _recorded_ts(row)
        elif event == "SIGNAL_UPDATE" and key in book:
            book[key].update(row)
        elif event == "SIGNAL_CLOSED":
            prev = book.get(key, {})
            book[key] = {**prev, **row, "status": "CLOSED"}
            book[key]["exit_ts"] = _recorded_ts(row)
    return [trade for trade in book.values() if trade.get("decision")]


def list_available_journal_days(journal_dir: Path = JOURNAL_DIR) -> List[str]:
    """Dates with at least one research-relevant journal artifact."""
    found: set[str] = set()
    for path in journal_dir.glob("*"):
        name = path.name
        for prefix in _JOURNAL_MARKERS:
            token = f"{prefix}_"
            if name.startswith(token):
                rest = name[len(token):]
                day_part = rest.split(".")[0]
                if len(day_part) == 10 and day_part[4] == "-" and day_part[7] == "-":
                    found.add(day_part)
    return sorted(found, reverse=True)


def journal_day_inventory(journal_dir: Path, day: date) -> Dict[str, Any]:
    """Per-day journal counts for replay UI."""
    d = day.isoformat()
    paths = {
        "decision_engine": journal_dir / f"decision_engine_{d}.jsonl",
        "options_analytics": journal_dir / f"nifty_options_analytics_{d}.jsonl",
        "paper_trades": journal_dir / f"nifty_paper_trades_{d}.jsonl",
        "signal_candidates": journal_dir / f"nifty_signal_candidates_{d}.jsonl",
        "alerts": journal_dir / f"nifty_alerts_{d}.jsonl",
    }
    counts = {key: len(_load_jsonl(path, limit=10_000)) for key, path in paths.items()}
    trades = load_paper_trades_from_journal(journal_dir, day)
    closed = [t for t in trades if str(t.get("status")) == "CLOSED"]
    return {
        "date": d,
        "counts": counts,
        "paper_trades": len(trades),
        "closed_trades": len(closed),
        "has_replay": counts.get("decision_engine", 0) > 0 or counts.get("options_analytics", 0) > 0,
    }


def _selftest() -> None:
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="journal-reader-selftest-"))
    day = date(2026, 6, 19)
    path = tmp / f"nifty_paper_trades_{day.isoformat()}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"event": "SIGNAL_GENERATED", "signal_key": "23000:PE:BUY_CE",
                             "decision": "BUY_CE", "generated_at": "2026-06-19 10:00:00"}) + "\n")
        fh.write(json.dumps({"event": "SIGNAL_CLOSED", "signal_key": "23000:PE:BUY_CE",
                             "recorded_at": "2026-06-19 10:30:00", "pnl_pct": 12.0}) + "\n")

    rows = _load_jsonl(path)
    assert len(rows) == 2
    assert _load_jsonl(tmp / "does_not_exist.jsonl") == []

    trades = load_paper_trades_from_journal(tmp, day)
    assert len(trades) == 1
    assert trades[0]["status"] == "CLOSED" and trades[0]["pnl_pct"] == 12.0

    ts = _recorded_ts({"recorded_at": "2026-06-19 10:30:00"})
    assert ts is not None and _fmt_time(ts) == "10:30"

    assert _liquidity_grab_info(None) == {}
    assert _liquidity_grab_info({"liquidity_grab": "UPSIDE_LIQUIDITY_GRAB"})["active"] is True

    days = list_available_journal_days(tmp)
    assert days == ["2026-06-19"]

    inv = journal_day_inventory(tmp, day)
    assert inv["paper_trades"] == 1 and inv["closed_trades"] == 1

    print("[analytics.journal_reader] selftest OK: jsonl loading, paper-trade reconstruction, day inventory")


if __name__ == "__main__":
    _selftest()
