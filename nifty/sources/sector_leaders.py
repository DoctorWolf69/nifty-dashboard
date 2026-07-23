#!/usr/bin/env python3
"""Per-stock sector leaders from NSE EOD equity bhavcopy.

Ported faithfully from quant-desk-engine v4/ATLAS's sector_leaders.py
(mentor-authored). No logic changed. Adaptations:
- NSE_EOD_DB/JOURNAL_DIR/SECTOR_CONSTITUENTS_PATH resolve via nifty.paths.
- `from desk_kite_spot import load_kite_optional` -> nifty.kite.spot
  (already ported, same function name).
- `from desk_phase_capture import analyze_sectors` -> nifty.morning.phases
  (already ported, same function name).
- `from kite_sector_scan import fetch_sector_performance_eod,
  load_desk_sector_names` -> nifty.sources.kite_sector_scan (this
  session's next port, adds the EOD/historical mode nifty-dashboard's
  existing fetch_sector_performance() lacks).
- `_filter_report_sectors` (originally in desk_morning_report.py, a
  2693-line file out of scope to port wholesale for one small helper) is
  inlined verbatim as a local function instead.

Genuinely new capability: computes per-sector-basket stock leaders/
laggards, advance/decline breadth, and green/red streak detection from raw
NSE equity bhavcopy data that nifty-dashboard already archives (nse_eod.py)
but doesn't yet turn into sector-relative stock rankings. Needs a
config/sector_constituents.json mapping (sector name -> list of ticker
symbols) that doesn't exist yet in nifty-dashboard — every function here
degrades to a documented "no_constituents_config" error dict when that
file is absent, exactly like the null-safe pattern used throughout this
porting effort, rather than raising.

build_eod_sector_scan() specifically has a further, currently-unresolvable
dependency: `_filter_report_sectors` is now local (resolved), but its own
deferred imports of `nifty.sources.kite_sector_scan.fetch_sector_performance_eod`/
`load_desk_sector_names` only resolve once that module is updated (next in
this session's porting batch). Since these are all local/deferred imports
inside the function body (not module-level), this file imports and
self-tests cleanly regardless — only calling build_eod_sector_scan() itself
would currently fail until kite_sector_scan.py's update lands.

Not yet wired into the live pipeline.
Self-check: python -m nifty.sources.sector_leaders
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from nifty.paths import JOURNAL_DIR, NSE_EOD_DIR, PROJECT_ROOT

NSE_EOD_DB = NSE_EOD_DIR / "nse_eod.sqlite"
SECTOR_CONSTITUENTS_PATH = PROJECT_ROOT / "config" / "sector_constituents.json"


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_constituents() -> Dict[str, List[str]]:
    if not SECTOR_CONSTITUENTS_PATH.exists():
        return {}
    payload = json.loads(SECTOR_CONSTITUENTS_PATH.read_text(encoding="utf-8"))
    return {str(k): [str(s).upper() for s in v] for k, v in payload.items()}


def _previous_trading_day(start: date) -> date:
    day = start - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def resolve_stock_session_date(
    trade_date: Optional[date] = None,
    explicit: Optional[str] = None,
) -> Optional[str]:
    """EOD session that feeds the morning sector stock leaders (T-1 vs today)."""
    if explicit:
        return str(explicit)[:10]
    day = trade_date or date.today()
    return _previous_trading_day(day).isoformat()


def load_stock_moves(
    session_date: str,
    symbols: Optional[List[str]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Load prior-session % change per symbol from equity bhavcopy SQLite."""
    if not NSE_EOD_DB.exists():
        return {}

    symbol_filter = ""
    params: List[Any] = [session_date]
    if symbols:
        placeholders = ",".join("?" for _ in symbols)
        symbol_filter = f" AND tckrsymb IN ({placeholders})"
        params.extend([s.upper() for s in symbols])

    query = f"""
        SELECT tckrsymb, clspric, prvsclsgpric
        FROM equity_bhavcopy
        WHERE trade_date = ? AND fininstrmtp = 'STK'{symbol_filter}
    """
    moves: Dict[str, Dict[str, Any]] = {}
    with sqlite3.connect(NSE_EOD_DB) as conn:
        rows = conn.execute(query, params).fetchall()

    for symbol, close, prev_close in rows:
        close_f = _as_float(close)
        prev_f = _as_float(prev_close)
        if not symbol or close_f is None or not prev_f or prev_f <= 0:
            continue
        change_pct = round((close_f - prev_f) / prev_f * 100, 2)
        moves[str(symbol).upper()] = {
            "symbol": str(symbol).upper(),
            "close": close_f,
            "prev_close": prev_f,
            "change_pct": change_pct,
            "session_date": session_date,
        }
    return moves


