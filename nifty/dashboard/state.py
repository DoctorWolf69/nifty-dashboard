#!/usr/bin/env python3
"""
Live NIFTY OI velocity dashboard.

Desk philosophy:
  - Morning bias (macro, sectors, GIFT) is context — it sets the watchlist.
  - Participant OI velocity at key areas is truth — it makes the trade call.
  - When bias and live participant action conflict, follow the participants.

Requires Kite Connect credentials in the environment:
  KITE_API_KEY=...
  KITE_API_SECRET=...
  KITE_ACCESS_TOKEN=...  (optional; use /kite/login if missing)

Read-only dashboard: streams NIFTY weekly option ticks, computes OI velocity,
flags sustained writer-adds at key areas, and logs paper trade decisions.
Start at 9:15 IST open — not mid-session.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import threading
import sqlite3
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple

from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

from nifty.core.sessions import (
    TECH_LEVEL_TOLERANCE,
    build_session_context,
    near_technical_level,
)
from nifty.core.commission import (
    CommissionConfig,
    commission_conviction_check,
    enrich_signal_with_commission,
    net_pnl_rupees,
)
from nifty.kite.spot import classify_open_gap, GAP_THRESHOLD, MILD_GAP_THRESHOLD
from nifty.morning.context import LiveMorningContext
from nifty.core.journal import NiftyJournalStore
from nifty.analytics.options import IVHistoryStore, analyze_option_chain
from nifty.analytics.futures import (
    build_futures_layer,
    evaluate_fut_opt_alignment,
    load_eod_futures_context,
    previous_trading_day,
)
from nifty.analytics.confluence import (
    TRADE_MIN_CONFLUENCE,
    score_signal_candidate,
)

try:
    from kiteconnect import KiteConnect, KiteTicker
except ImportError as exc:  # pragma: no cover - runtime setup check
    raise SystemExit(
        "Missing dependency: kiteconnect. Run `pip install kiteconnect` or "
        "`pip install -r requirements.txt` after adding it."
    ) from exc


NSE_NIFTY_SYMBOL = "NSE:NIFTY 50"
NIFTY_LOT_SIZE = 65
from nifty.paths import ENV_FILE, SIGNAL_JOURNAL_FILE, DATA_LIVE_OI as DATA_DIR
NIFTY_SPOT_TOKEN = 256265
KEY_AREA_DISTANCE_PCT = 0.35
SUSTAINED_ADD_MINUTES = 3
MIN_POSITIVE_MINUTE_ADDS = 3
MIN_VOLUME_CONFIRMED_MINUTES = 3
MAX_OPEN_SIGNALS = 1
MAX_SIGNAL_STRIKE_DISTANCE_PTS = 150.0
MIN_OPEN_STRIKE_SPACING = 100
SINGLE_DIRECTION_BOOK = True
BLOCK_SAME_THESIS_STACK = True
REQUIRE_PE_SPOT_CONFIRM_FOR_BUY_CE = True
REQUIRE_SPOT_WEAK_FOR_BUY_PE = True
GAMMA_NEAR_SPOT_PCT = 0.45
GAMMA_HEAVY_OI_MIN = 1_000_000
GAMMA_UNWIND_DELTA_MIN = 200_000
LATE_SESSION_SIGNAL_CUTOFF = (15, 15)  # No fresh paper signals after 15:15 IST
from nifty.core.expiry import (
    build_expiry_session_rules,
    is_expiry_session,
    is_no_trade_window,
    is_nifty_options_blocked,
    no_trade_seconds_remaining,
    no_trade_window_label,
)
def nearest(value: float, step: int) -> int:
    return int(round(value / step) * step)

GAP_PLAYBOOK_THRESHOLD = 30  # points vs prev close
PLAYBOOK_VELOCITY_ADD_PCT = 2.0
PLAYBOOK_VELOCITY_UNWIND_PCT = -2.0
PLAYBOOK_SPOT_FLAT_PTS = 8.0  # spot 5m move within +/- this = flat

# --- Dynamic trade management (mentor spec) -------------------------------
# Live conviction for an open position is derived (in the trade-management
# layer, not the conviction engine) from the OI-conviction level so it can be
# tracked tick-by-tick against its peak. Scores are 0..100.
CONVICTION_LEVEL_SCORE = {"STRONG": 100, "NEUTRAL": 50, "WEAK": 25, "INVALIDATED": 0}
CONVICTION_FADE_DROP = 30      # points below peak that counts as "weakened"
CONVICTION_FADE_STREAK = 2     # consecutive weakened updates -> exit
CONFIRMATION_LOST_MIN = 2      # >= this many entry confirmation factors lost -> exit
VELOCITY_1M_SEC = 60
VELOCITY_5M_SEC = 300
VELOCITY_15M_SEC = 900
PLAYBOOK_WATCH_STRIKES = (23100, 23200)

DESK_PRINCIPLES = {
    "headline": "Bias is context. Participant action is truth.",
    "rules": [
        "Morning desk sets the watchlist — macro, sectors, GIFT, key levels.",
        "Live OI velocity makes the trade call — sustained adds at key areas with volume.",
        "When bias and participants conflict, follow the participants — trade highest conviction.",
        "Record every signal candidate with confluence score — journal first, paper only at aligned score.",
        "9:15–9:30 IST is ORB watch only — no fresh paper entries until the opening range is set.",
        "Expiry day: observe until 9:45 — BankNifty primary; Nifty options only after 9:45.",
        "No fresh paper signals after 15:15 IST unless managing an open position.",
        "Start this dashboard at 9:15 open to capture ORB-low participant behavior.",
        "Every signal must cover round-trip commission with minimum net conviction.",
        "Max 1 open paper signal — no stacking; if PE support thesis is live, hold it until stop/target.",
        "BUY_CE only when PE OI adds with spot rising; BUY_PE only when spot is flat/falling.",
        "Open positions: re-check OI velocity every tick — STRONG / WEAK / INVALIDATED vs thesis.",
    ],
}


from nifty.dashboard.clock import CLOCK


def ist_now() -> str:
    return CLOCK.now_str()


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _is_expiry_day(state: "VelocityState") -> bool:
    today = CLOCK.today().isoformat()
    if state.expiry and str(state.expiry)[:10] == today:
        return True
    return is_expiry_session(trade_date=CLOCK.today())


def orb_no_trade_seconds_remaining(now: Optional[datetime] = None, *, is_expiry: bool = False) -> int:
    return no_trade_seconds_remaining(now, is_expiry=is_expiry)


def is_late_session(now: Optional[datetime] = None) -> bool:
    current = now or CLOCK.now()
    cutoff_h, cutoff_m = LATE_SESSION_SIGNAL_CUTOFF
    return (current.hour, current.minute) >= (cutoff_h, cutoff_m)


class LiveDataStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.lock = threading.RLock()
        self.conn: Optional[sqlite3.Connection] = None

    def connect(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS option_ticks (
                ts TEXT NOT NULL,
                token INTEGER NOT NULL,
                tradingsymbol TEXT NOT NULL,
                strike INTEGER NOT NULL,
                option_type TEXT NOT NULL,
                expiry TEXT NOT NULL,
                ltp REAL,
                oi INTEGER,
                volume INTEGER,
                last_quantity INTEGER,
                total_buy_quantity INTEGER,
                total_sell_quantity INTEGER,
                best_bid REAL,
                best_bid_qty INTEGER,
                best_bid_orders INTEGER,
                best_ask REAL,
                best_ask_qty INTEGER,
                best_ask_orders INTEGER,
                depth_buy_json TEXT,
                depth_sell_json TEXT
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS spot_ticks (
                ts TEXT NOT NULL,
                token INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                ltp REAL,
                open REAL,
                high REAL,
                low REAL,
                prev_close REAL
            )
            """
        )
        self.conn.commit()

    def insert_option_tick(self, item: "InstrumentState") -> None:
        if self.conn is None:
            return
        best_bid = item.depth_buy[0] if item.depth_buy else {}
        best_ask = item.depth_sell[0] if item.depth_sell else {}
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO option_ticks (
                    ts, token, tradingsymbol, strike, option_type, expiry,
                    ltp, oi, volume, last_quantity, total_buy_quantity, total_sell_quantity,
                    best_bid, best_bid_qty, best_bid_orders,
                    best_ask, best_ask_qty, best_ask_orders,
                    depth_buy_json, depth_sell_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.updated_at,
                    item.token,
                    item.tradingsymbol,
                    item.strike,
                    item.option_type,
                    item.expiry,
                    item.last_price,
                    item.oi,
                    item.volume,
                    item.last_quantity,
                    item.total_buy_quantity,
                    item.total_sell_quantity,
                    as_float(best_bid.get("price")),
                    as_int(best_bid.get("quantity")),
                    as_int(best_bid.get("orders")),
                    as_float(best_ask.get("price")),
                    as_int(best_ask.get("quantity")),
                    as_int(best_ask.get("orders")),
                    json.dumps(item.depth_buy, separators=(",", ":")),
                    json.dumps(item.depth_sell, separators=(",", ":")),
                ),
            )
            self.conn.commit()

    def insert_spot_tick(
        self,
        ts: str,
        ltp: float,
        open_price: float,
        high: float,
        low: float,
        prev_close: float,
    ) -> None:
        if self.conn is None:
            return
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO spot_ticks (
                    ts, token, symbol, ltp, open, high, low, prev_close
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ts, NIFTY_SPOT_TOKEN, "NIFTY 50", ltp, open_price, high, low, prev_close),
            )
            self.conn.commit()

    def query_orb_range(self, trade_date: date) -> Tuple[Optional[float], Optional[float]]:
        """ORB high/low from spot ticks recorded 09:15–09:30 IST."""
        if self.conn is None:
            return None, None
        label = trade_date.isoformat()
        start = f"{label} 09:15:00"
        end = f"{label} 09:30:59"
        with self.lock:
            row = self.conn.execute(
                """
                SELECT MAX(ltp), MIN(ltp) FROM spot_ticks
                WHERE ts >= ? AND ts <= ? AND ltp > 0
                """,
                (start, end),
            ).fetchone()
        if not row or row[0] is None or row[1] is None:
            return None, None
        return float(row[0]), float(row[1])


@dataclass
class TickPoint:
    ts: float
    oi: int
    price: float
    volume: int


