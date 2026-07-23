#!/usr/bin/env python3
"""NSE pre-open auction capture for desk journal — 9:00 entry, ~9:08 equilibrium, 9:15 cash open compare.

Ported faithfully from quant-desk-engine v4/ATLAS's nse_auction_capture.py
(mentor-authored). No logic changed. Adaptations:
- `from desk_phase_capture import JOURNAL_DIR, ist_now, today_str` ->
  nifty.paths.JOURNAL_DIR + nifty.core.journal.ist_now/today_str (both
  already exist with identical signatures).
- `from desk_kite_spot import load_official_prev_close` ->
  nifty.kite.spot.load_official_prev_close (added this session, verbatim
  from the same v4 desk_kite_spot.py, verified to already match
  nifty-dashboard's existing nse_eod_filing_{date}.json/daily_levels_{date}.json/
  key_levels_{date}.json journal shapes).
- `from desk_kite_spot import fetch_kite_nifty_quote` (local import inside
  fetch_kite_nifty_last) -> nifty.kite.spot.fetch_kite_nifty_quote (already
  exists, same name).
- PROBE_DIR/SESSIONS_DIR/READINESS_DIR keep the source's
  market-state-observatory/research/auction_probe relative layout, rooted
  at nifty.paths.PROJECT_ROOT. That directory doesn't exist in
  nifty-dashboard yet (market-state-observatory is a separate, larger,
  not-yet-reviewed initiative) — every read through equilibrium_from_probe/
  _load_probe_session_rows already degrades gracefully to {}/[]/None when
  the directory or files are absent, so this needs no additional guard.
- `from desk_morning_report import write_morning_desk_report` (local
  import inside refresh_morning_report_after_auction) points at a
  2693-line file that is out of scope for this port; the source's own
  try/except around the import means this call always gracefully returns
  None until (if ever) a morning-report writer is ported.

Genuinely new capability: nifty-dashboard has no pre-open auction capture
today. Polls NSE's pre-open indicative price every 15s during the 9:00-
9:08:30 IST auction window, records the swept high/low/equilibrium path
(not just the settled price), and journals it alongside the official
prior close for gap-quality context at cash open.

Not yet wired into the live pipeline (no scheduler entry calls
run_auction_sweep_poll).
Self-check: python -m nifty.sources.nse_auction_capture
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests

from nifty.core.journal import ist_now, today_str
from nifty.kite.spot import load_official_prev_close
from nifty.paths import JOURNAL_DIR, PROJECT_ROOT

PROBE_DIR = PROJECT_ROOT / "market-state-observatory" / "research" / "auction_probe"
SESSIONS_DIR = PROBE_DIR / "sessions"
READINESS_DIR = PROBE_DIR / "readiness"
IST = ZoneInfo("Asia/Kolkata")

NSE_HOME = "https://www.nseindia.com"
PREOPEN_URL = f"{NSE_HOME}/api/market-data-pre-open?key=NIFTY"
PREOPEN_REFERER = f"{NSE_HOME}/market-data/pre-open-market-cm-and-emerge-market"

SWEEP_INTERVAL_S = 15
SWEEP_WINDOW_START = dt_time(9, 0)
SWEEP_WINDOW_END = dt_time(9, 8, 30)


def auction_scan_path(trade_date: date) -> Path:
    return JOURNAL_DIR / f"auction_scan_{today_str(trade_date)}.json"


def auction_sweep_path(trade_date: date) -> Path:
    return JOURNAL_DIR / f"auction_sweep_{today_str(trade_date)}.jsonl"


def _ist_now() -> datetime:
    return datetime.now(IST)


def _wait_until(target: datetime) -> None:
    while True:
        now = _ist_now()
        if now >= target:
            return
        time.sleep(min(1.0, (target - now).total_seconds()))


def nse_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": PREOPEN_REFERER,
        }
    )
    session.get(NSE_HOME, timeout=20)
    return session


def fetch_nse_preopen(session: Optional[requests.Session] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    sess = session or nse_session()
    try:
        response = sess.get(PREOPEN_URL, timeout=20)
        response.raise_for_status()
        return response.json(), None
    except Exception as exc:
        return None, str(exc)


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def extract_nse_index_snapshot(payload: Dict[str, Any]) -> Dict[str, Any]:
    block = payload.get("niftyPreopenStatus") or {}
    last = _as_float(block.get("lastPrice"))
    change = _as_float(block.get("change"))
    pchange = _as_float(block.get("pChange"))
    return {
        "status": block.get("status"),
        "indicative": last,
        "change_pts": change,
        "change_pct": pchange,
        "nse_timestamp": payload.get("timestamp"),
        "advances": payload.get("advances"),
        "declines": payload.get("declines"),
        "unchanged": payload.get("unchanged"),
    }


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _load_probe_session_rows(trade_date: date) -> List[Dict[str, Any]]:
    path = SESSIONS_DIR / f"{trade_date.isoformat()}.jsonl"
    if not path.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def load_sweep_rows(trade_date: date) -> List[Dict[str, Any]]:
    path = auction_sweep_path(trade_date)
    if not path.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def append_sweep_row(trade_date: date, row: Dict[str, Any]) -> None:
    path = auction_sweep_path(trade_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def fetch_kite_nifty_last() -> Optional[float]:
    try:
        from nifty.kite.spot import fetch_kite_nifty_quote

        quote = fetch_kite_nifty_quote() or {}
        return _as_float(quote.get("last") or quote.get("open"))
    except Exception:
        return None


def summarize_sweep_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Derive auction path: high / low during OPEN polls, equilibrium at CLOSED."""
    nse_open_prices: List[float] = []
    kite_prices: List[float] = []
    first_open: Optional[Dict[str, Any]] = None
    last_before_closed: Optional[float] = None
    equilibrium: Optional[float] = None
    equilibrium_at: Optional[str] = None
    tick_trail: List[Dict[str, Any]] = []

    for row in rows:
        if row.get("error"):
            continue
        status = str(row.get("status") or "").upper()
        indicative = _as_float(row.get("indicative"))
        kite_last = _as_float(row.get("kite_last"))
        observed = row.get("observed_at")

        if kite_last is not None:
            kite_prices.append(kite_last)

        if indicative is None:
            continue

        if status != "CLOSED":
            nse_open_prices.append(indicative)
            last_before_closed = indicative
            if first_open is None:
                first_open = {
                    "observed_at": observed,
                    "indicative": indicative,
                    "status": row.get("status"),
                }
            tick_trail.append(
                {
                    "observed_at": observed,
                    "indicative": indicative,
                    "kite_last": kite_last,
                    "status": status,
                }
            )
        else:
            equilibrium = indicative
            equilibrium_at = observed
            tick_trail.append(
                {
                    "observed_at": observed,
                    "indicative": indicative,
                    "kite_last": kite_last,
                    "status": status,
                }
            )

    nse_high = round(max(nse_open_prices), 2) if nse_open_prices else None
    nse_low = round(min(nse_open_prices), 2) if nse_open_prices else None
    kite_high = round(max(kite_prices), 2) if kite_prices else None
    kite_low = round(min(kite_prices), 2) if kite_prices else None
    spread = round(nse_high - nse_low, 2) if nse_high is not None and nse_low is not None else None

    return {
        "poll_count": len(rows),
        "open_poll_count": len(nse_open_prices),
        "nse_indicative_high": nse_high,
        "nse_indicative_low": nse_low,
        "nse_indicative_spread_pts": spread,
        "kite_high": kite_high,
        "kite_low": kite_low,
        "first_open": first_open,
        "last_before_closed": last_before_closed,
        "equilibrium": equilibrium,
        "equilibrium_at": equilibrium_at,
        "sweep_complete": equilibrium is not None,
        "tick_trail": tick_trail[-24:],
    }