def _leaders_for_sector(
    sector_name: str,
    moves: Dict[str, Dict[str, Any]],
    constituents: Dict[str, List[str]],
    top_n: int = 2,
) -> List[Dict[str, Any]]:
    symbols = constituents.get(sector_name) or []
    rows = [moves[s] for s in symbols if s in moves]
    if not rows:
        return []
    rows.sort(key=lambda r: r.get("change_pct") or -999, reverse=True)
    return rows[:top_n]


def _laggards_for_sector(
    sector_name: str,
    moves: Dict[str, Dict[str, Any]],
    constituents: Dict[str, List[str]],
    top_n: int = 1,
) -> List[Dict[str, Any]]:
    symbols = constituents.get(sector_name) or []
    rows = [moves[s] for s in symbols if s in moves]
    if not rows:
        return []
    rows.sort(key=lambda r: r.get("change_pct") or 999)
    return rows[:top_n]


def sector_index_streak(
    sector_name: str,
    end_date: date,
    max_days: int = 10,
) -> Dict[str, Any]:
    """
    Count consecutive up/down sessions for a sector index from archived morning_desk files.
  """
    history: List[Tuple[str, float]] = []
    for path in sorted(JOURNAL_DIR.glob("morning_desk_*.json"), reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        trade_day = str(payload.get("trade_date") or "")
        if not trade_day or trade_day > end_date.isoformat():
            continue
        sectors = (payload.get("sector_scan") or {}).get("sectors") or []
        row = next((s for s in sectors if s.get("sector") == sector_name), None)
        if not row:
            continue
        chg = _as_float(row.get("change_pct"))
        if chg is None:
            continue
        history.append((trade_day, chg))
        if len(history) >= max_days:
            break

    if not history:
        return {"streak_days": 0, "streak_type": "UNKNOWN", "label": ""}

    history.sort(key=lambda x: x[0])
    latest_chg = history[-1][1]
    streak_type = "GREEN" if latest_chg > 0 else "RED" if latest_chg < 0 else "FLAT"
    streak_days = 0
    label = ""

    if latest_chg > 0:
        red_before = 0
        for _, chg in reversed(history[:-1]):
            if chg < 0:
                red_before += 1
            else:
                break
        streak_days = red_before + 1
        if red_before >= 2:
            label = f"FIRST GREEN after {red_before} down sessions"
        elif red_before == 1:
            label = "FIRST GREEN after 1 down session"
    elif latest_chg < 0:
        for _, chg in reversed(history):
            if chg < 0:
                streak_days += 1
            else:
                break
        if streak_days >= 3:
            label = f"{streak_days} down sessions"
    else:
        for _, chg in reversed(history):
            if chg == 0:
                streak_days += 1
            else:
                break

    return {
        "streak_days": streak_days,
        "streak_type": streak_type,
        "label": label,
        "latest_change_pct": latest_chg,
    }


def format_leader_line(leaders: List[Dict[str, Any]], max_names: int = 2) -> str:
    if not leaders:
        return "—"
    parts = []
    for row in leaders[:max_names]:
        chg = _as_float(row.get("change_pct"))
        if chg is None:
            continue
        sign = "+" if chg >= 0 else ""
        parts.append(f"{row.get('symbol')} {sign}{chg:.2f}%")
    return ", ".join(parts) if parts else "—"


def _advance_decline(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Advance/decline/unchanged from constituent change_pct rows."""
    advanced = declined = unchanged = 0
    for row in rows:
        chg = _as_float(row.get("change_pct"))
        if chg is None:
            continue
        if chg > 0:
            advanced += 1
        elif chg < 0:
            declined += 1
        else:
            unchanged += 1
    total = advanced + declined + unchanged
    return {
        "advanced": advanced,
        "declined": declined,
        "unchanged": unchanged,
        "total": total,
        "advance_pct": round((advanced / total) * 100, 1) if total else 0.0,
        "decline_pct": round((declined / total) * 100, 1) if total else 0.0,
    }


def fetch_kite_stock_moves(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """Live constituent % change vs previous close from Kite."""
    from nifty.kite.spot import load_kite_optional

    kite = load_kite_optional()
    if kite is None or not symbols:
        return {}

    moves: Dict[str, Dict[str, Any]] = {}
    keys = [f"NSE:{s.upper()}" for s in symbols]
    for i in range(0, len(keys), 400):
        chunk = keys[i : i + 400]
        try:
            quotes = kite.quote(chunk) or {}
        except Exception:
            continue
        for key, raw in quotes.items():
            sym = str(key).replace("NSE:", "").upper()
            last = _as_float(raw.get("last_price"))
            ohlc = raw.get("ohlc") or {}
            prev = _as_float(ohlc.get("close"))
            if last is None or not prev or prev <= 0:
                continue
            change_pct = round((last - prev) / prev * 100, 2)
            moves[sym] = {
                "symbol": sym,
                "last": last,
                "prev_close": prev,
                "change_pct": change_pct,
                "source": "kite",
            }
    return moves


def _constituent_moves_for_sector(
    sector_name: str,
    moves: Dict[str, Dict[str, Any]],
    constituents: Dict[str, List[str]],
) -> List[Dict[str, Any]]:
    symbols = constituents.get(sector_name) or []
    return [moves[s] for s in symbols if s in moves]


def constituent_breadth_for_sector(
    sector_name: str,
    moves: Dict[str, Dict[str, Any]],
    constituents: Dict[str, List[str]],
) -> Dict[str, Any]:
    rows = _constituent_moves_for_sector(sector_name, moves, constituents)
    symbols = constituents.get(sector_name) or []
    breadth = _advance_decline(rows)
    breadth["constituents_configured"] = len(symbols)
    breadth["constituents_quoted"] = len(rows)
    breadth["missing"] = [s for s in symbols if s not in moves]
    return breadth


def build_sector_constituent_breadth(
    sector_names: Optional[List[str]] = None,
    *,
    trade_date: Optional[date] = None,
    session_date: Optional[str] = None,
    live: bool = False,
) -> Dict[str, Any]:
    """
    Advance/decline for every sector basket in config/sector_constituents.json.
    live=True -> Kite LTP vs prev close; else NSE equity bhavcopy for session_date.
    """
    constituents = _load_constituents()
    if not constituents:
        return {"error": "no_constituents_config", "sectors": {}}

    names = sector_names or list(constituents.keys())
    all_symbols = sorted({s for name in names for s in constituents.get(name, [])})

    if live:
        moves = fetch_kite_stock_moves(all_symbols)
        source = "kite_live"
        as_of = "live"
    else:
        eod_day = session_date or resolve_stock_session_date(trade_date)
        moves = load_stock_moves(eod_day or "", all_symbols) if eod_day else {}
        source = "nse_eod_sqlite.equity_bhavcopy"
        as_of = eod_day

    sectors: Dict[str, Any] = {}
    for name in names:
        if name not in constituents:
            continue
        breadth = constituent_breadth_for_sector(name, moves, constituents)
        breadth["source"] = source
        breadth["as_of"] = as_of
        sectors[name] = breadth

    return {
        "as_of": as_of,
        "source": source,
        "sectors": sectors,
        "errors": [] if moves else ["no_constituent_moves"],
    }


def _constituent_avg_change(
    sector_name: str,
    moves: Dict[str, Dict[str, Any]],
    constituents: Dict[str, List[str]],
) -> Optional[float]:
    """Equal-weight constituent % change as sector index proxy."""
    symbols = constituents.get(sector_name) or []
    changes = [_as_float(moves[s].get("change_pct")) for s in symbols if s in moves]
    changes = [c for c in changes if c is not None]
    if not changes:
        return None
    return round(sum(changes) / len(changes), 2)


def _filter_report_sectors(sector_scan: Dict[str, Any]) -> Dict[str, Any]:
    """Recompute breadth on full sector list (no FinStack subset filter).

    Relocated verbatim from quant-desk-engine v4's desk_morning_report.py
    (a 2693-line file out of scope to port wholesale for this one helper).
    """
    sectors = list(sector_scan.get("sectors") or [])
    if not sectors:
        return sector_scan
    green = sum(1 for s in sectors if (_as_float(s.get("change_pct")) or 0) > 0)
    red = sum(1 for s in sectors if (_as_float(s.get("change_pct")) or 0) < 0)
    flat = len(sectors) - green - red
    out = dict(sector_scan)
    out["sectors"] = sectors
    out["breadth"] = {
        "green": green,
        "red": red,
        "flat": flat,
        "total": len(sectors),
        "green_pct": round((green / len(sectors)) * 100, 1) if sectors else 0,
    }
    if sectors:
        out["best_performer"] = max(sectors, key=lambda r: _as_float(r.get("change_pct")) or -999)
        out["worst_performer"] = min(sectors, key=lambda r: _as_float(r.get("change_pct")) or 999)
    return out


def build_eod_sector_scan(trade_date: date) -> Dict[str, Any]:
    """End-of-session sector scan: trade_date index % + same-day bhavcopy leaders."""
    from nifty.morning.phases import analyze_sectors
    from nifty.sources.kite_sector_scan import fetch_sector_performance_eod, load_desk_sector_names

    label = trade_date.isoformat()
    sector_raw = fetch_sector_performance_eod(trade_date)
    constituents = _load_constituents()
    all_symbols = sorted({s for syms in constituents.values() for s in syms})
    moves = load_stock_moves(label, all_symbols)

    by_name = {str(row.get("sector")): dict(row) for row in sector_raw.get("sectors") or []}
    merged: List[Dict[str, Any]] = []
    for name in load_desk_sector_names():
        row = by_name.get(name) or {"sector": name}
        if _as_float(row.get("change_pct")) is None and moves:
            proxy = _constituent_avg_change(name, moves, constituents)
            if proxy is not None:
                row["change_pct"] = proxy
                row["source"] = row.get("source") or "constituent_proxy_eod"
        if _as_float(row.get("change_pct")) is not None:
            merged.append(row)

    sector_raw["sectors"] = merged
    if merged:
        sector_raw["best_performer"] = max(
            merged, key=lambda row: _as_float(row.get("change_pct")) or -999
        )
        sector_raw["worst_performer"] = min(
            merged, key=lambda row: _as_float(row.get("change_pct")) or 999
        )

    sector_scan = analyze_sectors(sector_raw)
    sector_scan = build_sector_leaders(sector_scan, session_date=label, trade_date=trade_date)
    sector_scan["sector_index_session"] = label
    sector_scan["sector_index_source"] = sector_raw.get("source")
    sector_scan["eod_sector_scan"] = True
    return _filter_report_sectors(sector_scan)


def build_sector_leaders(
    sector_scan: Dict[str, Any],
    session_date: Optional[str] = None,
    trade_date: Optional[date] = None,
    top_n: int = 2,
) -> Dict[str, Any]:
    """Attach per-sector stock leaders to sector_scan (mutates copy)."""
    result = dict(sector_scan)
    constituents = _load_constituents()
    if not constituents:
        result["sector_leaders_error"] = "no_constituents_config"
        return result

    eod_day = session_date or resolve_stock_session_date(trade_date)
    if not eod_day:
        result["sector_leaders_error"] = "no_session_date"
        return result

    all_symbols = sorted({s for syms in constituents.values() for s in syms})
    moves = load_stock_moves(eod_day, all_symbols)
    if not moves:
        result["sector_leaders_error"] = f"no_equity_moves_{eod_day}"
        result["sector_leaders_session"] = eod_day
        return result

    end_date = trade_date or date.today()
    if session_date:
        try:
            end_date = date.fromisoformat(session_date)
        except ValueError:
            pass

    leaders_by_sector: Dict[str, Any] = {}
    enriched_sectors: List[Dict[str, Any]] = []

    for sector in result.get("sectors") or []:
        name = str(sector.get("sector", ""))
        leaders = _leaders_for_sector(name, moves, constituents, top_n=top_n)
        laggards = _laggards_for_sector(name, moves, constituents, top_n=1)
        streak = sector_index_streak(name, end_date)

        sector_row = dict(sector)
        sector_row["leaders"] = leaders
        sector_row["laggard"] = laggards[0] if laggards else None
        sector_row["leader_line"] = format_leader_line(leaders)
        sector_row["streak"] = streak
        sector_row["constituent_breadth"] = constituent_breadth_for_sector(name, moves, constituents)
        sector_row["constituent_breadth"]["source"] = result.get("sector_leaders_source", "nse_eod_sqlite.equity_bhavcopy")
        sector_row["constituent_breadth"]["as_of"] = eod_day
        enriched_sectors.append(sector_row)

        leaders_by_sector[name] = {
            "leaders": leaders,
            "laggard": laggards[0] if laggards else None,
            "leader_line": format_leader_line(leaders),
            "streak": streak,
            "constituent_breadth": sector_row["constituent_breadth"],
        }

    result["sectors"] = enriched_sectors
    result["sector_leaders"] = leaders_by_sector
    result["sector_leaders_session"] = eod_day
    result["sector_leaders_source"] = "nse_eod_sqlite.equity_bhavcopy"
    return result


def enrich_sector_scan_with_leaders(
    sector_scan: Dict[str, Any],
    *,
    session_date: Optional[str] = None,
    trade_date: Optional[date] = None,
    top_n: int = 2,
    live_constituents: bool = False,
) -> Dict[str, Any]:
    has_leaders = sector_scan.get("sector_leaders_session") and sector_scan.get("sectors") and any(
        s.get("leaders") for s in sector_scan.get("sectors") or []
    )
    has_breadth = any(s.get("constituent_breadth") for s in sector_scan.get("sectors") or [])

    if has_leaders and has_breadth and not live_constituents:
        return sector_scan

    if has_leaders and not has_breadth:
        result = dict(sector_scan)
        constituents = _load_constituents()
        eod_day = session_date or resolve_stock_session_date(trade_date)
        all_symbols = sorted({s for syms in constituents.values() for s in syms})
        moves = (
            fetch_kite_stock_moves(all_symbols)
            if live_constituents
            else load_stock_moves(eod_day or "", all_symbols)
        )
        enriched: List[Dict[str, Any]] = []
        for sector in result.get("sectors") or []:
            row = dict(sector)
            name = str(row.get("sector", ""))
            if name in constituents and moves:
                cb = constituent_breadth_for_sector(name, moves, constituents)
                cb["source"] = "kite_live" if live_constituents else "nse_eod_sqlite.equity_bhavcopy"
                cb["as_of"] = "live" if live_constituents else eod_day
                row["constituent_breadth"] = cb
            enriched.append(row)
        result["sectors"] = enriched
        result["constituent_breadth_live"] = live_constituents
        return result

    result = build_sector_leaders(
        sector_scan,
        session_date=session_date,
        trade_date=trade_date,
        top_n=top_n,
    )
    if live_constituents:
        constituents = _load_constituents()
        all_symbols = sorted({s for syms in constituents.values() for s in syms})
        live_moves = fetch_kite_stock_moves(all_symbols)
        if live_moves:
            enriched = []
            for sector in result.get("sectors") or []:
                row = dict(sector)
                name = str(row.get("sector", ""))
                cb = constituent_breadth_for_sector(name, live_moves, constituents)
                cb["source"] = "kite_live"
                cb["as_of"] = "live"
                row["constituent_breadth"] = cb
                if live_constituents:
                    row["leaders"] = _leaders_for_sector(name, live_moves, constituents, top_n=top_n)
                    laggards = _laggards_for_sector(name, live_moves, constituents, top_n=1)
                    row["laggard"] = laggards[0] if laggards else row.get("laggard")
                    row["leader_line"] = format_leader_line(row["leaders"])
                enriched.append(row)
            result["sectors"] = enriched
            result["constituent_breadth_live"] = True
    return result


def _selftest() -> None:
    import tempfile

    global JOURNAL_DIR, NSE_EOD_DB, SECTOR_CONSTITUENTS_PATH
    tmp = Path(tempfile.mkdtemp(prefix="sector-leaders-selftest-"))
    original_journal_dir = JOURNAL_DIR
    original_db = NSE_EOD_DB
    original_constituents = SECTOR_CONSTITUENTS_PATH
    try:
        JOURNAL_DIR = tmp
        NSE_EOD_DB = tmp / "nse_eod.sqlite"
        SECTOR_CONSTITUENTS_PATH = tmp / "sector_constituents.json"

        # No constituents config -> graceful error dict, never raises.
        assert build_sector_constituent_breadth() == {"error": "no_constituents_config", "sectors": {}}
        no_config = build_sector_leaders({"sectors": [{"sector": "IT"}]})
        assert no_config["sector_leaders_error"] == "no_constituents_config"

        SECTOR_CONSTITUENTS_PATH.write_text(
            json.dumps({"IT": ["TCS", "INFY"], "BANKING": ["HDFCBANK", "ICICIBANK"]}), encoding="utf-8"
        )
        assert _load_constituents() == {"IT": ["TCS", "INFY"], "BANKING": ["HDFCBANK", "ICICIBANK"]}

        # No sqlite db -> empty moves, still graceful.
        assert load_stock_moves("2026-07-21", ["TCS"]) == {}

        conn = sqlite3.connect(NSE_EOD_DB)
        conn.execute(
            "CREATE TABLE equity_bhavcopy (trade_date TEXT, tckrsymb TEXT, fininstrmtp TEXT, clspric REAL, prvsclsgpric REAL)"
        )
        conn.executemany(
            "INSERT INTO equity_bhavcopy VALUES (?, ?, 'STK', ?, ?)",
            [
                ("2026-07-21", "TCS", 4100.0, 4000.0),
                ("2026-07-21", "INFY", 1500.0, 1550.0),
                ("2026-07-21", "HDFCBANK", 1700.0, 1690.0),
                ("2026-07-21", "ICICIBANK", 1200.0, 1210.0),
            ],
        )
        conn.commit()
        conn.close()

        moves = load_stock_moves("2026-07-21", ["TCS", "INFY", "HDFCBANK", "ICICIBANK"])
        assert moves["TCS"]["change_pct"] == 2.5
        assert moves["INFY"]["change_pct"] == round((1500 - 1550) / 1550 * 100, 2)

        leaders = _leaders_for_sector("IT", moves, _load_constituents())
        assert leaders[0]["symbol"] == "TCS"  # TCS up 2.5%, INFY down -> TCS leads

        laggards = _laggards_for_sector("IT", moves, _load_constituents())
        assert laggards[0]["symbol"] == "INFY"

        line = format_leader_line(leaders)
        assert "TCS" in line

        breadth = constituent_breadth_for_sector("IT", moves, _load_constituents())
        assert breadth["advanced"] == 1 and breadth["declined"] == 1
        assert breadth["constituents_configured"] == 2

        avg = _constituent_avg_change("BANKING", moves, _load_constituents())
        assert avg is not None

        sector_scan = build_sector_leaders(
            {"sectors": [{"sector": "IT"}, {"sector": "BANKING"}]},
            session_date="2026-07-21",
        )
        assert sector_scan["sector_leaders_session"] == "2026-07-21"
        it_leaders = sector_scan["sector_leaders"]["IT"]
        assert it_leaders["leader_line"]

        filtered = _filter_report_sectors({"sectors": [{"sector": "IT", "change_pct": 1.5}, {"sector": "BANKING", "change_pct": -0.5}]})
        assert filtered["breadth"]["green"] == 1 and filtered["breadth"]["red"] == 1
        assert filtered["best_performer"]["sector"] == "IT"

        # sector_index_streak: no morning_desk journals -> UNKNOWN, never raises.
        streak = sector_index_streak("IT", date(2026, 7, 21))
        assert streak["streak_type"] == "UNKNOWN"

        breadth_summary = build_sector_constituent_breadth(session_date="2026-07-21")
        assert "IT" in breadth_summary["sectors"]
    finally:
        JOURNAL_DIR = original_journal_dir
        NSE_EOD_DB = original_db
        SECTOR_CONSTITUENTS_PATH = original_constituents

    print("[sources.sector_leaders] selftest OK: constituents, leaders/laggards, breadth, streak, filtering")


if __name__ == "__main__":
    _selftest()