@dataclass
class InstrumentState:
    token: int
    tradingsymbol: str
    strike: int
    option_type: str
    expiry: str
    series_role: str = ""
    last_price: float = 0.0
    oi: int = 0
    volume: int = 0
    last_quantity: int = 0
    total_buy_quantity: int = 0
    total_sell_quantity: int = 0
    depth_buy: List[Dict[str, Any]] = field(default_factory=list)
    depth_sell: List[Dict[str, Any]] = field(default_factory=list)
    updated_at: str = ""
    history: Deque[TickPoint] = field(default_factory=lambda: deque(maxlen=1800))

    def update_from_tick(self, tick: Dict[str, Any]) -> None:
        now = CLOCK.time()
        self.last_price = as_float(tick.get("last_price"), self.last_price)
        self.oi = as_int(tick.get("oi"), self.oi)
        self.volume = as_int(tick.get("volume_traded"), self.volume)
        self.last_quantity = as_int(tick.get("last_traded_quantity"), self.last_quantity)
        self.total_buy_quantity = as_int(tick.get("total_buy_quantity"), self.total_buy_quantity)
        self.total_sell_quantity = as_int(tick.get("total_sell_quantity"), self.total_sell_quantity)
        depth = tick.get("depth") or {}
        self.depth_buy = list(depth.get("buy") or [])[:5]
        self.depth_sell = list(depth.get("sell") or [])[:5]
        self.updated_at = ist_now()
        self.history.append(TickPoint(ts=now, oi=self.oi, price=self.last_price, volume=self.volume))

    def _point_at_or_before(self, seconds_back: int) -> Optional[TickPoint]:
        if not self.history:
            return None
        cutoff = CLOCK.time() - seconds_back
        candidate: Optional[TickPoint] = None
        for point in self.history:
            if point.ts <= cutoff:
                candidate = point
            else:
                break
        return candidate or self.history[0]

    def velocity(self, seconds_back: int) -> Dict[str, Any]:
        old = self._point_at_or_before(seconds_back)
        if old is None or old.oi <= 0:
            return {"delta": 0, "pct": 0.0, "price_delta": 0.0, "volume_delta": 0}
        delta = self.oi - old.oi
        pct = (delta / old.oi) * 100
        return {
            "delta": delta,
            "pct": round(pct, 2),
            "price_delta": round(self.last_price - old.price, 2),
            "volume_delta": self.volume - old.volume,
        }

    def recent_minute_deltas(self, count: int = 5) -> List[Dict[str, Any]]:
        points = list(self.history)
        if len(points) < 2:
            return []
        bucketed: Dict[int, TickPoint] = {}
        for point in points:
            minute = int(point.ts // 60)
            bucketed[minute] = point
        ordered = [bucketed[key] for key in sorted(bucketed)]
        deltas: List[Dict[str, Any]] = []
        for previous, current in zip(ordered, ordered[1:]):
            oi_delta = current.oi - previous.oi
            volume_delta = current.volume - previous.volume
            price_delta = current.price - previous.price
            pct = (oi_delta / previous.oi * 100) if previous.oi else 0.0
            deltas.append(
                {
                    "oi_delta": oi_delta,
                    "volume_delta": volume_delta,
                    "price_delta": round(price_delta, 2),
                    "pct": round(pct, 2),
                }
            )
        return deltas[-count:]

    @staticmethod
    def _depth_summary(levels: List[Dict[str, Any]]) -> Dict[str, Any]:
        total_qty = sum(as_int(level.get("quantity")) for level in levels)
        total_orders = sum(as_int(level.get("orders")) for level in levels)
        avg_qty_per_order = total_qty / total_orders if total_orders else 0.0
        largest = max((as_int(level.get("quantity")) for level in levels), default=0)
        return {
            "total_qty": total_qty,
            "orders": total_orders,
            "avg_qty_per_order": round(avg_qty_per_order, 2),
            "largest_qty": largest,
        }

    def snapshot(self) -> Dict[str, Any]:
        v1 = self.velocity(VELOCITY_1M_SEC)
        v5 = self.velocity(VELOCITY_5M_SEC)
        v15 = self.velocity(VELOCITY_15M_SEC)
        buy_depth = self._depth_summary(self.depth_buy)
        sell_depth = self._depth_summary(self.depth_sell)
        return {
            "token": self.token,
            "tradingsymbol": self.tradingsymbol,
            "strike": self.strike,
            "option_type": self.option_type,
            "series_role": self.series_role,
            "expiry": self.expiry,
            "last_price": self.last_price,
            "oi": self.oi,
            "volume": self.volume,
            "last_quantity": self.last_quantity,
            "total_buy_quantity": self.total_buy_quantity,
            "total_sell_quantity": self.total_sell_quantity,
            "velocity_1m": v1,
            "velocity_5m": v5,
            "velocity_15m": v15,
            "recent_1m_deltas": self.recent_minute_deltas(),
            "depth_buy": buy_depth,
            "depth_sell": sell_depth,
            "top_buy": self.depth_buy,
            "top_sell": self.depth_sell,
            "updated_at": self.updated_at,
        }

    def chart_series(self, max_points: int = 360) -> List[Dict[str, Any]]:
        points = list(self.history)[-max_points:]
        series: List[Dict[str, Any]] = []
        for point in points:
            series.append(
                {
                    "ts": point.ts,
                    "time": datetime.fromtimestamp(point.ts).strftime("%H:%M:%S"),
                    "price": round(point.price, 2),
                    "oi": point.oi,
                    "volume": point.volume,
                }
            )
        return series


class OIVelocityState:
    def __init__(
        self,
        data_store: Optional[LiveDataStore] = None,
        replay_dir: Optional[Path] = None,
    ) -> None:
        # replay_dir set => historical replay: journal + IV history are redirected
        # to a throwaway dir and tick persistence is disabled, so the live journal
        # and tick SQLite are never touched.
        self.replay_dir = replay_dir
        self.lock = threading.RLock()
        self.data_store = None if replay_dir is not None else data_store
        self.instruments: Dict[int, InstrumentState] = {}
        self.futures: Dict[int, InstrumentState] = {}
        self.futures_layer: Dict[str, Any] = {}
        # Latest project() payload, written only by the engine loop; HTTP
        # serves this reference (atomic swap) instead of re-running the
        # pipeline per poll when ENGINE_LOOP=1.
        self.projection: Optional[Dict[str, Any]] = None
        self.signals: List[Dict[str, Any]] = []
        self.last_signal_ts_by_key: Dict[str, float] = {}
        self.next_signal_id = 1
        self.status = "STARTING"
        self.error: Optional[str] = None
        # Live Kite websocket ticker + a generation counter. Every (re)connect
        # bumps the generation; only the current generation's callbacks may
        # touch status, so a retired/zombie ticker reconnecting (e.g. 403 after
        # a token roll) can never stomp the live connection's status.
        self.ticker: Any = None
        self.ticker_gen = 0
        self.started_at = ist_now()
        self.spot = 0.0
        self.spot_open = 0.0
        self.day_high = 0.0
        self.day_low = 0.0
        self.prev_close = 0.0
        self.orb_high = 0.0
        self.orb_low = 0.0
        self.spot_history: Deque[Tuple[float, float]] = deque(maxlen=2400)
        self.expiry = ""
        self.session_context: Dict[str, Any] = {}
        self.session_context_updated_at = 0.0
        self._kite: Optional[KiteConnect] = None
        self.commission_cfg = CommissionConfig.from_env(str(ENV_FILE))
        self.rejected_signals: List[Dict[str, Any]] = []
        self.signal_candidates: List[Dict[str, Any]] = []
        self.options_analytics: Dict[str, Any] = {}
        self._prev_net_dealer_delta: Optional[float] = None
        self._iv_store = IVHistoryStore(
            (replay_dir if replay_dir is not None else DATA_DIR) / "iv_history.jsonl"
        )
        self._iv_store.load()
        self._last_options_analytics_journal = 0.0
        self.last_tick_at = ""
        self.last_tick_ts = 0.0
        self.chart_contract: Optional[str] = None
        self.chart_strike: Optional[int] = None
        self.behavior_events: Deque[Dict[str, Any]] = deque(maxlen=800)
        self._behavior_event_keys: Dict[str, float] = {}
        self.last_abnormal_alerts: List[Dict[str, Any]] = []
        self.journal = NiftyJournalStore(replay_dir) if replay_dir is not None else NiftyJournalStore()
        self._signal_journal_file = (
            (replay_dir / "nifty_oi_signals.jsonl") if replay_dir is not None else SIGNAL_JOURNAL_FILE
        )
        self._last_gamma_signal = "NONE"
        self._daily_level_labels: set[str] = set()
        self._last_session_journal_minute = ""
        self.orb_high_reclaimed_at = ""
        self._playbook_phase = "INIT"
        self.morning_context: Dict[str, Any] = {}
        self._live_morning = LiveMorningContext()
        self._bias_verdict = "INIT"
        self._session_open_gap: Dict[str, Any] = {}
        self._orb_restore_attempted = False
        # Loaded lazily on the first evaluate() so replay's frozen clock (not
        # construction-time wall clock) picks the filing day: replaying 19 Jun
        # used to pull FII/DII positioning for *today* into the macro blocker.
        self.futures_eod_context: Optional[Dict[str, Any]] = None

    def set_futures(self, futures: Iterable[InstrumentState]) -> None:
        with self.lock:
            self.futures = {item.token: item for item in futures}

    def _future_instrument_list(self) -> List[InstrumentState]:
        with self.lock:
            return list(self.futures.values())

    def _record_behavior_event(
        self,
        kind: str,
        contract: str,
        *,
        strike: Optional[int] = None,
        option_type: str = "",
        direction: str = "",
        reason: str = "",
        key_area_reasons: Optional[List[str]] = None,
        decision: str = "",
        ts: Optional[float] = None,
    ) -> None:
        event_ts = ts or CLOCK.time()
        dedupe_key = f"{kind}:{contract}:{int(event_ts // 60)}"
        if dedupe_key in self._behavior_event_keys:
            return
        self._behavior_event_keys[dedupe_key] = event_ts
        if len(self._behavior_event_keys) > 2000:
            cutoff = event_ts - 7200
            self._behavior_event_keys = {
                key: seen_ts for key, seen_ts in self._behavior_event_keys.items() if seen_ts >= cutoff
            }
        short_label = {
            "WRITER_ADD": f"{option_type} writers",
            "OI_UNCONFIRMED": f"{option_type} OI unconfirmed",
            "SIGNAL_ENTRY": decision or "Paper entry",
            "SIGNAL_REJECTED": "Rejected thin",
        }.get(kind, kind)
        if key_area_reasons:
            short_label = f"{short_label} @ {key_area_reasons[0]}"
        self.behavior_events.append(
            {
                "ts": event_ts,
                "time": datetime.fromtimestamp(event_ts).strftime("%H:%M:%S"),
                "kind": kind,
                "contract": contract,
                "strike": strike,
                "option_type": option_type,
                "direction": direction,
                "reason": reason,
                "key_area_reasons": key_area_reasons or [],
                "decision": decision,
                "short_label": short_label,
            }
        )
        self.journal.append_behavior(self.behavior_events[-1])

    def _maybe_snapshot_daily_levels(self) -> None:
        now = CLOCK.now()
        clock = (now.hour, now.minute)
        labels: List[str] = []
        if (9, 30) <= clock <= (9, 31):
            labels.append("orb_close")
        if (15, 30) <= clock <= (15, 31):
            labels.append("session_close")
        for label in labels:
            if label in self._daily_level_labels:
                continue
            self._daily_level_labels.add(label)
            self.journal.write_daily_levels(
                label,
                {
                    "spot": self.spot,
                    "open": self.spot_open,
                    "day_high": self.day_high,
                    "day_low": self.day_low,
                    "prev_close": self.prev_close,
                    "orb_high": self.orb_high,
                    "orb_low": self.orb_low,
                    "expiry": self.expiry,
                    "session_context": self.session_context,
                },
            )

    def _maybe_journal_session_context(self) -> None:
        minute_bucket = ist_now()[:16]
        if minute_bucket == self._last_session_journal_minute:
            return
        self._last_session_journal_minute = minute_bucket
        self.journal.append_session_context(
            {
                "spot": self.spot,
                "session_context": self.session_context,
            }
        )

    def _build_spot_levels(self) -> List[Dict[str, Any]]:
        level_defs: List[tuple[str, float, str]] = [
            ("ORB High", self.orb_high, "#f59e0b"),
            ("ORB Low", self.orb_low, "#f59e0b"),
            ("Day High", self.day_high, "#ef4444"),
            ("Day Low", self.day_low, "#3b82f6"),
            ("Open", self.spot_open, "#9ca3af"),
            ("Prev Close", self.prev_close, "#9ca3af"),
        ]
        tech_levels = (self.session_context or {}).get("technical_levels") or {}
        for period in (20, 50, 100, 200):
            ema = as_float(tech_levels.get(f"ema_{period}"))
            if ema > 0:
                level_defs.append((f"EMA {period}", ema, "#c084fc"))
        key_levels = (self.morning_context or {}).get("key_levels") or {}
        pivots = key_levels.get("pivots") or {}
        cam = key_levels.get("camarilla") or {}
        fib = key_levels.get("fibonacci") or {}
        oi = key_levels.get("oi") or {}
        extras: List[tuple[str, float, str]] = [
            ("PP", as_float(pivots.get("PP")), "#eab308"),
            ("R1", as_float(pivots.get("R1")), "#facc15"),
            ("S1", as_float(pivots.get("S1")), "#facc15"),
            ("CR1", as_float(cam.get("CR1")), "#06b6d4"),
            ("CS1", as_float(cam.get("CS1")), "#06b6d4"),
            ("Fib 61.8%", as_float(fib.get("61.8%")), "#ec4899"),
            ("OI Ceiling", as_float((oi.get("ceiling") or {}).get("strike")), "#22c55e"),
            ("OI Floor", as_float((oi.get("floor") or {}).get("strike")), "#ef4444"),
            ("Max Pain", as_float(oi.get("max_pain")), "#f472b6"),
        ]
        extremes = key_levels.get("period_extremes") or {}
        extras.extend(
            [
                ("52W High", as_float(extremes.get("52w_high")), "#a855f7"),
                ("52W Low", as_float(extremes.get("52w_low")), "#a855f7"),
            ]
        )
        for label, price, color in extras:
            if price > 0:
                level_defs.append((label, price, color))
        levels: List[Dict[str, Any]] = []
        for label, price, color in level_defs:
            if price > 0:
                levels.append({"label": label, "price": round(price, 2), "color": color})
        return levels

    def _behavior_markers_for_contract(
        self,
        contract: Optional[str],
        writer_contract: Optional[str] = None,
        max_points: int = 360,
    ) -> List[Dict[str, Any]]:
        if not contract:
            return []
        contracts = {contract}
        if writer_contract:
            contracts.add(writer_contract)
        marker_styles = {
            "WRITER_ADD": {"color": "#22c55e", "chart": "option"},
            "OI_UNCONFIRMED": {"color": "#f97316", "chart": "option"},
            "SIGNAL_ENTRY": {"color": "#a855f7", "chart": "option"},
            "SIGNAL_REJECTED": {"color": "#6b7280", "chart": "option"},
        }
        cutoff_ts = CLOCK.time() - (max_points * 2)
        markers: List[Dict[str, Any]] = []
        for event in self.behavior_events:
            if event.get("contract") not in contracts:
                continue
            if event.get("ts", 0) < cutoff_ts:
                continue
            style = marker_styles.get(str(event.get("kind")), {"color": "#e5e7eb", "chart": "option"})
            markers.append({**event, **style})
        return markers[-40:]

    def _pick_default_chart_instrument(self, instruments: List[InstrumentState]) -> Optional[InstrumentState]:
        if not instruments or self.spot <= 0:
            return instruments[0] if instruments else None
        strike = nearest(self.spot, 100)
        for side in ("CE", "PE"):
            for item in instruments:
                if item.strike == strike and item.option_type == side:
                    return item
        return min(instruments, key=lambda item: abs(item.strike - self.spot))

    def _resolve_chart_strike(
        self,
        instruments: List[InstrumentState],
        *,
        contract: Optional[str] = None,
        strike: Optional[int] = None,
    ) -> int:
        if strike is not None:
            return int(strike)
        if contract:
            match = next((item for item in instruments if item.tradingsymbol == contract), None)
            if match is not None:
                return int(match.strike)
        if self.chart_strike is not None:
            return int(self.chart_strike)
        default = self._pick_default_chart_instrument(instruments)
        if default is not None:
            return int(default.strike)
        if self.spot > 0:
            return nearest(self.spot, 100)
        return int(instruments[0].strike) if instruments else 0

    def _leg_at_strike(
        self,
        instruments: List[InstrumentState],
        strike: int,
        option_type: str,
    ) -> Optional[InstrumentState]:
        return next(
            (item for item in instruments if item.strike == strike and item.option_type == option_type),
            None,
        )

    def build_paired_option_series(
        self,
        ce: Optional[InstrumentState],
        pe: Optional[InstrumentState],
        max_points: int = 360,
    ) -> List[Dict[str, Any]]:
        """Align CE/PE premium + OI on a shared time axis for the OI chart."""
        by_time: Dict[str, Dict[str, Any]] = {}

        def ingest(points: List[Dict[str, Any]], side: str) -> None:
            for point in points:
                key = str(point.get("time") or "")
                if not key:
                    continue
                row = by_time.setdefault(
                    key,
                    {
                        "time": key,
                        "ts": point.get("ts") or 0.0,
                        "ce_price": None,
                        "pe_price": None,
                        "ce_oi": None,
                        "pe_oi": None,
                    },
                )
                row["ts"] = max(float(row.get("ts") or 0), float(point.get("ts") or 0))
                if side == "CE":
                    row["ce_price"] = point.get("price")
                    row["ce_oi"] = point.get("oi")
                else:
                    row["pe_price"] = point.get("price")
                    row["pe_oi"] = point.get("oi")

        if ce is not None:
            ingest(ce.chart_series(max_points), "CE")
        if pe is not None:
            ingest(pe.chart_series(max_points), "PE")

        series = sorted(by_time.values(), key=lambda row: float(row.get("ts") or 0))
        last_ce_price = last_pe_price = None
        last_ce_oi = last_pe_oi = None
        for row in series:
            if row.get("ce_price") is not None:
                last_ce_price = row["ce_price"]
            elif last_ce_price is not None:
                row["ce_price"] = last_ce_price
            if row.get("pe_price") is not None:
                last_pe_price = row["pe_price"]
            elif last_pe_price is not None:
                row["pe_price"] = last_pe_price
            if row.get("ce_oi") is not None:
                last_ce_oi = row["ce_oi"]
            elif last_ce_oi is not None:
                row["ce_oi"] = last_ce_oi
            if row.get("pe_oi") is not None:
                last_pe_oi = row["pe_oi"]
            elif last_pe_oi is not None:
                row["pe_oi"] = last_pe_oi
        return series[-max_points:]

    def spot_chart_series(self, max_points: int = 360) -> List[Dict[str, Any]]:
        points = list(self.spot_history)[-max_points:]
        series: List[Dict[str, Any]] = []
        for ts, ltp in points:
            price = round(float(ltp), 2)
            if price < 15_000 or price > 35_000:
                continue
            series.append(
                {
                    "ts": ts,
                    "time": datetime.fromtimestamp(ts).strftime("%H:%M:%S"),
                    "price": price,
                }
            )
        if len(series) >= 3:
            prices = [row["price"] for row in series]
            median = statistics.median(prices)
            series = [row for row in series if abs(row["price"] - median) <= 400]
        return series

    def _spot_chart_y_range(self, series: List[Dict[str, Any]]) -> Dict[str, float]:
        prices = [row["price"] for row in series if row.get("price")]
        anchor = self.spot if self.spot > 0 else (prices[-1] if prices else 0.0)
        if not prices:
            if anchor > 0:
                pad = max(60.0, anchor * 0.004)
                return {"min": round(anchor - pad, 2), "max": round(anchor + pad, 2)}
            return {"min": 0.0, "max": 0.0}
        lo = min(prices)
        hi = max(prices)
        if anchor > 0:
            lo = min(lo, anchor)
            hi = max(hi, anchor)
        span = max(hi - lo, 1.0)
        pad = max(45.0, span * 0.25, anchor * 0.003 if anchor else 0.0)
        return {"min": round(lo - pad, 2), "max": round(hi + pad, 2)}

    def _build_spot_chart_levels(self) -> List[Dict[str, Any]]:
        """Session-relevant levels only — exclude 52W extremes that wreck intraday Y-scale."""
        all_levels = self._build_spot_levels()
        if self.spot <= 0:
            return [level for level in all_levels if not level["label"].startswith("52W")]
        band = max(120.0, self.spot * 0.018)
        keep_labels = {
            "ORB High",
            "ORB Low",
            "Day High",
            "Day Low",
            "Open",
            "Prev Close",
            "PP",
            "R1",
            "S1",
            "CR1",
            "CS1",
            "Fib 61.8%",
            "Max Pain",
            "OI Ceiling",
            "OI Floor",
        }
        levels: List[Dict[str, Any]] = []
        for level in all_levels:
            label = str(level.get("label") or "")
            if label.startswith("52W"):
                continue
            price = as_float(level.get("price"))
            if label in keep_labels or (price > 0 and abs(price - self.spot) <= band):
                levels.append(level)
        return levels

    def build_chart_data(
        self,
        contract: Optional[str] = None,
        strike: Optional[int] = None,
    ) -> Dict[str, Any]:
        with self.lock:
            instruments = list(self.instruments.values())
            chart_strike = self._resolve_chart_strike(
                instruments,
                contract=contract,
                strike=strike,
            )
            self.chart_strike = chart_strike
            ce_leg = self._leg_at_strike(instruments, chart_strike, "CE")
            pe_leg = self._leg_at_strike(instruments, chart_strike, "PE")
            if ce_leg is not None:
                self.chart_contract = ce_leg.tradingsymbol
            elif pe_leg is not None:
                self.chart_contract = pe_leg.tradingsymbol

            available_strikes = sorted({int(item.strike) for item in instruments})
            strike_rows = []
            for strike_val in available_strikes:
                ce = self._leg_at_strike(instruments, strike_val, "CE")
                pe = self._leg_at_strike(instruments, strike_val, "PE")
                strike_rows.append(
                    {
                        "strike": strike_val,
                        "ce_symbol": ce.tradingsymbol if ce else None,
                        "pe_symbol": pe.tradingsymbol if pe else None,
                        "ce_ltp": ce.last_price if ce else None,
                        "pe_ltp": pe.last_price if pe else None,
                        "ce_oi": ce.oi if ce else None,
                        "pe_oi": pe.oi if pe else None,
                    }
                )

            paired_series = self.build_paired_option_series(ce_leg, pe_leg)
            spot_series = self.spot_chart_series()
            spot_y_range = self._spot_chart_y_range(spot_series)

            contract_names = {
                name
                for name in (
                    ce_leg.tradingsymbol if ce_leg else None,
                    pe_leg.tradingsymbol if pe_leg else None,
                )
                if name
            }
            active_alerts = [
                alert
                for alert in self.last_abnormal_alerts
                if alert.get("contract") in contract_names
            ]
            writer_contracts = {
                str(signal.get("writer_contract"))
                for signal in self.signals
                if signal.get("entry_contract") in contract_names
                or signal.get("writer_contract") in contract_names
            }
            behavior_markers: List[Dict[str, Any]] = []
            for leg in (ce_leg, pe_leg):
                if leg is None:
                    continue
                behavior_markers.extend(
                    self._behavior_markers_for_contract(
                        leg.tradingsymbol,
                        writer_contract=next(iter(writer_contracts), None),
                    )
                )
            behavior_markers.sort(key=lambda row: row.get("ts") or 0)
            behavior_markers = behavior_markers[-40:]
            cutoff_ts = CLOCK.time() - 720
            spot_markers = [
                {
                    **event,
                    "color": {
                        "WRITER_ADD": "#22c55e",
                        "SIGNAL_ENTRY": "#a855f7",
                    }.get(str(event.get("kind")), "#94a3b8"),
                }
                for event in self.behavior_events
                if event.get("kind") in {"WRITER_ADD", "SIGNAL_ENTRY"} and event.get("ts", 0) >= cutoff_ts
            ][-30:]
            return {
                "strike": chart_strike,
                "ce_contract": ce_leg.tradingsymbol if ce_leg else None,
                "pe_contract": pe_leg.tradingsymbol if pe_leg else None,
                "contract": ce_leg.tradingsymbol if ce_leg else (pe_leg.tradingsymbol if pe_leg else None),
                "option_type": "PAIR",
                "spot": self.spot,
                "spot_series": spot_series,
                "spot_y_range": spot_y_range,
                "paired_series": paired_series,
                "option_series": paired_series,
                "available_strikes": strike_rows,
                "available_contracts": strike_rows,
                "spot_levels": self._build_spot_chart_levels(),
                "behavior_markers": behavior_markers,
                "spot_markers": spot_markers,
                "active_behaviors": active_alerts,
                "behavior_legend": [
                    {"kind": "WRITER_ADD", "label": "Writer add (confirmed)", "color": "#22c55e"},
                    {"kind": "OI_UNCONFIRMED", "label": "OI add — price not confirmed", "color": "#f97316"},
                    {"kind": "SIGNAL_ENTRY", "label": "Paper entry", "color": "#a855f7"},
                    {"kind": "SIGNAL_REJECTED", "label": "Rejected — commission too thin", "color": "#6b7280"},
                ],
                "ce_snapshot": {
                    "ltp": ce_leg.last_price if ce_leg else None,
                    "oi": ce_leg.oi if ce_leg else None,
                },
                "pe_snapshot": {
                    "ltp": pe_leg.last_price if pe_leg else None,
                    "oi": pe_leg.oi if pe_leg else None,
                },
            }

    def refresh_session_context(self, force: bool = False) -> None:
        if self._kite is None:
            return
        now_ts = CLOCK.time()
        if not force and now_ts - self.session_context_updated_at < 900:
            return
        with self.lock:
            spot = self.spot
        self.session_context = build_session_context(self._kite, spot=spot)
        self.session_context_updated_at = now_ts
        self._maybe_journal_session_context()

    def set_kite(self, kite: Optional[KiteConnect]) -> None:
        self._kite = kite
        if kite is not None:
            self.refresh_session_context(force=True)
            self._restore_orb_levels()

    def _orb_snapshot_path(self) -> Path:
        return DATA_DIR / f"orb_{CLOCK.today().isoformat()}.json"

    def _persist_orb_snapshot(self) -> None:
        if self.orb_high <= 0 or self.orb_low <= 0:
            return
        path = self._orb_snapshot_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "orb_high": self.orb_high,
                    "orb_low": self.orb_low,
                    "spot_open": self.spot_open,
                    "prev_close": self.prev_close,
                    "captured_at": ist_now(),
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )

    def _load_orb_snapshot(self) -> bool:
        path = self._orb_snapshot_path()
        if not path.exists():
            return False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        hi = as_float(payload.get("orb_high"))
        lo = as_float(payload.get("orb_low"))
        if hi <= 0 or lo <= 0:
            return False
        with self.lock:
            self.orb_high = hi
            self.orb_low = lo
        return True

    def _restore_orb_from_kite(self) -> bool:
        if self._kite is None:
            return False
        try:
            today = CLOCK.today()
            start = datetime.combine(today, dt_time(9, 15))
            end = datetime.combine(today, dt_time(9, 30, 59))
            candles = self._kite.historical_data(
                NIFTY_SPOT_TOKEN,
                start,
                end,
                "minute",
                continuous=False,
                oi=False,
            )
        except Exception:
            return False
        if not candles:
            return False
        highs = [as_float(row.get("high")) for row in candles]
        lows = [as_float(row.get("low")) for row in candles]
        highs = [value for value in highs if value > 0]
        lows = [value for value in lows if value > 0]
        if not highs or not lows:
            return False
        with self.lock:
            self.orb_high = max(highs)
            self.orb_low = min(lows)
        return True

    def _restore_orb_levels(self) -> None:
        if self.orb_high > 0 and self.orb_low > 0:
            return
        if self._load_orb_snapshot():
            return
        if self.data_store is not None:
            hi, lo = self.data_store.query_orb_range(CLOCK.today())
            if hi and lo and hi > 0 and lo > 0:
                with self.lock:
                    self.orb_high = hi
                    self.orb_low = lo
                return
        if not self._orb_restore_attempted:
            self._orb_restore_attempted = True
            if self._restore_orb_from_kite():
                self._persist_orb_snapshot()

    def _capture_session_open_gap(self) -> None:
        if self._session_open_gap.get("gap_type") not in (None, "", "UNKNOWN"):
            return
        if self.spot_open <= 0 or self.prev_close <= 0:
            return
        self._session_open_gap = classify_open_gap(
            self.spot_open,
            self.spot_open,
            self.prev_close,
        )

    def _session_gap_points(self) -> float:
        gap_pts = self._session_open_gap.get("gap_pts")
        if gap_pts is not None:
            return float(gap_pts)
        if self.spot_open and self.prev_close:
            return round(self.spot_open - self.prev_close, 2)
        return 0.0

    @staticmethod
    def _playbook_gap_type(gap_pts: float) -> str:
        if gap_pts <= -GAP_PLAYBOOK_THRESHOLD:
            return "GAP_DOWN"
        if gap_pts >= GAP_PLAYBOOK_THRESHOLD:
            return "GAP_UP"
        return "FLAT"

    def set_instruments(self, instruments: Iterable[InstrumentState], spot: float, expiry: str) -> None:
        with self.lock:
            self.instruments = {item.token: item for item in instruments}
            self.spot = spot
            self.expiry = expiry
            if spot >= 15_000:
                self.spot_history.append((CLOCK.time(), spot))
        self._load_signals_from_journal()
        self._backfill_paper_trade_journal()

    def _backfill_paper_trade_journal(self) -> None:
        """Ensure today's paper rows exist in dated journal (legacy file → daily archive)."""
        if not self._signal_journal_file.exists():
            return
        today = CLOCK.today().isoformat()
        dated_path = self.journal._dated_path("nifty_paper_trades")
        seen: set[str] = set()
        if dated_path.exists():
            for raw in dated_path.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                seen.add(self._paper_journal_fingerprint(row))
        for raw in self._signal_journal_file.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            ts = str(row.get("recorded_at") or row.get("generated_at") or row.get("exit_time") or "")
            if today not in ts:
                continue
            fp = self._paper_journal_fingerprint(row)
            if fp in seen:
                continue
            self.journal.append_paper_trade(row)
            seen.add(fp)

    @staticmethod
    def _paper_journal_fingerprint(row: Dict[str, Any]) -> str:
        return "|".join(
            [
                str(row.get("event") or ""),
                str(row.get("id") or ""),
                str(row.get("generated_at") or ""),
                str(row.get("exit_time") or ""),
                str(row.get("recorded_at") or ""),
            ]
        )
        self._purge_stale_open_signals()

    def _purge_stale_open_signals(self) -> None:
        """Close paper signals tied to a prior expiry series after roll."""
        with self.lock:
            live_symbols = {item.tradingsymbol for item in self.instruments.values()}
            if not live_symbols:
                return
            for signal in self.signals:
                if signal.get("status") != "OPEN":
                    continue
                entry = str(signal.get("entry_contract") or "")
                writer = str(signal.get("writer_contract") or "")
                if entry in live_symbols or writer in live_symbols:
                    continue
                signal["status"] = "CLOSED"
                signal["exit_time"] = ist_now()
                signal["exit_reason"] = "EXPIRED_SERIES_PURGE"
                signal["exit_price"] = signal.get("current_price") or signal.get("entry_price")
                signal["pnl_pct"] = signal.get("pnl_pct") or 0.0
                self._append_signal_journal(signal)

    def update_ticks(self, ticks: List[Dict[str, Any]]) -> None:
        db_spot: Optional[Tuple[str, float, float, float, float, float]] = None
        db_options: List[InstrumentState] = []
        with self.lock:
            for tick in ticks:
                token = as_int(tick.get("instrument_token"))
                if token == NIFTY_SPOT_TOKEN:
                    tick_ltp = as_float(tick.get("last_price"))
                    if tick_ltp >= 15_000 and tick_ltp <= 35_000:
                        if self.spot > 0 and abs(tick_ltp - self.spot) > 250:
                            tick_ltp = self.spot
                        self.spot = tick_ltp
                    ohlc = tick.get("ohlc") or {}
                    self.spot_open = as_float(ohlc.get("open"), self.spot_open)
                    self.day_high = as_float(ohlc.get("high"), self.day_high)
                    self.day_low = as_float(ohlc.get("low"), self.day_low)
                    self.prev_close = as_float(ohlc.get("close"), self.prev_close)
                    now = CLOCK.time()
                    if self.spot >= 15_000:
                        self.spot_history.append((now, self.spot))
                    if self.data_store is not None:
                        db_spot = (
                            ist_now(),
                            self.spot,
                            self.spot_open,
                            self.day_high,
                            self.day_low,
                            self.prev_close,
                        )
                    today = CLOCK.now()
                    if today.hour == 9 and 15 <= today.minute <= 29:
                        self.orb_high = max(self.orb_high or self.spot, self.spot)
                        self.orb_low = min(self.orb_low or self.spot, self.spot)
                        self._persist_orb_snapshot()
                    self._capture_session_open_gap()
                    continue
                item = self.instruments.get(token)
                if item is not None:
                    item.update_from_tick(tick)
                    if self.data_store is not None:
                        db_options.append(item)
                    continue
                fut = self.futures.get(token)
                if fut is not None:
                    fut.update_from_tick(tick)
            self.status = "RUNNING"
            self.error = None
            self.last_tick_at = ist_now()
            self.last_tick_ts = CLOCK.time()
        if self.data_store is not None:
            if db_spot is not None:
                ts, ltp, open_price, high, low, prev_close = db_spot
                self.data_store.insert_spot_tick(
                    ts=ts,
                    ltp=ltp,
                    open_price=open_price,
                    high=high,
                    low=low,
                    prev_close=prev_close,
                )
            for item in db_options:
                self.data_store.insert_option_tick(item)

    def set_status(self, status: str, error: Optional[str] = None) -> None:
        with self.lock:
            self.status = status
            self.error = error

    def quick_status(self) -> Dict[str, Any]:
        tick_age_sec = round(CLOCK.time() - self.last_tick_ts, 1) if self.last_tick_ts else None
        stream_alive = tick_age_sec is not None and tick_age_sec <= 20
        with self.lock:
            return {
                "ok": True,
                "status": self.status,
                "error": self.error,
                "stream_alive": stream_alive,
                "last_tick_at": self.last_tick_at,
                "tick_age_sec": tick_age_sec,
                "spot": self.spot,
                "instrument_count": len(self.instruments),
                "futures_count": len(self.futures),
                "server_time": ist_now(),
            }

    def _append_signal_journal(self, event: Dict[str, Any]) -> None:
        payload = dict(event)
        payload.setdefault("recorded_at", ist_now())
        self._signal_journal_file.parent.mkdir(parents=True, exist_ok=True)
        with self._signal_journal_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, default=str, separators=(",", ":")) + "\n")
        self.journal.append_paper_trade(payload)

    def _load_signals_from_journal(self) -> None:
        """Restore today's paper book from journal after dashboard restart."""
        path = self._signal_journal_file
        if not path.exists():
            return
        states: Dict[int, Dict[str, Any]] = {}
        for raw in path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            sid = as_int(event.get("id"))
            if sid <= 0:
                continue
            event_name = str(event.get("event") or "")
            if event_name == "SIGNAL_REJECTED":
                continue
            if event_name == "SIGNAL_CLOSED":
                prev = states.get(sid, {})
                states[sid] = {**prev, **event, "status": "CLOSED"}
            elif event_name in {"SIGNAL_GENERATED", "SIGNAL_ENTRY"} or event.get("decision"):
                states[sid] = {**states.get(sid, {}), **event}
                if states[sid].get("status") != "CLOSED":
                    states[sid]["status"] = "OPEN"

        live_symbols = {item.tradingsymbol for item in self.instruments.values()}
        restored: List[Dict[str, Any]] = []
        for sid in sorted(states.keys(), reverse=True):
            sig = dict(states[sid])
            if sig.get("status") == "OPEN" and live_symbols:
                entry = str(sig.get("entry_contract") or "")
                writer = str(sig.get("writer_contract") or "")
                if entry and entry not in live_symbols and writer not in live_symbols:
                    continue
            restored.append(sig)

        if restored:
            self.signals = restored[:200]
            self.next_signal_id = max(states.keys()) + 1
            for sig in self.signals:
                if sig.get("status") != "OPEN":
                    continue
                key = str(sig.get("signal_key") or "")
                if not key:
                    continue
                generated = str(sig.get("generated_at") or "")
                try:
                    ts = datetime.strptime(generated, "%Y-%m-%d %H:%M:%S").timestamp()
                except ValueError:
                    ts = CLOCK.time()
                self.last_signal_ts_by_key[key] = ts

    def _row_by_strike_side(self, rows: List[Dict[str, Any]]) -> Dict[Tuple[int, str], Dict[str, Any]]:
        return {(int(row["strike"]), str(row["option_type"])): row for row in rows}

    @staticmethod
    def _near(value: float, level: float, points: float) -> bool:
        return bool(value and level and abs(value - level) <= points)

    def _key_area_reasons(self, row: Dict[str, Any], rows: List[Dict[str, Any]]) -> List[str]:
        strike = as_float(row.get("strike"))
        if not strike:
            return []
        reasons: List[str] = []
        distance_pct = abs(self.spot - strike) / self.spot * 100 if self.spot else 999.0
        if distance_pct <= KEY_AREA_DISTANCE_PCT:
            reasons.append("near spot")
        if int(strike) % 500 == 0:
            reasons.append("psychological 500 strike")
        elif int(strike) % 100 == 0:
            reasons.append("round 100 strike")

        same_side = [candidate for candidate in rows if candidate.get("option_type") == row.get("option_type")]
        ranked_side = sorted(same_side, key=lambda candidate: candidate.get("oi", 0), reverse=True)
        top_strikes = {candidate.get("strike") for candidate in ranked_side[:2]}
        if row.get("strike") in top_strikes:
            reasons.append("top OI wall")

        level_tolerance = 60.0
        levels = [
            ("day high", self.day_high),
            ("day low", self.day_low),
            ("open", self.spot_open),
            ("prev close", self.prev_close),
            ("ORB high", self.orb_high),
            ("ORB low", self.orb_low),
        ]
        tech_levels = (self.session_context or {}).get("technical_levels") or {}
        for label, level in levels:
            if self._near(strike, level, level_tolerance):
                reasons.append(label)
        for tech_reason in near_technical_level(strike, tech_levels, TECH_LEVEL_TOLERANCE):
            reasons.append(tech_reason)
        key_levels = (self.morning_context or {}).get("key_levels") or {}
        for row in key_levels.get("flat_levels") or []:
            level_price = as_float(row.get("value"))
            label = str(row.get("label") or "level")
            if level_price and self._near(strike, level_price, level_tolerance):
                reasons.append(label.lower())
        return reasons

    def _open_signals(self) -> List[Dict[str, Any]]:
        return [signal for signal in self.signals if signal.get("status") == "OPEN"]

    def _update_open_signals(
        self,
        rows: List[Dict[str, Any]],
        paired_rows: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        rows_by_contract = {str(row["tradingsymbol"]): row for row in rows}
        pairs_by_strike = {as_int(pair.get("strike")): pair for pair in (paired_rows or [])}
        spot_v5 = self._spot_velocity(300)
        now = ist_now()
        for signal in self.signals:
            if signal.get("status") != "OPEN":
                continue
            current = rows_by_contract.get(str(signal.get("entry_contract")))
            if not current:
                continue
            current_price = as_float(current.get("last_price"))
            signal["current_price"] = current_price
            entry_price = as_float(signal.get("entry_price"))
            pnl_pct = 0.0
            if entry_price > 0:
                pnl_pct = round(((current_price - entry_price) / entry_price) * 100, 2)
                signal["pnl_pct"] = pnl_pct
            stop_price = as_float(signal.get("stop_price"))

            # --- Live conviction re-evaluation (mentor: every update) --------
            oi_conv = self._evaluate_open_oi_conviction(signal, pairs_by_strike, spot_v5, rows)
            signal["oi_conviction"] = oi_conv
            cur_conv = self._conviction_score(oi_conv)
            if signal.get("entry_conviction") is None:
                signal["entry_conviction"] = cur_conv
            prev_peak = as_int(signal.get("peak_conviction"), cur_conv)
            peak_conv = max(prev_peak, cur_conv)
            signal["current_conviction"] = cur_conv
            signal["peak_conviction"] = peak_conv

            # --- Excursions: MFE / MAE / max profit (mentor: every update) ---
            mfe_pct = max(as_float(signal.get("mfe_pct", pnl_pct)), pnl_pct)
            mae_pct = min(as_float(signal.get("mae_pct", pnl_pct)), pnl_pct)
            signal["mfe_pct"] = round(mfe_pct, 2)
            signal["mae_pct"] = round(mae_pct, 2)
            signal["max_profit_pct"] = round(max(0.0, mfe_pct), 2)

            # --- Live confirmation factors (for CONFIRMATION_LOST) -----------
            writer_5m = as_float(oi_conv.get("writer_oi_5m_pct"))
            live_conf = {
                "thesis": str(oi_conv.get("level")) in {"STRONG", "NEUTRAL"},
                "commission": bool(oi_conv.get("commission_to_target_pass")),
                "participant": writer_5m > 0,
            }
            if signal.get("entry_confirmations") is None:
                signal["entry_confirmations"] = sorted(k for k, v in live_conf.items() if v)
            entry_confs = signal.get("entry_confirmations") or []
            lost_confs = [k for k in entry_confs if not live_conf.get(k)]

            prev_level = str(signal.get("_last_journaled_oi_level") or "")
            new_level = str(oi_conv.get("level") or "")
            if new_level and new_level != prev_level:
                signal["_last_journaled_oi_level"] = new_level
                self._append_signal_journal(
                    {
                        "event": "SIGNAL_UPDATE",
                        "id": signal.get("id"),
                        "signal_key": signal.get("signal_key"),
                        "status": "OPEN",
                        "decision": signal.get("decision"),
                        "strike": signal.get("strike"),
                        "entry_contract": signal.get("entry_contract"),
                        "current_price": current_price,
                        "pnl_pct": pnl_pct,
                        "current_conviction": cur_conv,
                        "peak_conviction": peak_conv,
                        "mfe_pct": signal["mfe_pct"],
                        "mae_pct": signal["mae_pct"],
                        "oi_conviction": oi_conv,
                        "spot": self.spot,
                    }
                )

            # --- Dynamic exits (no fixed profit target; protect captured P&L)
            # Priority: catastrophic stop, thesis invalidation, participant
            # reversal, loss of multiple confirmations, then conviction fade.
            exit_reason = None
            exit_note = None
            if stop_price and current_price <= stop_price:
                exit_reason = "STOP_HIT"  # catastrophic stop — unchanged
            elif new_level == "INVALIDATED":
                # Entry thesis broken — exit immediately (mentor rules 1 & 5).
                signal["oi_invalid_streak"] = as_int(signal.get("oi_invalid_streak")) + 1
                exit_reason = "OI_CONVICTION_BROKEN"
                exit_note = oi_conv.get("read")
            else:
                signal["oi_invalid_streak"] = 0
                if writer_5m <= PLAYBOOK_VELOCITY_UNWIND_PCT:
                    # The followed writer is covering — participant reversed.
                    exit_reason = "PARTICIPANT_REVERSAL"
                    exit_note = f"Followed writer unwinding ({writer_5m:.1f}% 5m)"
                elif len(lost_confs) >= CONFIRMATION_LOST_MIN:
                    exit_reason = "CONFIRMATION_LOST"
                    exit_note = "Entry confirmations lost: " + ", ".join(lost_confs)

            # Conviction fade: weakened well below peak and not recovering.
            weakened = (peak_conv - cur_conv) >= CONVICTION_FADE_DROP
            fade_streak = as_int(signal.get("conv_fade_streak")) + 1 if weakened else 0
            signal["conv_fade_streak"] = fade_streak
            if exit_reason is None and weakened and fade_streak >= CONVICTION_FADE_STREAK:
                exit_reason = "CONVICTION_FADE"
                exit_note = f"Conviction {cur_conv} faded from peak {peak_conv}"

            if exit_reason is None:
                enriched = enrich_signal_with_commission(signal, self.commission_cfg)
                signal.update(
                    {
                        "pnl_gross_rupees": enriched.get("pnl_gross_rupees"),
                        "pnl_commission_rupees": enriched.get("pnl_commission_rupees"),
                        "pnl_net_rupees": enriched.get("pnl_net_rupees"),
                    }
                )
                continue

            # --- Close: record dynamic-management summary (mentor journal) ---
            signal["status"] = "CLOSED"
            signal["exit_time"] = now
            signal["exit_price"] = current_price
            signal["exit_reason"] = exit_reason
            if exit_note:
                signal["exit_note"] = exit_note
            signal["exit_conviction"] = cur_conv
            signal["profit_captured_pct"] = round(pnl_pct, 2)
            signal["profit_given_back_pct"] = round(max(0.0, signal["max_profit_pct"] - pnl_pct), 2)
            closed = enrich_signal_with_commission(signal, self.commission_cfg)
            signal.update(closed)
            self._append_signal_journal({"event": "SIGNAL_CLOSED", **signal})

    def _framework_confluence_bonus(self, decision: str, analytics: Dict[str, Any]) -> Dict[str, Any]:
        """Phases 7–9 bonus (0–15) for journal — does not block paper on its own."""
        if not analytics or analytics.get("error"):
            return {"bonus": 0, "max": 15, "factors": [], "detail": "Analytics warming up"}
        factors: List[str] = []
        bonus = 0
        div = (analytics.get("price_delta_divergence") or {}).get("label", "")
        if decision == "BUY_CE" and div in {"AGGRESSIVE_BUYERS", "SELLER_ABSORPTION"}:
            bonus += 5
            factors.append("delta+price aligned for long")
        elif decision == "BUY_PE" and div in {"AGGRESSIVE_SELLERS", "BUYER_ABSORPTION"}:
            bonus += 5
            factors.append("delta+price aligned for fade")
        elif div not in {"", "NEUTRAL"}:
            factors.append(f"delta divergence: {div}")

        prem = analytics.get("premium_vs_vix")
        if prem == "CHEAP":
            bonus += 5
            factors.append("IV cheap vs India VIX")
        elif prem == "FAIR":
            bonus += 2
            factors.append("IV fair vs VIX")

        gex = analytics.get("gex_regime")
        if decision == "BUY_CE" and gex == "POSITIVE_GAMMA":
            bonus += 5
            factors.append("positive GEX supports pin/revert long")
        elif decision == "BUY_PE" and gex == "NEGATIVE_GAMMA":
            bonus += 5
            factors.append("negative GEX supports expansion fade")
        elif gex:
            factors.append(f"GEX regime: {gex}")

        return {
            "bonus": min(bonus, 15),
            "max": 15,
            "factors": factors,
            "detail": "; ".join(factors) if factors else "No phase 7–9 bonus",
        }

    def _refresh_options_analytics(self, paired_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        tech = (self.session_context or {}).get("technical_levels") or {}
        india_vix = as_float(tech.get("india_vix"), 0.0) or None
        spot_v5 = self._spot_velocity(300)
        payload = analyze_option_chain(
            paired_rows,
            spot=self.spot,
            expiry=self.expiry,
            lot_size=self.commission_cfg.lot_size,
            spot_delta_5m=as_float(spot_v5.get("delta")),
            prev_net_dealer_delta=self._prev_net_dealer_delta,
            india_vix=india_vix,
            iv_store=self._iv_store,
        )
        if not payload.get("error"):
            self._prev_net_dealer_delta = as_float(payload.get("net_dealer_delta"))
        self.options_analytics = payload
        now_ts = CLOCK.time()
        if now_ts - self._last_options_analytics_journal >= 300:
            self._last_options_analytics_journal = now_ts
            summary = {k: v for k, v in payload.items() if k != "chain_rows"}
            summary["spot"] = self.spot
            self.journal.append_options_analytics(summary)
        return payload

    def _process_signal_candidates(
        self,
        alerts: List[Dict[str, Any]],
        rows: List[Dict[str, Any]],
        paired_rows: List[Dict[str, Any]],
        playbook: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Score every writer alert, journal for review, return ranked candidates."""
        is_expiry = _is_expiry_day(self)
        gate_now = CLOCK.now()
        in_orb = is_no_trade_window(gate_now, is_expiry=is_expiry) or is_nifty_options_blocked(gate_now, is_expiry=is_expiry)
        late = is_late_session(gate_now)
        open_signals = self._open_signals()
        open_strikes = [as_int(signal.get("strike")) for signal in open_signals]
        open_decisions = {str(signal.get("decision")) for signal in open_signals}
        rows_by_side = self._row_by_strike_side(rows)
        pairs_by_strike = {as_int(pair.get("strike")): pair for pair in paired_rows}
        spot_v5 = self._spot_velocity(300)
        now_ts = CLOCK.time()
        scored: List[Dict[str, Any]] = []

        for alert in alerts:
            direction = str(alert.get("direction") or "")
            if direction not in {"WRITERS ADDING", "OI ADDING - PRICE NOT CONFIRMED"}:
                continue
            writer_side = str(alert.get("option_type"))
            strike = as_int(alert.get("strike"))
            if writer_side == "CE":
                decision = "BUY_PE"
                entry_side = "PE"
            elif writer_side == "PE":
                decision = "BUY_CE"
                entry_side = "CE"
            else:
                continue
            key = f"{strike}:{writer_side}:{decision}"
            pe_behavior = self._analyze_pe_strike_row(
                pairs_by_strike.get(strike),
                strike,
                spot_v5,
            ).get("behavior", "")
            entry_row = rows_by_side.get((strike, entry_side))
            candidate = score_signal_candidate(
                alert,
                spot=self.spot,
                spot_v5=spot_v5,
                pe_behavior=str(pe_behavior or ""),
                entry_row=entry_row,
                commission_cfg=self.commission_cfg,
                playbook=playbook,
                morning_context=self.morning_context,
                open_signals_count=len(open_signals),
                block_same_thesis_stack=BLOCK_SAME_THESIS_STACK,
                in_orb_no_trade=in_orb,
                late_session=late,
                open_strikes=open_strikes,
                open_decisions=open_decisions,
                single_direction_book=SINGLE_DIRECTION_BOOK,
                last_signal_ts=self.last_signal_ts_by_key.get(key, 0.0),
                now_ts=now_ts,
                require_pe_spot_for_buy_ce=REQUIRE_PE_SPOT_CONFIRM_FOR_BUY_CE,
                require_spot_weak_for_buy_pe=REQUIRE_SPOT_WEAK_FOR_BUY_PE,
                signal_cooldown_sec=600,
                min_open_strike_spacing=MIN_OPEN_STRIKE_SPACING,
            )
            candidate["desk_context"] = self.journal.build_signal_context(self)
            candidate["evaluated_at"] = ist_now()
            candidate["framework_bonus"] = self._framework_confluence_bonus(
                candidate.get("decision", ""),
                self.options_analytics,
            )
            fb = candidate.get("framework_bonus") or {}
            candidate["framework_bonus"] = fb
            candidate["total_score_with_bonus"] = as_int(candidate.get("total_score")) + as_int(
                fb.get("bonus") if isinstance(fb, dict) else 0
            )
            front_behavior = str((self.futures_layer or {}).get("front_behavior") or "FLAT")
            fut_align = evaluate_fut_opt_alignment(
                str(candidate.get("decision") or ""),
                eod_context=self.futures_eod_context,
                front_behavior=front_behavior,
            )
            candidate["futures_alignment"] = fut_align
            if fut_align.get("blocker"):
                blockers = list(candidate.get("blockers") or [])
                if fut_align["blocker"] not in blockers:
                    blockers.append(fut_align["blocker"])
                candidate["blockers"] = blockers
                candidate["paper_eligible"] = (
                    bool(candidate.get("confluence_ready"))
                    and len(blockers) == 0
                )
            self.journal.append_signal_candidate(candidate)
            scored.append(candidate)

        scored.sort(key=lambda row: (row.get("total_score", 0), row.get("score_pct", 0)), reverse=True)
        self.signal_candidates = scored[:50]
        return scored

    def _maybe_take_paper_signal(
        self,
        scored: List[Dict[str, Any]],
        alerts_by_contract: Dict[str, Dict[str, Any]],
    ) -> None:
        """Open at most one paper position from highest-scoring eligible candidate."""
        is_expiry = _is_expiry_day(self)
        gate_now = CLOCK.now()
        if is_no_trade_window(gate_now, is_expiry=is_expiry) or is_nifty_options_blocked(gate_now, is_expiry=is_expiry) or is_late_session(gate_now):
            return
        if len(self._open_signals()) >= MAX_OPEN_SIGNALS:
            return
        if BLOCK_SAME_THESIS_STACK and self._open_signals():
            return

        eligible = [row for row in scored if row.get("paper_eligible")]
        if not eligible:
            return

        open_decisions = {str(signal.get("decision")) for signal in self._open_signals()}
        if SINGLE_DIRECTION_BOOK and not open_decisions:
            buy_ce = [row for row in eligible if row["decision"] == "BUY_CE"]
            buy_pe = [row for row in eligible if row["decision"] == "BUY_PE"]
            if buy_ce and buy_pe:
                best_ce = max(buy_ce, key=lambda row: row["total_score"])
                best_pe = max(buy_pe, key=lambda row: row["total_score"])
                eligible = [best_pe] if best_pe["total_score"] >= best_ce["total_score"] else [best_ce]

        best = max(eligible, key=lambda row: row["total_score"])
        alert = alerts_by_contract.get(str(best.get("writer_contract"))) or {}
        if not alert:
            return

        strike = as_int(best.get("strike"))
        decision = str(best.get("decision"))
        entry_side = str(best.get("entry_side"))
        key = str(best.get("signal_key"))
        now_ts = CLOCK.time()
        now = ist_now()

        if any(signal.get("status") == "OPEN" and signal.get("signal_key") == key for signal in self.signals):
            return
        if now_ts - self.last_signal_ts_by_key.get(key, 0) < 600:
            return

        entry_price = as_float(best.get("entry_price"))
        target_price = as_float(best.get("target_price"))
        if entry_price <= 0:
            return

        thesis = (
            "Confirmed PE writer-add below/near spot; support building."
            if decision == "BUY_CE"
            else "Confirmed CE writer-add above/near spot; bearish pressure."
        )

        signal = {
            "id": self.next_signal_id,
            "signal_key": key,
            "status": "OPEN",
            "generated_at": now,
            "spot": self.spot,
            "writer_contract": best.get("writer_contract"),
            "writer_side": best.get("writer_side"),
            "strike": strike,
            "decision": decision,
            "entry_contract": best.get("entry_contract"),
            "entry_side": entry_side,
            "entry_price": entry_price,
            "current_price": entry_price,
            "lot_size": self.commission_cfg.lot_size,
            "stop_price": round(entry_price * 0.70, 2),
            "target_price": target_price,
            "pnl_pct": 0.0,
            "exit_time": None,
            "exit_price": None,
            "exit_reason": None,
            "thesis": thesis,
            "confluence_score": best.get("total_score"),
            "confluence_grade": best.get("grade"),
            "confluence_dimensions": best.get("dimensions"),
            "source_alert": alert,
            "desk_context": self.journal.build_signal_context(self),
            "paper_only": True,
        }
        signal = enrich_signal_with_commission(signal, self.commission_cfg)
        self.next_signal_id += 1
        self.last_signal_ts_by_key[key] = now_ts
        self.signals.insert(0, signal)
        self.signals = self.signals[:200]
        self._append_signal_journal({"event": "SIGNAL_GENERATED", **signal})
        self.journal.append_signal_candidate(
            {
                **best,
                "event": "SIGNAL_CANDIDATE_TAKEN",
                "paper_signal_id": signal["id"],
                "evaluated_at": now,
            }
        )
        self._record_behavior_event(
            "SIGNAL_ENTRY",
            str(signal.get("entry_contract")),
            strike=strike,
            option_type=entry_side,
            direction=decision,
            reason=f"{thesis} (confluence {best.get('total_score')}/{best.get('max_score')})",
            key_area_reasons=alert.get("key_area_reasons"),
            decision=decision,
        )

    def _maybe_generate_signals(
        self,
        alerts: List[Dict[str, Any]],
        rows: List[Dict[str, Any]],
        paired_rows: List[Dict[str, Any]],
        playbook: Optional[Dict[str, Any]] = None,
    ) -> None:
        scored = self._process_signal_candidates(alerts, rows, paired_rows, playbook=playbook)
        alerts_by_contract = {str(alert.get("contract")): alert for alert in alerts}
        self._maybe_take_paper_signal(scored, alerts_by_contract)

    def _spot_velocity(self, seconds_back: int = 300) -> Dict[str, float]:
        points = list(self.spot_history)
        if not points or self.spot <= 0:
            return {"delta": 0.0, "pct": 0.0}
        cutoff = CLOCK.time() - seconds_back
        old_ltp = points[0][1]
        for ts, ltp in points:
            if ts <= cutoff:
                old_ltp = ltp
            else:
                break
        delta = round(self.spot - old_ltp, 2)
        pct = round((delta / old_ltp) * 100, 3) if old_ltp else 0.0
        return {"delta": delta, "pct": pct}

    def _playbook_watch_strikes(self) -> List[int]:
        strikes = list(PLAYBOOK_WATCH_STRIKES)
        if self.spot > 0:
            atm = nearest(self.spot, 100)
            if atm not in strikes:
                strikes.append(atm)
        if self.orb_low > 0:
            orb_strike = nearest(self.orb_low, 100)
            if orb_strike not in strikes:
                strikes.append(orb_strike)
        return sorted(set(strikes))

    def _analyze_pe_strike_row(
        self,
        pair: Optional[Dict[str, Any]],
        strike: int,
        spot_v5: Dict[str, float],
    ) -> Dict[str, Any]:
        if not pair:
            return {
                "strike": strike,
                "tracked": False,
                "behavior": "NOT_IN_CHAIN",
                "read": "Strike not in subscription window",
                "status": "info",
            }
        pe = pair.get("pe") or {}
        ce = pair.get("ce") or {}
        pe_v5 = pe.get("velocity_5m") or {}
        pe_v1 = pe.get("velocity_1m") or {}
        ce_v5 = ce.get("velocity_5m") or {}
        oi_delta = as_int(pe_v5.get("delta"))
        oi_pct = as_float(pe_v5.get("pct"))
        pe_premium_delta = as_float(pe_v5.get("price_delta"))
        spot_delta = as_float(spot_v5.get("delta"))
        pe_adding = oi_pct >= PLAYBOOK_VELOCITY_ADD_PCT or oi_delta > 0
        pe_unwinding = oi_pct <= PLAYBOOK_VELOCITY_UNWIND_PCT
        spot_up = spot_delta > PLAYBOOK_SPOT_FLAT_PTS
        spot_flat = abs(spot_delta) <= PLAYBOOK_SPOT_FLAT_PTS
        spot_down = spot_delta < -PLAYBOOK_SPOT_FLAT_PTS
        ce_adding = as_float(ce_v5.get("pct")) >= PLAYBOOK_VELOCITY_ADD_PCT

        if pe_adding and spot_up:
            behavior, read, status = "PE_ADD_SPOT_UP", "PE OI adding + spot rising — support confirmed", "ok"
        elif pe_adding and spot_flat:
            behavior, read, status = (
                "PE_ADD_SPOT_FLAT",
                "PE OI adding but spot flat — writers stacking, need price follow-through",
                "warn",
            )
        elif pe_adding and spot_down:
            behavior, read, status = (
                "PE_ADD_SPOT_DOWN",
                "PE OI adding but spot falling — divergence, support not working yet",
                "bad",
            )
        elif pe_unwinding:
            behavior, read, status = "PE_UNWIND", "PE OI unwinding — support leaving", "bad"
        elif ce_adding:
            behavior, read, status = "CE_DOMINANT", "CE OI building at this strike — overhead pressure", "warn"
        else:
            behavior, read, status = "QUIET", "No strong PE/CE footprint at this strike", "info"

        return {
            "strike": strike,
            "tracked": True,
            "behavior": behavior,
            "read": read,
            "status": status,
            "pe_oi": pe.get("oi"),
            "pe_ltp": pe.get("last_price"),
            "ce_ltp": ce.get("last_price"),
            "pe_oi_5m_delta": oi_delta,
            "pe_oi_5m_pct": oi_pct,
            "pe_premium_5m_delta": pe_premium_delta,
            "ce_oi_5m_pct": as_float(ce_v5.get("pct")),
            "spot_5m_delta": spot_delta,
            "spot_dist": round(self.spot - strike, 2) if self.spot else None,
            "pair_read": pair.get("read"),
        }

    @staticmethod
    def _conviction_score(oi_conv: Dict[str, Any]) -> int:
        """Map a live OI-conviction read to a 0..100 score for tick-by-tick tracking.

        Derived in the trade-management layer (the conviction engine itself is
        unchanged). A failed commission-to-target check shaves points so a
        position whose remaining move no longer covers costs reads as weaker.
        """
        level = str(oi_conv.get("level") or "NEUTRAL")
        score = CONVICTION_LEVEL_SCORE.get(level, 50)
        if oi_conv.get("commission_to_target_pass") is False and score > 0:
            score = max(0, score - 15)
        return int(score)

    def _evaluate_open_oi_conviction(
        self,
        signal: Dict[str, Any],
        pairs_by_strike: Dict[int, Dict[str, Any]],
        spot_v5: Dict[str, float],
        rows: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Live OI velocity conviction for an open paper position."""
        decision = str(signal.get("decision") or "")
        strike = as_int(signal.get("strike"))
        pair = pairs_by_strike.get(strike)
        pe_analysis = self._analyze_pe_strike_row(pair, strike, spot_v5)
        pe_behavior = str(pe_analysis.get("behavior") or "QUIET")
        spot_delta = as_float(spot_v5.get("delta"))

        ce = (pair or {}).get("ce") or {}
        pe = (pair or {}).get("pe") or {}
        ce_v5 = ce.get("velocity_5m") or {}
        pe_v5 = pe.get("velocity_5m") or {}
        ce_v15 = ce.get("velocity_15m") or {}
        pe_v15 = pe.get("velocity_15m") or {}
        ce_5m_pct = as_float(ce_v5.get("pct"))
        pe_5m_pct = as_float(pe_v5.get("pct"))
        ce_15m_pct = as_float(ce_v15.get("pct"))
        pe_15m_pct = as_float(pe_v15.get("pct"))

        rows_by_contract = {str(row["tradingsymbol"]): row for row in rows}
        writer_row = rows_by_contract.get(str(signal.get("writer_contract") or ""))
        writer_v5 = (writer_row.get("velocity_5m") or {}) if writer_row else {}
        writer_5m_pct = as_float(writer_v5.get("pct"))

        level = "NEUTRAL"
        status = "info"
        read = pe_analysis.get("read") or "Monitoring OI velocity vs thesis"

        if decision == "BUY_CE":
            if pe_behavior == "PE_ADD_SPOT_UP":
                level, status, read = "STRONG", "ok", pe_analysis["read"]
            elif pe_behavior in {"PE_ADD_SPOT_FLAT", "QUIET"}:
                level, status = "WEAK", "warn"
                read = f"Thesis cooling — {pe_analysis['read']}"
            elif pe_behavior in {"PE_UNWIND", "PE_ADD_SPOT_DOWN"}:
                level, status = "INVALIDATED", "bad"
                read = f"Thesis broken — {pe_analysis['read']}"
            elif pe_behavior == "CE_DOMINANT":
                level, status = "WEAK", "warn"
                read = "CE building overhead — long needs PE+spot follow-through"
        elif decision == "BUY_PE":
            ce_adding = ce_5m_pct >= PLAYBOOK_VELOCITY_ADD_PCT or writer_5m_pct >= PLAYBOOK_VELOCITY_ADD_PCT
            ce_unwinding = ce_5m_pct <= PLAYBOOK_VELOCITY_UNWIND_PCT
            spot_weak = spot_delta <= PLAYBOOK_SPOT_FLAT_PTS
            if ce_adding and spot_weak:
                level, status = "STRONG", "ok"
                read = "CE writers still adding + spot not rising — fade thesis holds"
            elif pe_behavior == "PE_ADD_SPOT_UP":
                level, status = "INVALIDATED", "bad"
                read = "PE+spot confirming support — contradicts short fade"
            elif ce_unwinding and spot_delta > PLAYBOOK_SPOT_FLAT_PTS:
                level, status = "INVALIDATED", "bad"
                read = "CE writers covering + spot rising — fade thesis broken"
            elif ce_adding:
                level, status = "WEAK", "warn"
                read = "CE OI building but spot not weak enough — watch closely"
            else:
                level, status = "WEAK", "warn"
                read = "No fresh CE writer velocity — thesis aging"

        current_price = as_float(signal.get("current_price") or signal.get("entry_price"))
        target_price = as_float(signal.get("target_price"))
        lot_size = as_int(signal.get("lot_size"), self.commission_cfg.lot_size)
        comm = commission_conviction_check(current_price, target_price, lot_size, self.commission_cfg)

        return {
            "level": level,
            "status": status,
            "read": read,
            "decision": decision,
            "strike": strike,
            "pe_behavior": pe_behavior,
            "ce_oi_5m_pct": round(ce_5m_pct, 2),
            "pe_oi_5m_pct": round(pe_5m_pct, 2),
            "ce_oi_15m_pct": round(ce_15m_pct, 2),
            "pe_oi_15m_pct": round(pe_15m_pct, 2),
            "writer_oi_5m_pct": round(writer_5m_pct, 2),
            "spot_5m_delta": spot_delta,
            "commission_to_target_pass": comm.get("passed"),
            "commission_reason": comm.get("reason"),
            "checked_at": ist_now(),
        }

    def refresh_morning_context(self, force: bool = False) -> Dict[str, Any]:
        with self.lock:
            spot = self.spot
            day_open = self.spot_open
            day_high = self.day_high
            day_low = self.day_low
            prev_close = self.prev_close
            orb_high = self.orb_high
            orb_low = self.orb_low
            expiry = self.expiry
        self.morning_context = self._live_morning.refresh(
            spot,
            day_open=day_open,
            day_high=day_high,
            day_low=day_low,
            prev_close=prev_close,
            orb_high=orb_high,
            orb_low=orb_low,
            option_expiry=expiry,
            force_journal=force,
        )
        return self.morning_context

    def _evaluate_bias_verdict(
        self,
        *,
        gap_type: str,
        orb_low_held: bool,
        orb_high_reclaimed: bool,
        can_extend: bool,
        pe_divergence: List[Dict[str, Any]],
        phase: str,
        cash_open_gap: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        ctx = self.refresh_morning_context()
        expected = str(ctx.get("combined_bias") or "UNKNOWN").upper()
        gift_bias = str(ctx.get("gift_overnight_bias") or "")
        cash_gap = dict(cash_open_gap or self._session_open_gap or {})
        if not cash_gap.get("gap_type") or cash_gap.get("gap_type") == "UNKNOWN":
            cash_gap = classify_open_gap(self.spot_open, self.spot_open, self.prev_close)
        cash_gap_type = str(cash_gap.get("gap_type") or gap_type)
        gap_pts = cash_gap.get("gap_pts")
        gap_label = (
            f"{cash_gap_type} ({gap_pts:+.1f} pts vs prev close)"
            if gap_pts is not None
            else cash_gap_type
        )
        open_up = cash_gap_type in {"GAP_UP", "MILD_UP"}
        open_down = cash_gap_type in {"GAP_DOWN", "MILD_DOWN"}
        open_flat = cash_gap_type in {"FLAT", "UNKNOWN"}
        bullish_expected = expected.startswith("BULL") or gift_bias in {"GAP_UP", "MILD_UP"}
        bearish_expected = expected.startswith("BEAR") or gift_bias in {"GAP_DOWN", "MILD_DOWN"}

        verdict = "PENDING"
        detail: List[str] = []

        if open_up:
            if bullish_expected:
                verdict = "CONFIRMED"
                detail.append(f"Cash open {gap_label} matches morning bullish context")
            elif bearish_expected:
                verdict = "REJECTED"
                detail.append(f"Cash open {gap_label} vs bearish morning bias")
            else:
                verdict = "PARTIAL"
                detail.append(f"Gap-up open {gap_label} — no strong morning lean")
        elif open_down:
            if bearish_expected:
                verdict = "CONFIRMED"
                detail.append(f"Cash open {gap_label} matches morning bearish context")
            elif bullish_expected:
                if orb_high_reclaimed and can_extend:
                    verdict = "TRAP"
                    detail.append(f"Gap-down trap — ORB reclaimed against bullish morning bias ({gap_label})")
                elif not orb_low_held:
                    verdict = "REJECTED"
                    detail.append(f"Gap-down held — morning bullish bias rejected at open ({gap_label})")
                else:
                    verdict = "PARTIAL"
                    detail.append(f"Gap-down vs bullish bias — ORB low still held ({gap_label})")
            else:
                verdict = "PARTIAL"
                detail.append(f"Gap-down open {gap_label} — watch ORB + participants")
        elif open_flat:
            if expected == "NEUTRAL":
                verdict = "NEUTRAL_OPEN"
                detail.append(f"Flat open {gap_label} — neutral morning context")
            elif bullish_expected:
                verdict = "PARTIAL"
                detail.append(
                    f"Flat/mild open {gap_label} — bullish bias not confirmed at cash open; follow OI at key areas"
                )
            elif bearish_expected:
                verdict = "PARTIAL"
                detail.append(
                    f"Flat/mild open {gap_label} — bearish bias intact; participants decide at support/resistance"
                )
            else:
                verdict = "PARTIAL"
                detail.append(f"Flat open {gap_label} — compare morning context vs live OI")
        else:
            verdict = "PARTIAL"
            detail.append(f"Open {gap_label} vs {expected} morning bias")

        if pe_divergence and verdict == "CONFIRMED":
            verdict = "PARTIAL"
            detail.append("PE divergence — participants not confirming bias")

        if phase in {"CE_PUSH", "PE_DIVERGENCE", "GAP_WEAK", "RECLAIM_FAILED"}:
            if verdict in {"CONFIRMED", "PARTIAL"}:
                verdict = "REJECTED"
                detail.append(f"Playbook phase {phase} rejects morning bias")

        payload = {
            "verdict": verdict,
            "expected_bias": expected,
            "gift_overnight_bias": gift_bias,
            "live_gap": cash_gap_type,
            "playbook_gap": gap_type,
            "cash_open_gap": cash_gap,
            "playbook_phase": phase,
            "chosen_instrument": ctx.get("chosen_instrument"),
            "oi_ceiling": ctx.get("oi_ceiling"),
            "oi_floor": ctx.get("oi_floor"),
            "max_pain": ctx.get("max_pain"),
            "detail": "; ".join(detail),
            "morning_loaded": ctx.get("loaded", False),
        }

        if verdict != self._bias_verdict:
            self._bias_verdict = verdict
            self.journal.append_bias_verdict(payload)
        return payload

    def _detect_intraday_playbook(
        self,
        rows: List[Dict[str, Any]],
        paired_rows: List[Dict[str, Any]],
        abnormal_alerts: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Gap-down → ORB strength → 9:30 PE build → unwind → CE push sequence."""
        self._capture_session_open_gap()
        cash_open_gap = dict(self._session_open_gap)
        gap_pts = self._session_gap_points()
        gap_pct = round((gap_pts / self.prev_close) * 100, 2) if self.prev_close else 0.0
        gap_type = self._playbook_gap_type(gap_pts)
        cash_gap_type = str(cash_open_gap.get("gap_type") or gap_type)

        orb_low_held = bool(self.orb_low and self.day_low and self.day_low >= self.orb_low - 5)
        if self.orb_high and self.spot >= self.orb_high and not self.orb_high_reclaimed_at:
            self.orb_high_reclaimed_at = ist_now()
        orb_high_reclaimed = bool(self.orb_high_reclaimed_at)

        now = CLOCK.now()
        is_expiry = _is_expiry_day(self)
        in_orb_watch = is_no_trade_window(now, is_expiry=is_expiry)
        in_930_window = (now.hour == 9 and now.minute >= 30) or (now.hour == 10 and now.minute <= 15)
        after_930 = (now.hour > 9) or (now.hour == 9 and now.minute >= 30)
        spot_v5 = self._spot_velocity(300)

        def leg_velocity(pair: Dict[str, Any], side: str) -> Dict[str, Any]:
            leg = pair.get(side) or {}
            return leg.get("velocity_5m") or {}

        support_strike = nearest(self.orb_low or self.spot, 100) if self.spot else 0
        pe_build: List[Dict[str, Any]] = []
        pe_unwind: List[Dict[str, Any]] = []
        ce_push: List[Dict[str, Any]] = []

        for pair in paired_rows:
            strike = as_int(pair.get("strike"))
            pe_v5 = leg_velocity(pair, "PE")
            ce_v5 = leg_velocity(pair, "CE")
            pe_pct = as_float(pe_v5.get("pct"))
            ce_pct = as_float(ce_v5.get("pct"))
            near_support = self.orb_low and abs(strike - self.orb_low) <= 100
            near_spot = self.spot and abs(strike - self.spot) <= 150
            at_round = strike % 100 == 0

            if pe_pct >= PLAYBOOK_VELOCITY_ADD_PCT and (near_support or at_round):
                pe_build.append(
                    {
                        "strike": strike,
                        "read": pair.get("read"),
                        "velocity_5m": pe_v5,
                        "near": "ORB low" if near_support else "round",
                    }
                )
            if pe_pct <= PLAYBOOK_VELOCITY_UNWIND_PCT and (near_support or near_spot):
                pe_unwind.append({"strike": strike, "read": pair.get("read"), "velocity_5m": pe_v5})
            if ce_pct >= PLAYBOOK_VELOCITY_ADD_PCT and (near_spot or strike >= self.spot):
                ce_push.append({"strike": strike, "read": pair.get("read"), "velocity_5m": ce_v5})

        ce_writer_alerts = [
            alert for alert in abnormal_alerts if alert.get("option_type") == "CE" and alert.get("direction") == "WRITERS ADDING"
        ]
        pe_writer_alerts = [
            alert for alert in abnormal_alerts if alert.get("option_type") == "PE" and alert.get("direction") == "WRITERS ADDING"
        ]

        pairs_by_strike = {as_int(pair.get("strike")): pair for pair in paired_rows}
        pe_strike_watch = [
            self._analyze_pe_strike_row(pairs_by_strike.get(strike), strike, spot_v5)
            for strike in self._playbook_watch_strikes()
        ]
        pe_divergence = [
            row
            for row in pe_strike_watch
            if row.get("behavior") in {"PE_ADD_SPOT_FLAT", "PE_ADD_SPOT_DOWN"}
        ]
        pe_confirmed = [
            row for row in pe_strike_watch if row.get("behavior") == "PE_ADD_SPOT_UP"
        ]
        focus_23100_23200 = [
            row for row in pe_strike_watch if row.get("strike") in (23100, 23200)
        ]

        can_extend = False
        extend_reasons: List[str] = []
        if gap_type == "GAP_DOWN" and orb_high_reclaimed:
            has_confirmed_pe = bool(pe_confirmed) or any(
                row.get("strike") in (23100, 23200) and row.get("behavior") == "PE_ADD_SPOT_UP"
                for row in pe_strike_watch
            )
            no_divergence = not pe_divergence
            can_extend = has_confirmed_pe and not ce_push and no_divergence
            if orb_high_reclaimed:
                extend_reasons.append("ORB high reclaimed after gap-down")
            if pe_confirmed:
                extend_reasons.append(
                    f"PE + spot confirmed at {', '.join(str(row['strike']) for row in pe_confirmed[:3])}"
                )
            if pe_divergence:
                extend_reasons.append(
                    f"PE adding but spot flat/down at {', '.join(str(row['strike']) for row in pe_divergence)} — wait"
                )
            if self.spot > self.orb_high:
                extend_reasons.append("Spot holding above ORB high")
            if ce_push:
                extend_reasons.append("CE push present — extension risk")

        if in_orb_watch:
            can_extend = False

        phase = "WAITING"
        phase_note = "Waiting for open data"
        if in_orb_watch:
            orb_range = ""
            if self.orb_high and self.orb_low:
                orb_range = f" | ORB {self.orb_low:.0f}–{self.orb_high:.0f}"
            secs = orb_no_trade_seconds_remaining(now, is_expiry=is_expiry)
            until = "9:45" if is_expiry else "9:30"
            phase = "ORB_WATCH" if not is_expiry else "EXPIRY_WATCH"
            phase_note = (
                f"{'EXPIRY DAY — ' if is_expiry else ''}9:15–{until} IST — watch only, no fresh entries. "
                f"ORB forming{orb_range}. Trade window opens in {secs // 60}m {secs % 60}s."
            )
        elif gap_type == "GAP_DOWN":
            if not orb_low_held:
                phase, phase_note = "GAP_WEAK", "Gap-down and ORB low lost — avoid counter-trend long"
            elif not orb_high_reclaimed:
                phase, phase_note = "ORB_HOLD", "Gap-down but ORB low held — watch 9:30 PE rebuild for reclaim"
            elif pe_divergence and not pe_unwind:
                phase, phase_note = (
                    "PE_DIVERGENCE",
                    "PE adding at 23100/23200 but spot flat/down — support not confirmed, wait for spot follow-through",
                )
            elif in_930_window and pe_build:
                phase, phase_note = "PE_BUILD_930", "9:30 window — PE OI adding at support; check if rally can extend"
            elif can_extend and not pe_unwind:
                phase, phase_note = "EXTENSION", "ORB reclaimed + PE support building — rally can extend"
            elif pe_unwind and not ce_push:
                phase, phase_note = "PE_UNWIND", "PE OI unwinding — support leaving, tighten longs"
            elif ce_push or ce_writer_alerts:
                phase, phase_note = "CE_PUSH", "CE writers pushing near spot/high — fade / short watch"
            elif orb_high_reclaimed and self.spot < self.orb_high:
                phase, phase_note = "RECLAIM_FAILED", "Failed back below ORB high after reclaim"
            else:
                phase, phase_note = "ORB_RECLAIMED", "ORB high reclaimed — watch PE vs CE at 9:30+"
        elif gap_type == "GAP_UP":
            phase, phase_note = "GAP_UP", "Gap-up day — mirror playbook: CE build early, PE push on failure"
        else:
            cash_note = f" (cash open: {cash_gap_type} {gap_pts:+.0f} pts)"
            phase, phase_note = "FLAT_OPEN", f"No large gap — standard ORB + key-area velocity{cash_note}"

        checks = [
            {
                "id": "orb_no_trade",
                "label": "9:15–9:30 no-trade" if not is_expiry else "9:15–9:45 expiry watch",
                "status": "warn" if in_orb_watch else "ok",
                "detail": (
                    f"WATCH ONLY — {'expiry ORB trap + delta hedge' if is_expiry else 'ORB forming'}"
                    f"{f' ({self.orb_low:.0f}–{self.orb_high:.0f})' if self.orb_low and self.orb_high else ''}"
                    if in_orb_watch
                    else f"Window closed — entries from {'9:45' if is_expiry else '9:30'}"
                ),
            },
            {
                "id": "gap_context",
                "label": "Gap context",
                "status": "ok" if gap_type != "FLAT" else "info",
                "detail": (
                    f"Playbook {gap_type} {gap_pts:+.1f} pts ({gap_pct}%) | "
                    f"Cash open {cash_gap_type} @ {as_float(cash_open_gap.get('reference_open')):.2f}"
                    if as_float(cash_open_gap.get("reference_open")) > 0
                    else f"Playbook {gap_type} {gap_pts:+.1f} pts ({gap_pct}%) | Cash {cash_gap_type}"
                ),
            },
            {
                "id": "orb_low_held",
                "label": "ORB low held",
                "status": "ok" if orb_low_held else "bad",
                "detail": f"ORB low {self.orb_low:.2f} | day low {self.day_low:.2f}" if self.orb_low else "ORB not set",
            },
            {
                "id": "orb_high_reclaim",
                "label": "ORB high reclaim",
                "status": "ok" if orb_high_reclaimed else "warn",
                "detail": self.orb_high_reclaimed_at or f"Need spot >= ORB high {self.orb_high:.2f}",
            },
            {
                "id": "pe_23100_23200",
                "label": "23100 / 23200 PE vs spot",
                "status": "bad"
                if any(r.get("behavior") in {"PE_ADD_SPOT_FLAT", "PE_ADD_SPOT_DOWN"} for r in focus_23100_23200)
                else ("ok" if any(r.get("behavior") == "PE_ADD_SPOT_UP" for r in focus_23100_23200) else "warn"),
                "detail": " | ".join(
                    f"{row['strike']}: {row.get('behavior', 'NA').replace('PE_ADD_', 'PE+').replace('_', ' ')}"
                    for row in focus_23100_23200
                )
                or "Watch 23100 & 23200 PE OI vs spot together",
            },
            {
                "id": "pe_build_930",
                "label": "9:30 PE OI build",
                "status": "ok" if pe_build and (in_930_window or after_930) else "warn",
                "detail": pe_build[0]["strike"] if pe_build else "Watch round/ORB-low PE writer-adds",
            },
            {
                "id": "can_extend",
                "label": "Can extend further?",
                "status": "ok" if can_extend else ("bad" if ce_push else "warn"),
                "detail": "; ".join(extend_reasons) if extend_reasons else "Need reclaim + PE build without CE push",
            },
            {
                "id": "pe_unwind",
                "label": "PE unwinding",
                "status": "bad" if pe_unwind else "ok",
                "detail": f"Strikes {', '.join(str(row['strike']) for row in pe_unwind[:3])}" if pe_unwind else "No PE unwind at support yet",
            },
            {
                "id": "ce_push",
                "label": "CE being pushed",
                "status": "bad" if ce_push or ce_writer_alerts else "ok",
                "detail": f"Strikes {', '.join(str(row['strike']) for row in ce_push[:3])}" if ce_push else "No CE writer push at spot/high yet",
            },
        ]

        payload = {
            "phase": phase,
            "phase_note": phase_note,
            "gap_type": gap_type,
            "gap_points": gap_pts,
            "gap_pct": gap_pct,
            "cash_open_gap": cash_open_gap,
            "cash_open_gap_type": cash_gap_type,
            "orb_high": self.orb_high,
            "orb_low": self.orb_low,
            "orb_low_held": orb_low_held,
            "orb_high_reclaimed": orb_high_reclaimed,
            "orb_high_reclaimed_at": self.orb_high_reclaimed_at,
            "in_930_window": in_930_window,
            "no_trade_window_active": in_orb_watch,
            "no_trade_window": no_trade_window_label(is_expiry),
            "is_expiry_day": is_expiry,
            "no_trade_seconds_remaining": orb_no_trade_seconds_remaining(now, is_expiry=is_expiry) if in_orb_watch else 0,
            "can_extend": can_extend,
            "extend_reasons": extend_reasons,
            "support_strike_watch": support_strike,
            "pe_strike_watch": pe_strike_watch,
            "pe_strike_focus": focus_23100_23200,
            "pe_divergence": pe_divergence,
            "pe_confirmed": pe_confirmed,
            "spot_5m": spot_v5,
            "pe_build": pe_build[:5],
            "pe_unwind": pe_unwind[:5],
            "ce_push": ce_push[:5],
            "pe_writer_alerts": pe_writer_alerts[:3],
            "ce_writer_alerts": ce_writer_alerts[:3],
            "checks": checks,
            "playbook": "GAP_DOWN → ORB hold → reclaim → 23100/23200 PE vs spot → extension OR PE+spot divergence → unwind → CE push",
        }
        payload["bias_verdict"] = self._evaluate_bias_verdict(
            gap_type=gap_type,
            orb_low_held=orb_low_held,
            orb_high_reclaimed=orb_high_reclaimed,
            can_extend=can_extend,
            pe_divergence=pe_divergence,
            phase=phase,
            cash_open_gap=cash_open_gap,
        )
        payload["morning_context"] = self.refresh_morning_context()

        if pe_divergence:
            div_key = ",".join(str(row.get("strike")) for row in pe_divergence)
            self.journal.append_jsonl(
                self.journal._dated_path("nifty_playbook"),
                {"event": "PE_DIVERGENCE", "strikes": pe_divergence, "spot_5m": spot_v5},
                dedupe_key=f"pe_div:{div_key}:{ist_now()[:16]}",
            )

        if phase != self._playbook_phase:
            self._playbook_phase = phase
            self.journal.append_jsonl(
                self.journal._dated_path("nifty_playbook"),
                {"event": "PLAYBOOK_PHASE", **payload},
                dedupe_key=f"playbook:{phase}:{ist_now()[:16]}",
            )
        return payload

    def evaluate(self) -> Dict[str, Any]:
        """One full pipeline pass — MUTATES the engine: journals alerts,
        updates/generates paper signals, refreshes analytics and playbook.
        Returns the intermediates project() needs. Only the engine loop and
        replay may call this; HTTP routes must serve project() output."""
        if self.futures_eod_context is None:
            self.futures_eod_context = load_eod_futures_context(
                previous_trading_day(CLOCK.today())
            )
        self._restore_orb_levels()
        self.refresh_session_context()
        self.refresh_morning_context()
        self._maybe_snapshot_daily_levels()
        with self.lock:
            self._capture_session_open_gap()
            rows = [item.snapshot() for item in self.instruments.values()]
        rows.sort(key=lambda row: (row["strike"], row["option_type"]))
        by_strike: Dict[int, Dict[str, Dict[str, Any]]] = defaultdict(dict)
        for row in rows:
            by_strike[int(row["strike"])][str(row["option_type"])] = row

        paired_rows = []
        for strike, legs in sorted(by_strike.items()):
            ce = legs.get("CE")
            pe = legs.get("PE")
            ce_oi = as_int(ce.get("oi") if ce else 0)
            pe_oi = as_int(pe.get("oi") if pe else 0)
            ce_v5 = as_float((ce or {}).get("velocity_5m", {}).get("pct"))
            pe_v5 = as_float((pe or {}).get("velocity_5m", {}).get("pct"))
            if ce_v5 > 2 and pe_v5 <= 1:
                read = "CE writers adding"
            elif pe_v5 > 2 and ce_v5 <= 1:
                read = "PE writers adding"
            elif ce_v5 > 2 and pe_v5 > 2:
                read = "Both sides adding"
            elif ce_v5 < -2 and pe_v5 < -2:
                read = "Both sides unwinding"
            elif ce_v5 < -2:
                read = "CE unwinding"
            elif pe_v5 < -2:
                read = "PE unwinding"
            else:
                read = "Stable / mixed"
            paired_rows.append(
                {
                    "strike": strike,
                    "ce": ce,
                    "pe": pe,
                    "ce_minus_pe_oi": ce_oi - pe_oi,
                    "read": read,
                }
            )

        strongest_ce_add = sorted(
            [row for row in rows if row["option_type"] == "CE"],
            key=lambda row: row["velocity_5m"]["delta"],
            reverse=True,
        )[:3]
        strongest_pe_add = sorted(
            [row for row in rows if row["option_type"] == "PE"],
            key=lambda row: row["velocity_5m"]["delta"],
            reverse=True,
        )[:3]

        positive_1m = [row["velocity_1m"]["delta"] for row in rows if row.get("oi", 0) > 0 and row["velocity_1m"]["delta"] > 0]
        positive_5m = [row["velocity_5m"]["delta"] for row in rows if row.get("oi", 0) > 0 and row["velocity_5m"]["delta"] > 0]
        positive_15m = [row["velocity_15m"]["delta"] for row in rows if row.get("oi", 0) > 0 and row["velocity_15m"]["delta"] > 0]
        median_1m = statistics.median(positive_1m) if positive_1m else 0
        median_5m = statistics.median(positive_5m) if positive_5m else 0
        median_15m = statistics.median(positive_15m) if positive_15m else 0
        abnormal_alerts = []
        for row in rows:
            if row.get("oi", 0) <= 0:
                continue
            v1 = row["velocity_1m"]
            v5 = row["velocity_5m"]
            v15 = row["velocity_15m"]
            distance_pct = abs(self.spot - row["strike"]) / self.spot * 100 if self.spot else 999.0
            key_area_reasons = self._key_area_reasons(row, rows)
            key_area = bool(key_area_reasons)
            recent = row.get("recent_1m_deltas", [])
            positive_recent = [entry for entry in recent if entry["oi_delta"] > 0]
            volume_positive_recent = [entry for entry in positive_recent if entry["volume_delta"] > 0]
            repeated_add = len(positive_recent) >= MIN_POSITIVE_MINUTE_ADDS
            volume_confirmed = len(volume_positive_recent) >= MIN_VOLUME_CONFIRMED_MINUTES
            sustained_enough = len(recent) >= SUSTAINED_ADD_MINUTES
            chain_outlier = v5["delta"] >= max(200000, median_5m * 3) or v1["delta"] >= max(75000, median_1m * 3)
            pct_outlier = v5["pct"] >= 8 or v1["pct"] >= 4
            price_confirmed = (row["option_type"] == "CE" and self.spot <= row["strike"]) or (
                row["option_type"] == "PE" and self.spot >= row["strike"]
            )
            if not (key_area and sustained_enough and repeated_add and volume_confirmed and (chain_outlier or pct_outlier)):
                continue
            direction = "WRITERS ADDING" if price_confirmed else "OI ADDING - PRICE NOT CONFIRMED"
            reason_parts = []
            if chain_outlier:
                reason_parts.append("chain outlier")
            if pct_outlier:
                reason_parts.append("pct outlier")
            reason_parts.append(f"{len(positive_recent)}/{len(recent)} sustained add")
            reason_parts.append(f"{distance_pct:.2f}% from spot")
            reason_parts.append("key: " + ", ".join(key_area_reasons))
            abnormal_alerts.append(
                {
                    "contract": row["tradingsymbol"],
                    "strike": row["strike"],
                    "option_type": row["option_type"],
                    "last_price": row["last_price"],
                    "oi": row["oi"],
                    "velocity_1m": v1,
                    "velocity_5m": v5,
                    "velocity_15m": v15,
                    "recent_1m_deltas": recent,
                    "distance_from_spot_pct": round(distance_pct, 3),
                    "key_area": key_area,
                    "key_area_reasons": key_area_reasons,
                    "direction": direction,
                    "reason": ", ".join(reason_parts),
                }
            )
        abnormal_alerts.sort(
            key=lambda row: max(
                abs(row["velocity_5m"]["delta"]),
                abs(row["velocity_1m"]["delta"]),
                abs(row["velocity_15m"]["delta"]),
            ),
            reverse=True,
        )
        for alert in abnormal_alerts:
            kind = "WRITER_ADD" if alert.get("direction") == "WRITERS ADDING" else "OI_UNCONFIRMED"
            self._record_behavior_event(
                kind,
                str(alert.get("contract")),
                strike=as_int(alert.get("strike")),
                option_type=str(alert.get("option_type")),
                direction=str(alert.get("direction")),
                reason=str(alert.get("reason")),
                key_area_reasons=alert.get("key_area_reasons"),
            )
            self.journal.append_alert(alert)
        self.last_abnormal_alerts = abnormal_alerts[:20]
        gamma_state = self._detect_gamma_blast(paired_rows)
        active_gamma = str(gamma_state.get("active_signal") or "NONE")
        if active_gamma != self._last_gamma_signal:
            self._last_gamma_signal = active_gamma
            self.journal.append_gamma_state(gamma_state)
        self._update_open_signals(rows, paired_rows)
        self._refresh_options_analytics(paired_rows)
        playbook = self._detect_intraday_playbook(rows, paired_rows, abnormal_alerts)
        self._maybe_generate_signals(abnormal_alerts, rows, paired_rows, playbook=playbook)
        return {
            "rows": rows,
            "paired_rows": paired_rows,
            "strongest_ce_add": strongest_ce_add,
            "strongest_pe_add": strongest_pe_add,
            "abnormal_alerts": abnormal_alerts,
            "median_1m": median_1m,
            "median_5m": median_5m,
            "median_15m": median_15m,
            "gamma_state": gamma_state,
            "playbook": playbook,
        }

    def project(self, ev: Dict[str, Any]) -> Dict[str, Any]:
        """Assemble the API payload from evaluate()'s intermediates.
        Read-only apart from the futures_layer display cache."""
        rows = ev["rows"]
        paired_rows = ev["paired_rows"]
        strongest_ce_add = ev["strongest_ce_add"]
        strongest_pe_add = ev["strongest_pe_add"]
        abnormal_alerts = ev["abnormal_alerts"]
        median_1m = ev["median_1m"]
        median_5m = ev["median_5m"]
        median_15m = ev["median_15m"]
        gamma_state = ev["gamma_state"]
        playbook = ev["playbook"]
        is_expiry = _is_expiry_day(self)
        gate_now = CLOCK.now()

        tick_age_sec = round(CLOCK.time() - self.last_tick_ts, 1) if self.last_tick_ts else None
        stream_alive = tick_age_sec is not None and tick_age_sec <= 20
        spot_v5 = self._spot_velocity(300)
        # Under the lock: build_futures_layer reads each future's history deque,
        # which the ticker thread appends to in update_ticks. Without the lock the
        # two race -> "RuntimeError: deque mutated during iteration". Options rows
        # above are already read under the lock for the same reason.
        with self.lock:
            futures_list = self._future_instrument_list()
            futures_layer = build_futures_layer(
                futures_list,
                spot=self.spot,
                spot_v5=spot_v5,
                eod_context=self.futures_eod_context,
            )
        self.futures_layer = futures_layer

        with self.lock:
            return {
                "status": self.status,
                "error": self.error,
                "stream_alive": stream_alive,
                "last_tick_at": self.last_tick_at,
                "tick_age_sec": tick_age_sec,
                "started_at": self.started_at,
                "server_time": ist_now(),
                "spot": self.spot,
                "levels": {
                    "open": self.spot_open,
                    "day_high": self.day_high,
                    "day_low": self.day_low,
                    "prev_close": self.prev_close,
                    "orb_high": self.orb_high,
                    "orb_low": self.orb_low,
                    **{
                        key: value
                        for key, value in ((self.session_context or {}).get("technical_levels") or {}).items()
                        if key.startswith("ema_") and not key.endswith("_dist") and not key.endswith("_dist_pct")
                    },
                },
                "expiry": self.expiry,
                "instrument_count": len(self.instruments),
                "futures_count": len(self.futures),
                "futures_layer": futures_layer,
                "rows": rows,
                "paired_rows": paired_rows,
                "strongest_ce_add": strongest_ce_add,
                "strongest_pe_add": strongest_pe_add,
                "abnormal_alerts": abnormal_alerts[:10],
                "abnormal_baseline": {
                    "median_positive_1m_delta": median_1m,
                    "median_positive_5m_delta": median_5m,
                    "median_positive_15m_delta": median_15m,
                },
                "signal_rules": {
                    "key_area_distance_pct": KEY_AREA_DISTANCE_PCT,
                    "sustained_add_minutes": SUSTAINED_ADD_MINUTES,
                    "min_positive_minute_adds": MIN_POSITIVE_MINUTE_ADDS,
                    "min_volume_confirmed_minutes": MIN_VOLUME_CONFIRMED_MINUTES,
                    "max_open_signals": MAX_OPEN_SIGNALS,
                    "max_strike_distance_pts": MAX_SIGNAL_STRIKE_DISTANCE_PTS,
                    "min_open_strike_spacing": MIN_OPEN_STRIKE_SPACING,
                    "single_direction_book": SINGLE_DIRECTION_BOOK,
                    "block_same_thesis_stack": BLOCK_SAME_THESIS_STACK,
                    "require_pe_spot_confirm_for_buy_ce": REQUIRE_PE_SPOT_CONFIRM_FOR_BUY_CE,
                    "require_spot_weak_for_buy_pe": REQUIRE_SPOT_WEAK_FOR_BUY_PE,
                    "is_expiry_day": is_expiry,
                    "orb_no_trade_window": no_trade_window_label(is_expiry),
                    "orb_no_trade_active": is_no_trade_window(gate_now, is_expiry=is_expiry),
                    "orb_no_trade_seconds_remaining": orb_no_trade_seconds_remaining(gate_now, is_expiry=is_expiry),
                    "nifty_options_blocked": is_nifty_options_blocked(gate_now, is_expiry=is_expiry),
                    "late_session_cutoff": f"{LATE_SESSION_SIGNAL_CUTOFF[0]:02d}:{LATE_SESSION_SIGNAL_CUTOFF[1]:02d} IST",
                    "late_session_active": is_late_session(gate_now),
                    "fresh_entries_allowed": (
                        not is_no_trade_window(gate_now, is_expiry=is_expiry)
                        and not is_nifty_options_blocked(gate_now, is_expiry=is_expiry)
                        and not is_late_session(gate_now)
                    ),
                    "gap_playbook_threshold": GAP_PLAYBOOK_THRESHOLD,
                    "mild_gap_threshold": MILD_GAP_THRESHOLD,
                    "confluence_min_score": TRADE_MIN_CONFLUENCE,
                },
                "desk_principles": DESK_PRINCIPLES,
                "morning_context": self.morning_context,
                "key_levels": self.morning_context.get("key_levels") or {},
                "session_context": self.session_context,
                "commission_config": {
                    "lot_size": self.commission_cfg.lot_size,
                    "brokerage_per_order": self.commission_cfg.brokerage_per_order,
                    "min_net_profit_multiple": self.commission_cfg.min_net_profit_multiple,
                    "min_gross_rupees": self.commission_cfg.min_gross_rupees,
                },
                "rejected_signals": self.rejected_signals[:20],
                "signal_candidates": self.signal_candidates[:25],
                "signal_candidates_journal": str(
                    self.journal._dated_path("nifty_signal_candidates")
                ),
                "paper_trades_journal": str(self.journal._dated_path("nifty_paper_trades")),
                "journal_summary": self.journal.journal_day_summary(),
                "gamma_blast": gamma_state,
                "options_analytics": self.options_analytics,
                "intraday_playbook": playbook,
                "signals": self.signals[:100],
                "signal_journal_file": str(SIGNAL_JOURNAL_FILE),
                "data_store_file": str(self.data_store.db_path) if self.data_store else None,
            }

    def snapshot(self) -> Dict[str, Any]:
        """Evaluate then project — the legacy per-poll entry point. With the
        engine loop on, HTTP serves the cached projection instead."""
        return self.project(self.evaluate())

    def _detect_gamma_blast(self, paired_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        zones = []
        for pair in paired_rows:
            strike = as_float(pair.get("strike"))
            if not self.spot or not strike:
                continue
            distance_pct = abs(self.spot - strike) / self.spot * 100
            if distance_pct > GAMMA_NEAR_SPOT_PCT:
                continue
            ce = pair.get("ce") or {}
            pe = pair.get("pe") or {}
            ce_oi = as_int(ce.get("oi"))
            pe_oi = as_int(pe.get("oi"))
            ce_v5 = ce.get("velocity_5m") or {}
            pe_v5 = pe.get("velocity_5m") or {}
            ce_delta = as_int(ce_v5.get("delta"))
            pe_delta = as_int(pe_v5.get("delta"))
            compression = ce_oi >= GAMMA_HEAVY_OI_MIN and pe_oi >= GAMMA_HEAVY_OI_MIN
            signal = "OBSERVE"
            direction = "NONE"
            if compression:
                signal = "COMPRESSION"
                if ce_delta <= -GAMMA_UNWIND_DELTA_MIN and pe_delta >= 0 and self.spot >= strike:
                    signal = "GAMMA_BLAST_UP_RISK"
                    direction = "UP"
                elif pe_delta <= -GAMMA_UNWIND_DELTA_MIN and ce_delta >= 0 and self.spot <= strike:
                    signal = "GAMMA_BLAST_DOWN_RISK"
                    direction = "DOWN"
                elif ce_delta <= -GAMMA_UNWIND_DELTA_MIN and pe_delta <= -GAMMA_UNWIND_DELTA_MIN:
                    signal = "EXPIRY_DECAY_UNWIND"
                    direction = "MIXED"
            zones.append(
                {
                    "strike": pair.get("strike"),
                    "distance_from_spot_pct": round(distance_pct, 3),
                    "ce_oi": ce_oi,
                    "pe_oi": pe_oi,
                    "ce_5m_delta": ce_delta,
                    "pe_5m_delta": pe_delta,
                    "compression": compression,
                    "signal": signal,
                    "direction": direction,
                }
            )
        zones.sort(
            key=lambda zone: (
                zone["signal"] not in {"GAMMA_BLAST_UP_RISK", "GAMMA_BLAST_DOWN_RISK"},
                zone["distance_from_spot_pct"],
            )
        )
        active = next(
            (zone for zone in zones if zone["signal"] in {"GAMMA_BLAST_UP_RISK", "GAMMA_BLAST_DOWN_RISK"}),
            None,
        )
        compression_count = sum(1 for zone in zones if zone["compression"])
        return {
            "active_signal": active["signal"] if active else ("COMPRESSION" if compression_count else "NONE"),
            "active_direction": active["direction"] if active else "NONE",
            "compression_count": compression_count,
            "zones": zones,
            "rules": {
                "near_spot_pct": GAMMA_NEAR_SPOT_PCT,
                "heavy_oi_min": GAMMA_HEAVY_OI_MIN,
                "unwind_delta_min": GAMMA_UNWIND_DELTA_MIN,
            },
        }