def indicative_range_from_rows(rows: List[Dict[str, Any]]) -> Tuple[Optional[float], Optional[float]]:
    """Highest / lowest NSE indicative lastPrice while auction status is not CLOSED."""
    prices: List[float] = []
    for row in rows:
        if row.get("error"):
            continue
        if str(row.get("status", "")).upper() == "CLOSED":
            continue
        price = _as_float(row.get("lastPrice"))
        if price is not None:
            prices.append(price)
    if not prices:
        return None, None
    return round(max(prices), 2), round(min(prices), 2)


def equilibrium_from_probe(trade_date: date) -> Dict[str, Any]:
    iso = trade_date.isoformat()
    readiness = _load_json(READINESS_DIR / f"{iso}_readiness.json") or _load_json(
        READINESS_DIR / f"{iso}_verify.json"
    ) or {}

    closed = readiness.get("first_closed_status_seen") or {}
    eq = _as_float(closed.get("lastPrice"))
    observed_at = closed.get("observed_at")
    source = "probe_readiness"

    if eq is None:
        eq = _as_float(readiness.get("frozen_preopen_lastPrice"))
        source = "probe_frozen"

    if eq is None:
        for row in reversed(_load_probe_session_rows(trade_date)):
            if row.get("error"):
                continue
            if str(row.get("status", "")).upper() == "CLOSED" and row.get("lastPrice"):
                eq = _as_float(row.get("lastPrice"))
                observed_at = row.get("observed_at")
                source = "probe_session"
                break

    high, low = indicative_range_from_rows(_load_probe_session_rows(trade_date))
    return {
        "equilibrium": eq,
        "observed_at": observed_at,
        "source": source if eq is not None else None,
        "indicative_high": high,
        "indicative_low": low,
    }


