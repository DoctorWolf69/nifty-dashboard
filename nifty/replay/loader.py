"""Read an archived tick day back into the shapes the live engine consumes.

The per-day SQLite files (`data/live_nifty_oi/nifty_oi_ticks_YYYY-MM-DD.sqlite`)
store one row per tick. Here we:
  - reconstruct the KiteTicker-style tick dicts `OIVelocityState.update_ticks`
    expects (so replay feeds the SAME engine), and
  - expose pandas DataFrames for ad-hoc backtesting.

Timestamps are server-local IST. Ticks before 09:15 are pre-open/stale quote
snapshots, so we default the window to the real session 09:15:00–15:30:59.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from nifty.paths import DATA_LIVE_OI
from nifty.dashboard.state import InstrumentState, NIFTY_SPOT_TOKEN

SESSION_START = "09:15:00"
SESSION_END = "15:30:59"


def db_path(day: str) -> Path:
    return DATA_LIVE_OI / f"nifty_oi_ticks_{day}.sqlite"


def available_days() -> List[str]:
    """Sorted YYYY-MM-DD strings that have an archived tick DB."""
    days = []
    for p in DATA_LIVE_OI.glob("nifty_oi_ticks_*.sqlite"):
        stem = p.stem.replace("nifty_oi_ticks_", "")
        try:
            datetime.strptime(stem, "%Y-%m-%d")
        except ValueError:
            continue
        days.append(stem)
    return sorted(days)


def _connect(day: str) -> sqlite3.Connection:
    path = db_path(day)
    if not path.exists():
        raise FileNotFoundError(f"No tick archive for {day}: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_depth(raw: Any) -> List[Dict[str, Any]]:
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []


def load_instruments(day: str) -> List[InstrumentState]:
    """Reconstruct the option InstrumentState list from the day's distinct contracts."""
    conn = _connect(day)
    try:
        rows = conn.execute(
            """
            SELECT token, tradingsymbol, strike, option_type, expiry
            FROM option_ticks GROUP BY token
            ORDER BY strike, option_type
            """
        ).fetchall()
    finally:
        conn.close()
    return [
        InstrumentState(
            token=int(r["token"]),
            tradingsymbol=str(r["tradingsymbol"]),
            strike=int(r["strike"]),
            option_type=str(r["option_type"]),
            expiry=str(r["expiry"]),
        )
        for r in rows
    ]


def day_expiry(day: str) -> str:
    conn = _connect(day)
    try:
        row = conn.execute("SELECT expiry FROM option_ticks LIMIT 1").fetchone()
    finally:
        conn.close()
    return str(row["expiry"]) if row else ""


def _option_tick(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "instrument_token": int(row["token"]),
        "last_price": row["ltp"],
        "oi": row["oi"],
        "volume_traded": row["volume"],
        "last_traded_quantity": row["last_quantity"],
        "total_buy_quantity": row["total_buy_quantity"],
        "total_sell_quantity": row["total_sell_quantity"],
        "depth": {
            "buy": _parse_depth(row["depth_buy_json"]),
            "sell": _parse_depth(row["depth_sell_json"]),
        },
    }


def _spot_tick(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "instrument_token": NIFTY_SPOT_TOKEN,
        "last_price": row["ltp"],
        "ohlc": {
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["prev_close"],
        },
    }


def iter_tick_groups(
    day: str,
    after_ts: Optional[str] = None,
    until_ts: Optional[str] = None,
    start: str = SESSION_START,
    end: str = SESSION_END,
) -> Iterator[Tuple[str, datetime, List[Dict[str, Any]]]]:
    """Yield (ts_str, ts_dt, [tick_dicts]) grouped by second, in time order.

    Spot + option ticks sharing the same timestamp are merged into one group so a
    single update_ticks() call advances both spot and chain together. `after_ts`
    (exclusive) / `until_ts` (inclusive) bound the window for incremental seeks.
    """
    lo = f"{day} {start}"
    hi = f"{day} {end}"
    if after_ts is not None and after_ts > lo:
        lo_clause, lo_val = "ts > ?", after_ts
    else:
        lo_clause, lo_val = "ts >= ?", lo
    hi_val = min(hi, until_ts) if until_ts is not None else hi

    conn = _connect(day)
    try:
        opt_cur = conn.execute(
            f"SELECT * FROM option_ticks WHERE {lo_clause} AND ts <= ? ORDER BY ts",
            (lo_val, hi_val),
        )
        spot_cur = conn.execute(
            f"SELECT * FROM spot_ticks WHERE {lo_clause} AND ts <= ? ORDER BY ts",
            (lo_val, hi_val),
        )
        opt_row = opt_cur.fetchone()
        spot_row = spot_cur.fetchone()
        current_ts: Optional[str] = None
        bucket: List[Dict[str, Any]] = []

        def _next_ts() -> Optional[str]:
            candidates = []
            if opt_row is not None:
                candidates.append(opt_row["ts"])
            if spot_row is not None:
                candidates.append(spot_row["ts"])
            return min(candidates) if candidates else None

        while True:
            nts = _next_ts()
            if nts is None:
                break
            if current_ts is not None and nts != current_ts and bucket:
                yield current_ts, datetime.strptime(current_ts[:19], "%Y-%m-%d %H:%M:%S"), bucket
                bucket = []
            current_ts = nts
            # spot first (engine updates spot before chain in the live path)
            while spot_row is not None and spot_row["ts"] == current_ts:
                bucket.append(_spot_tick(spot_row))
                spot_row = spot_cur.fetchone()
            while opt_row is not None and opt_row["ts"] == current_ts:
                bucket.append(_option_tick(opt_row))
                opt_row = opt_cur.fetchone()
        if bucket and current_ts is not None:
            yield current_ts, datetime.strptime(current_ts[:19], "%Y-%m-%d %H:%M:%S"), bucket
    finally:
        conn.close()


def option_price_path(day: str, tradingsymbol: str, start_ts: str, end_ts: str) -> List[Tuple[str, float]]:
    """(ts, ltp) for one contract between two timestamps — for MFE/MAE in the backtest."""
    conn = _connect(day)
    try:
        rows = conn.execute(
            "SELECT ts, ltp FROM option_ticks WHERE tradingsymbol = ? AND ts >= ? AND ts <= ? ORDER BY ts",
            (tradingsymbol, start_ts, end_ts),
        ).fetchall()
    finally:
        conn.close()
    return [(str(r["ts"]), float(r["ltp"])) for r in rows if r["ltp"] is not None]
