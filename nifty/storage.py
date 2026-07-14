"""Slim tick sink: write-on-change, batched, with 1-minute candles.

Measured against the 2026-06-19 archive: the legacy per-tick store writes
610 MB/day, of which 435 MB is 5-level depth JSON no decision reads, and
99.5% of option rows repeat the previous OI. This sink stores one row per
material change (oi, ltp or volume moved) plus top-of-book, and rolls
1-minute candles - 26 MB/day measured on the same data, 23x smaller, with
the indexes the replay/backtest queries always needed.

Runs ALONGSIDE the legacy store during the dual-write window; compare the
two with `python tests/verify_slim.py <day>` after a live day, and retire
the legacy writer after three clean days (Migration Phase 3).

Hot-path contract: on_option_tick()/on_spot_tick() only enqueue - the
change filter, batching (200 rows / 2 s, the proven pattern from the DHAN
project's TickStore) and candle upserts all happen on the writer thread.

Self-check: python -m nifty.storage
"""

from __future__ import annotations

import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

BATCH_SIZE = 200
FLUSH_SECONDS = 2.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS instrument (
    id            INTEGER PRIMARY KEY,
    token         INTEGER UNIQUE NOT NULL,
    tradingsymbol TEXT NOT NULL,
    strike        INTEGER NOT NULL,
    option_type   TEXT NOT NULL,
    expiry        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tick (
    ts            INTEGER NOT NULL,
    instrument_id INTEGER NOT NULL,
    ltp           REAL,
    oi            INTEGER,
    volume        INTEGER,
    bid           REAL,
    ask           REAL
);
CREATE INDEX IF NOT EXISTS ix_tick ON tick(instrument_id, ts);
CREATE TABLE IF NOT EXISTS spot_tick (
    ts  INTEGER NOT NULL,
    ltp REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_spot ON spot_tick(ts);
CREATE TABLE IF NOT EXISTS candle_1m (
    instrument_id INTEGER NOT NULL,
    ts            INTEGER NOT NULL,
    o REAL, h REAL, l REAL, c REAL,
    volume INTEGER, oi INTEGER,
    PRIMARY KEY (instrument_id, ts)
);
"""

_CANDLE_UPSERT = """
INSERT INTO candle_1m (instrument_id, ts, o, h, l, c, volume, oi)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(instrument_id, ts) DO UPDATE SET
    h = MAX(h, excluded.h),
    l = MIN(l, excluded.l),
    c = excluded.c,
    volume = excluded.volume,
    oi = excluded.oi
"""


class SlimTickStore:
    """Queue-fed writer thread; never blocks the market-feed path."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.queue: "queue.Queue[Tuple[Any, ...]]" = queue.Queue()
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self._token_ids: Dict[int, int] = {}
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    def register_instruments(self, items: Iterable[Any]) -> None:
        """Upsert instrument rows; build the token -> id map used by ticks."""
        with sqlite3.connect(self.db_path) as conn:
            for item in items:
                conn.execute(
                    "INSERT OR IGNORE INTO instrument"
                    " (token, tradingsymbol, strike, option_type, expiry)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        int(item.token),
                        str(item.tradingsymbol),
                        int(item.strike),
                        str(item.option_type),
                        str(item.expiry),
                    ),
                )
            for token, iid in conn.execute("SELECT token, id FROM instrument"):
                self._token_ids[int(token)] = int(iid)

    # ── hot path: enqueue only ────────────────────────────────────────────
    def on_option_tick(
        self, token: int, ts: int, ltp: float, oi: int, volume: int,
        bid: Optional[float], ask: Optional[float],
    ) -> None:
        try:
            self.queue.put_nowait(("opt", token, ts, ltp, oi, volume, bid, ask))
        except queue.Full:  # pragma: no cover - unbounded by default
            pass

    def on_spot_tick(self, ts: int, ltp: float) -> None:
        try:
            self.queue.put_nowait(("spot", ts, ltp))
        except queue.Full:  # pragma: no cover
            pass

    # ── writer thread ─────────────────────────────────────────────────────
    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True, name="slim-tick-writer")
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=10)

    def _run(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        last_opt: Dict[int, Tuple[Any, Any, Any]] = {}   # token -> (oi, ltp, volume)
        last_spot_ltp: Optional[float] = None
        ticks: list = []
        spots: list = []
        candles: list = []
        last_flush = time.monotonic()

        def flush() -> None:
            nonlocal ticks, spots, candles, last_flush
            if ticks:
                conn.executemany(
                    "INSERT INTO tick (ts, instrument_id, ltp, oi, volume, bid, ask)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ticks,
                )
            if spots:
                conn.executemany("INSERT INTO spot_tick (ts, ltp) VALUES (?, ?)", spots)
            if candles:
                conn.executemany(_CANDLE_UPSERT, candles)
            if ticks or spots or candles:
                conn.commit()
            ticks, spots, candles = [], [], []
            last_flush = time.monotonic()

        while not self.stop_event.is_set() or not self.queue.empty():
            try:
                row = self.queue.get(timeout=FLUSH_SECONDS)
            except queue.Empty:
                row = None
            if row is not None:
                if row[0] == "opt":
                    _, token, ts, ltp, oi, volume, bid, ask = row
                    iid = self._token_ids.get(int(token))
                    if iid is not None and last_opt.get(token) != (oi, ltp, volume):
                        last_opt[token] = (oi, ltp, volume)
                        ticks.append((ts, iid, ltp, oi, volume, bid, ask))
                        bucket = ts - ts % 60
                        candles.append((iid, bucket, ltp, ltp, ltp, ltp, volume, oi))
                else:
                    _, ts, ltp = row
                    if ltp != last_spot_ltp:
                        last_spot_ltp = ltp
                        spots.append((ts, ltp))
            if (
                len(ticks) + len(spots) + len(candles) >= BATCH_SIZE
                or time.monotonic() - last_flush >= FLUSH_SECONDS
            ):
                flush()

        flush()
        conn.close()


def _selftest() -> None:
    import tempfile
    from types import SimpleNamespace

    day_dir = Path(tempfile.mkdtemp(prefix="slim-selftest-"))
    store = SlimTickStore(day_dir / "slim.sqlite")
    store.register_instruments([
        SimpleNamespace(token=1, tradingsymbol="TESTCE", strike=100, option_type="CE", expiry="2026-01-01"),
    ])
    store.start()
    base = 1_700_000_000
    store.on_option_tick(1, base + 0, ltp=10.0, oi=100, volume=5, bid=9.9, ask=10.1)
    store.on_option_tick(1, base + 1, ltp=10.0, oi=100, volume=5, bid=9.9, ask=10.1)  # dup -> filtered
    store.on_option_tick(1, base + 2, ltp=12.0, oi=110, volume=6, bid=11.9, ask=12.1)
    store.on_option_tick(1, base + 61, ltp=8.0, oi=110, volume=7, bid=7.9, ask=8.1)   # next minute
    store.on_spot_tick(base + 0, 23000.0)
    store.on_spot_tick(base + 1, 23000.0)  # dup -> filtered
    store.on_spot_tick(base + 2, 23010.0)
    store.stop()

    conn = sqlite3.connect(day_dir / "slim.sqlite")
    assert conn.execute("SELECT COUNT(*) FROM tick").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM spot_tick").fetchone()[0] == 2
    c1, c2 = conn.execute(
        "SELECT o, h, l, c, volume, oi FROM candle_1m ORDER BY ts"
    ).fetchall()
    assert c1 == (10.0, 12.0, 10.0, 12.0, 6, 110), c1
    assert c2 == (8.0, 8.0, 8.0, 8.0, 7, 110), c2
    conn.close()
    print("[storage] selftest OK: change-filter, spot dedup, candle o/h/l/c")


if __name__ == "__main__":
    _selftest()