def _recompute_auction_range(doc: Dict[str, Any]) -> None:
    sweep = doc.get("sweep_summary") or {}
    highs: List[float] = []
    lows: List[float] = []
    probe = doc.get("probe") or {}

    if sweep.get("nse_indicative_high") is not None:
        highs.append(float(sweep["nse_indicative_high"]))
    if sweep.get("nse_indicative_low") is not None:
        lows.append(float(sweep["nse_indicative_low"]))
    if probe.get("indicative_high") is not None:
        highs.append(float(probe["indicative_high"]))
    if probe.get("indicative_low") is not None:
        lows.append(float(probe["indicative_low"]))
    for cp in (doc.get("checkpoints") or {}).values():
        if str(cp.get("status", "")).upper() == "CLOSED":
            continue
        val = _as_float(cp.get("indicative"))
        if val is not None:
            highs.append(val)
            lows.append(val)

    upper = round(max(highs), 2) if highs else None
    lower = round(min(lows), 2) if lows else None
    spread = round(upper - lower, 2) if upper is not None and lower is not None else None
    poll_count = sweep.get("poll_count") or 0
    open_polls = sweep.get("open_poll_count") or 0

    if spread == 0 and poll_count <= 1:
        note = "Equilibrium only — no live 9:00–9:08 sweep captured (poll at auction open tomorrow)"
    elif spread and spread > 0:
        note = f"NSE indicative swept {open_polls} polls · spread {spread:.0f} pts before ~9:08 match"
    else:
        note = "NSE index pre-open indicative sweep (upper / lower bid proxy)"

    doc["auction_range"] = {
        "upper_indicative": upper,
        "lower_indicative": lower,
        "spread_pts": spread,
        "poll_count": poll_count,
        "open_poll_count": open_polls,
        "equilibrium": sweep.get("equilibrium") or probe.get("equilibrium"),
        "note": note,
    }


def load_auction_scan(trade_date: date) -> Dict[str, Any]:
    path = auction_scan_path(trade_date)
    if path.is_file():
        data = _load_json(path) or {}
        if isinstance(data, dict):
            return data
    return {}


def record_auction_checkpoint(
    trade_date: date,
    checkpoint_id: str,
    *,
    extra: Optional[Dict[str, Any]] = None,
    session: Optional[requests.Session] = None,
) -> Dict[str, Any]:
    """Append/update auction_scan artifact for a lifecycle checkpoint."""
    label = today_str(trade_date)
    prev_close = load_official_prev_close(trade_date)
    payload, error = fetch_nse_preopen(session)
    snap = extract_nse_index_snapshot(payload) if payload else {}
    if error:
        snap["error"] = error

    doc = load_auction_scan(trade_date)
    if not doc:
        doc = {
            "trade_date": label,
            "prev_close": prev_close,
            "checkpoints": {},
        }
    doc["prev_close"] = prev_close or doc.get("prev_close")
    doc["updated_at"] = ist_now()

    cp: Dict[str, Any] = {
        "checkpoint": checkpoint_id,
        "captured_at": ist_now(),
        **snap,
    }
    if extra:
        cp.update(extra)

    status = str(snap.get("status") or "").upper()
    if checkpoint_id == "auction_eq_908" or status == "CLOSED":
        eq = _as_float(snap.get("indicative"))
        if eq is not None:
            cp["equilibrium"] = eq
            cp["equilibrium_note"] = "NSE pre-open match (~9:08)"

    doc.setdefault("checkpoints", {})[checkpoint_id] = cp
    sweep_rows = load_sweep_rows(trade_date)
    if sweep_rows:
        doc["sweep_summary"] = summarize_sweep_rows(sweep_rows)
        doc["sweep_log"] = str(auction_sweep_path(trade_date))
    doc["probe"] = equilibrium_from_probe(trade_date)
    _recompute_auction_range(doc)

    path = auction_scan_path(trade_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")
    return doc


def run_auction_sweep_poll(
    trade_date: Optional[date] = None,
    *,
    refresh_report: bool = True,
    wait_for_window: bool = True,
) -> Dict[str, Any]:
    """
    Poll NSE pre-open every 15s during 09:00-09:08:30 IST.
    Captures indicative up/down path before equilibrium — not just the settled price.
    """
    day = trade_date or _ist_now().date()
    label = today_str(day)
    start_at = datetime.combine(day, SWEEP_WINDOW_START, IST)
    end_at = datetime.combine(day, SWEEP_WINDOW_END, IST)
    now = _ist_now()

    if wait_for_window and now < start_at:
        _wait_until(start_at)
    if now > end_at and not load_sweep_rows(day):
        record_auction_checkpoint(day, "auction_eq_908", extra={"sweep_note": "late_run_single_sample"})
        doc = load_auction_scan(day)
        if refresh_report:
            refresh_morning_report_after_auction(day)
        return {"trade_date": label, "late_run": True, "auction_scan": doc}

    session = nse_session()
    prev_status: Optional[str] = None
    while _ist_now() <= end_at:
        payload, error = fetch_nse_preopen(session)
        snap = extract_nse_index_snapshot(payload) if payload else {}
        row: Dict[str, Any] = {
            "observed_at": _ist_now().isoformat(),
            "trade_date": label,
            "poll_type": "auction_sweep",
            "error": error,
            **snap,
            "kite_last": fetch_kite_nifty_last(),
            "status_changed": (
                prev_status is not None
                and snap.get("status") is not None
                and str(snap["status"]) != prev_status
            ),
        }
        append_sweep_row(day, row)
        if snap.get("status"):
            prev_status = str(snap["status"])
        if _ist_now() + timedelta(seconds=SWEEP_INTERVAL_S) > end_at:
            break
        time.sleep(SWEEP_INTERVAL_S)

    sweep_rows = load_sweep_rows(day)
    summary = summarize_sweep_rows(sweep_rows)
    prev_close = load_official_prev_close(day)
    doc = load_auction_scan(day) or {
        "trade_date": label,
        "prev_close": prev_close,
        "checkpoints": {},
    }
    doc["prev_close"] = prev_close or doc.get("prev_close")
    doc["updated_at"] = ist_now()
    doc["sweep_summary"] = summary
    doc["sweep_log"] = str(auction_sweep_path(day))

    if summary.get("first_open"):
        fo = summary["first_open"]
        doc.setdefault("checkpoints", {})["auction_start_900"] = {
            "checkpoint": "auction_start_900",
            "captured_at": fo.get("observed_at"),
            "status": fo.get("status"),
            "indicative": fo.get("indicative"),
            "source": "sweep_first_open",
        }
    if summary.get("equilibrium") is not None:
        doc.setdefault("checkpoints", {})["auction_eq_908"] = {
            "checkpoint": "auction_eq_908",
            "captured_at": summary.get("equilibrium_at"),
            "status": "CLOSED",
            "indicative": summary.get("equilibrium"),
            "equilibrium": summary.get("equilibrium"),
            "equilibrium_note": "NSE pre-open match (~9:08)",
            "source": "sweep_equilibrium",
        }

    doc["probe"] = equilibrium_from_probe(day)
    _recompute_auction_range(doc)
    auction_scan_path(day).write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")

    report_path = None
    if refresh_report:
        report_path = refresh_morning_report_after_auction(day)

    return {
        "trade_date": label,
        "sweep_summary": summary,
        "auction_scan": doc,
        "morning_desk_report": report_path,
        "sweep_log": str(auction_sweep_path(day)),
    }


def refresh_morning_report_after_auction(trade_date: date) -> Optional[str]:
    try:
        from nifty.morning.report import write_morning_desk_report

        paths = write_morning_desk_report(trade_date)
        return paths.get("markdown")
    except Exception:
        return None


def _selftest() -> None:
    import tempfile

    global JOURNAL_DIR
    tmp = Path(tempfile.mkdtemp(prefix="nse-auction-capture-selftest-"))
    original_journal_dir = JOURNAL_DIR
    try:
        JOURNAL_DIR = tmp
        day = date(2026, 7, 21)

        assert auction_scan_path(day).name == "auction_scan_2026-07-21.json"
        assert auction_sweep_path(day).name == "auction_sweep_2026-07-21.jsonl"

        snapshot = extract_nse_index_snapshot(
            {
                "niftyPreopenStatus": {"lastPrice": "25100.50", "change": "12.5", "pChange": "0.05"},
                "timestamp": "21-Jul-2026 09:08:12",
                "advances": 30,
                "declines": 18,
                "unchanged": 2,
            }
        )
        assert snapshot["indicative"] == 25100.5
        assert snapshot["status"] is None  # no top-level "status" key in this fixture

        # No sweep file yet -> empty rows, never raises.
        assert load_sweep_rows(day) == []
        assert load_auction_scan(day) == {}

        rows = [
            {"observed_at": "t1", "indicative": 25080.0, "status": "OPEN", "kite_last": 25082.0},
            {"observed_at": "t2", "indicative": 25095.0, "status": "OPEN", "kite_last": 25090.0},
            {"observed_at": "t3", "indicative": 25101.0, "status": "CLOSED", "kite_last": 25101.5},
        ]
        for row in rows:
            append_sweep_row(day, row)
        loaded = load_sweep_rows(day)
        assert len(loaded) == 3

        summary = summarize_sweep_rows(loaded)
        assert summary["nse_indicative_high"] == 25095.0  # highest among OPEN rows only
        assert summary["nse_indicative_low"] == 25080.0
        assert summary["equilibrium"] == 25101.0  # from the CLOSED row
        assert summary["sweep_complete"] is True
        assert summary["kite_high"] == 25101.5

        high, low = indicative_range_from_rows(
            [{"lastPrice": 25080.0, "status": "OPEN"}, {"lastPrice": 25095.0, "status": "OPEN"}, {"lastPrice": 25101.0, "status": "CLOSED"}]
        )
        assert high == 25095.0 and low == 25080.0  # CLOSED row excluded

        # equilibrium_from_probe: no market-state-observatory dir -> graceful None, never raises.
        probe = equilibrium_from_probe(day)
        assert probe["equilibrium"] is None
        assert probe["source"] is None

        doc = {
            "sweep_summary": summary,
            "probe": probe,
            "checkpoints": {"auction_start_900": {"status": "OPEN", "indicative": 25080.0}},
        }
        _recompute_auction_range(doc)
        assert doc["auction_range"]["upper_indicative"] == 25095.0
        assert doc["auction_range"]["lower_indicative"] == 25080.0
        assert doc["auction_range"]["equilibrium"] == 25101.0
        assert "swept" in doc["auction_range"]["note"]

        # refresh_morning_report_after_auction: no morning-report writer ported yet -> graceful None.
        assert refresh_morning_report_after_auction(day) is None

        # record_auction_checkpoint: NSE fetch will fail in this sandboxed test env (no
        # network access assumed); it must still produce a well-formed journal entry.
        doc2 = record_auction_checkpoint(day, "auction_start_900")
        assert doc2["trade_date"] == "2026-07-21"
        assert "checkpoints" in doc2 and "auction_start_900" in doc2["checkpoints"]
        assert auction_scan_path(day).exists()
    finally:
        JOURNAL_DIR = original_journal_dir

    print("[sources.nse_auction_capture] selftest OK: snapshot parsing, sweep summary, auction range, checkpoint journal")


if __name__ == "__main__":
    _selftest()
