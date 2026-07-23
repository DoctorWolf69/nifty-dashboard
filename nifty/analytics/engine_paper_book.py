#!/usr/bin/env python3
"""Four-engine paper ledgers — shared tape, isolated books, primary stays alert voice.

Ported faithfully from quant-desk-engine v4/ATLAS's engine_paper_book.py
(mentor-authored). No logic changed. Only adaptation: imports from
nifty.core.commission / nifty.analytics.trade_book / nifty.core.journal
(this session's ports/updates of the standalone nifty_commission /
nifty_trade_book / nifty_journal_store modules) instead of those top-level
modules, and CONFIG_PATH now resolves via nifty.paths.PROJECT_ROOT instead
of a bare __file__-relative parent.

L1 opens via the existing dashboard conviction path (primary).
L2 / EV1 / EV2 open silently on gate rising-edge and exit on stop/target/hold.

Shadow-model-comparison pattern: L2/EV1/EV2 gates key off `legacy_score_v2`
and `ev_model` fields (engine_gate_pass) that no upstream module in
nifty-dashboard produces yet — this is a documented, built-in null-safe
degradation identical in spirit to every other "accepts an optional input
that doesn't exist yet" module ported this session: `engine_gate_pass`
returns False for L2/EV1/EV2 whenever those fields are absent (`if not l2:
return False`, `if not fq: return False`), so those three books simply stay
empty and inert until/if a shadow-scoring pipeline populates those fields.
L1 (primary) is unaffected either way — it mirrors the dashboard's existing
conviction path, not this gate.

Not yet wired into the live pipeline.
Self-check: python -m nifty.analytics.engine_paper_book
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from nifty.paths import PROJECT_ROOT
from nifty.core.commission import CommissionConfig, enrich_signal_with_commission, net_pnl_rupees
from nifty.analytics.trade_book import build_trade_book_payload, sync_position_fields
from nifty.core.journal import NiftyJournalStore, ist_now, today_str

CONFIG_PATH = PROJECT_ROOT / "config" / "engine_books.json"

ENGINES = ("L1", "L2", "EV1", "EV2")
ORB_BLOCKERS = frozenset({"ORB_NO_TRADE", "ORB_RESTRICTION"})

DEFAULT_CONFIG: Dict[str, Any] = {
    "primary": "L1",
    "enabled": list(ENGINES),
    "max_open_per_book": 0,  # 0 = unlimited open theses per book
    "silent_alert": True,
    "orb_no_trade_enabled": True,
    "entry_cooldown_sec": 0,
    "allow_thesis_stack": True,
    "stop_fraction": 0.70,
    "max_hold_minutes": 90,
}


def load_engine_books_config(path: Path = CONFIG_PATH) -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if not path.exists():
        return cfg
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            cfg.update(loaded)
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def engines_agreed_from_candidate(
    candidate: Dict[str, Any],
    *,
    orb_no_trade_enabled: bool = True,
) -> List[str]:
    """Snapshot which gates pass on this candidate row."""
    agreed: List[str] = []
    for engine in ENGINES:
        if engine_gate_pass(candidate, engine, orb_no_trade_enabled=orb_no_trade_enabled):
            agreed.append(engine)
    return agreed


def _blockers(candidate: Dict[str, Any], *, orb_no_trade_enabled: bool) -> List[str]:
    raw = [str(b) for b in (candidate.get("blockers") or [])]
    if orb_no_trade_enabled:
        return raw
    return [b for b in raw if b not in ORB_BLOCKERS]


def engine_gate_pass(
    candidate: Dict[str, Any],
    engine: str,
    *,
    orb_no_trade_enabled: bool = True,
) -> bool:
    """Return True when this engine would trade the candidate."""
    engine = str(engine or "").upper()
    if engine == "L1":
        if orb_no_trade_enabled:
            return bool(candidate.get("paper_eligible"))
        ready = bool(candidate.get("confluence_ready"))
        if not ready:
            ready = int(candidate.get("total_score") or 0) >= int(candidate.get("paper_min_score") or 65)
        return ready and len(_blockers(candidate, orb_no_trade_enabled=False)) == 0

    if engine == "L2":
        l2 = candidate.get("legacy_score_v2") or {}
        if not l2:
            return False
        if orb_no_trade_enabled:
            return bool(l2.get("paper_eligible"))
        ready = bool(l2.get("confluence_ready"))
        if not ready:
            ready = int(l2.get("total_score") or 0) >= int(l2.get("min_score") or 65)
        hard = set(l2.get("hard_blockers") or []) - ORB_BLOCKERS
        return ready and not hard

    if engine == "EV1":
        if orb_no_trade_enabled:
            return bool(
                candidate.get("ev_trade_eligible")
                or (candidate.get("ev_model") or {}).get("trade_eligible")
            )
        # Soft-off ORB: reuse five_questions but allow risk if ORB was sole fatal
        return _ev_eligible_orb_soft(candidate, calibrated=False)

    if engine == "EV2":
        if orb_no_trade_enabled:
            return bool((candidate.get("ev_model") or {}).get("trade_eligible_calibrated"))
        return _ev_eligible_orb_soft(candidate, calibrated=True)

    return False


def _ev_eligible_orb_soft(candidate: Dict[str, Any], *, calibrated: bool) -> bool:
    ev = candidate.get("ev_model") or {}
    fq_key = "five_questions_calibrated" if calibrated else "five_questions"
    fq = dict(ev.get(fq_key) or ev.get("five_questions") or {})
    if not fq:
        return False
    blockers = _blockers(candidate, orb_no_trade_enabled=False)
    fatal = {
        "BOTH_SIDES_ADDING",
        "NEWS",
        "DATA_ERROR",
        "EXTREME_LIQUIDITY",
        "LATE_SESSION",
    }
    thesis = {
        "WRITER_NOT_CONFIRMED",
        "PE_SPOT_NOT_CONFIRMED",
        "SPOT_NOT_WEAK",
        "SPOT_NOT_WEAK_FOR_PE",
        "MARKET_PROFILE_CONFLICT",
        "GEX_DELTA_CONFLICT",
        "GEX_VOL_EXPANSION",
        "CROSS_EXPIRY_CONFLICT",
        "DEALER_CONFLICT",
        "STRIKE_INTENT_CONFLICT",
        "CHAIN_DIRECTION_CONFLICT",
        "LIQUIDITY_GRAB_CONFLICT",
        "VOL_REGIME_CONFLICT",
    }
    has_fatal = any(b in fatal for b in blockers)
    if has_fatal:
        return False
    n_thesis = sum(1 for b in blockers if b in thesis)
    risk = "REJECT" if has_fatal else ("HIGH" if n_thesis >= 2 else ("MEDIUM" if n_thesis else "LOW"))
    fq = dict(fq)
    fq["risk_acceptable"] = risk in {"LOW", "MEDIUM"}
    return all(bool(v) for v in fq.values()) and risk != "REJECT"


class EnginePaperBook:
    """One engine's isolated paper ledger (silent books)."""

    def __init__(
        self,
        engine: str,
        *,
        journal: NiftyJournalStore,
        config: Dict[str, Any],
        commission_cfg: Optional[CommissionConfig] = None,
    ) -> None:
        self.engine = engine
        self.journal = journal
        self.config = config
        self.commission_cfg = commission_cfg or CommissionConfig.from_env()
        self.open_trades: Dict[str, Dict[str, Any]] = {}
        self.closed_today: List[Dict[str, Any]] = []
        self._prev_elig: Dict[str, bool] = {}
        self._last_entry_ts: Dict[str, float] = {}
        self._next_id = 1
        self.book_role = "primary" if engine == str(config.get("primary") or "L1") else "silent"
        self._clock = ist_now  # overridable for historical replay

    def set_clock(self, label: str) -> None:
        """Set wall-clock label used for journal timestamps (replay)."""
        self._clock = lambda: label

    def restore_from_journal(self, day: Optional[date] = None) -> None:
        path = self.journal.engine_paper_path(self.engine, day)
        if not path.exists():
            return
        states: Dict[int, Dict[str, Any]] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = int(row.get("id") or 0)
            if sid <= 0:
                continue
            event = str(row.get("event") or "")
            if event == "SIGNAL_CLOSED":
                states[sid] = {**states.get(sid, {}), **row, "status": "CLOSED"}
            elif event in {"SIGNAL_GENERATED", "SIGNAL_UPDATE", "SIGNAL_STACK"}:
                states[sid] = {**states.get(sid, {}), **row}
                if states[sid].get("status") != "CLOSED":
                    states[sid]["status"] = "OPEN"
            self._next_id = max(self._next_id, sid + 1)
        self.open_trades.clear()
        self.closed_today.clear()
        for sig in states.values():
            sync_position_fields(sig, self.commission_cfg)
            if sig.get("status") == "OPEN":
                key = str(sig.get("signal_key") or "")
                if key:
                    self.open_trades[key] = sig
            else:
                self.closed_today.append(sig)

    def day_stats(self) -> Dict[str, Any]:
        closed = list(self.closed_today)
        opens = list(self.open_trades.values())

        def _net(trade: Dict[str, Any]) -> float:
            return float(trade.get("pnl_net_rupees") or 0)

        closed_net = sum(_net(t) for t in closed)
        open_mtm = sum(_net(t) for t in opens)
        wins = sum(1 for t in closed if _net(t) > 0)
        losses = sum(1 for t in closed if _net(t) <= 0)
        last = None
        if closed:
            last = closed[-1]
        elif opens:
            last = next(iter(opens))
        return {
            "engine": self.engine,
            "book_role": self.book_role,
            "open": len(opens),
            "closed": len(closed),
            "wins": wins,
            "losses": losses,
            "closed_net_pnl_rupees": round(closed_net, 2),
            "open_mtm_rupees": round(open_mtm, 2),
            "net_pnl_rupees": round(closed_net + open_mtm, 2),
            "last_fill": {
                "signal_key": (last or {}).get("signal_key"),
                "status": (last or {}).get("status"),
                "pnl_net_rupees": (last or {}).get("pnl_net_rupees"),
                "generated_at": (last or {}).get("generated_at"),
                "exit_time": (last or {}).get("exit_time"),
            }
            if last
            else None,
        }

    def process_candidates(
        self,
        candidates: List[Dict[str, Any]],
        rows_by_contract: Dict[str, Dict[str, Any]],
        *,
        allow_new_entries: bool,
        now_ts: Optional[float] = None,
    ) -> None:
        now_ts = now_ts or time.time()
        orb_on = bool(self.config.get("orb_no_trade_enabled", True))
        # 0 / negative = unlimited concurrent opens per book
        max_open = int(self.config.get("max_open_per_book") or 0)
        unlimited = max_open <= 0
        cooldown = float(self.config.get("entry_cooldown_sec") or 0)
        allow_stack = bool(self.config.get("allow_thesis_stack", True))
        stop_frac = float(self.config.get("stop_fraction") or 0.70)
        max_hold = float(self.config.get("max_hold_minutes") or 90) * 60.0

        # Mark open positions
        for key, trade in list(self.open_trades.items()):
            self._update_open(trade, rows_by_contract, now_ts=now_ts, max_hold=max_hold)

        if not allow_new_entries or self.book_role == "primary":
            # Primary L1 opens via dashboard conviction path — only track marks if we ever mirror
            return

        if not unlimited and len(self.open_trades) >= max_open:
            return

        for cand in candidates:
            key = str(cand.get("signal_key") or "")
            if not key:
                continue
            elig = engine_gate_pass(cand, self.engine, orb_no_trade_enabled=orb_on)
            was = self._prev_elig.get(key, False)
            self._prev_elig[key] = elig
            if not elig or was:
                continue
            last = self._last_entry_ts.get(key, 0.0)
            if cooldown > 0 and (now_ts - last) < cooldown:
                continue
            if key in self.open_trades:
                if allow_stack:
                    self._stack_trade(
                        self.open_trades[key],
                        cand,
                        stop_frac=stop_frac,
                        orb_on=orb_on,
                        now_ts=now_ts,
                    )
                continue
            if not unlimited and len(self.open_trades) >= max_open:
                break
            self._open_trade(cand, stop_frac=stop_frac, orb_on=orb_on, now_ts=now_ts)

    def _stack_trade(
        self,
        trade: Dict[str, Any],
        candidate: Dict[str, Any],
        *,
        stop_frac: float,
        orb_on: bool,
        now_ts: float,
    ) -> None:
        """Add one lot on rising-edge while already open — VWAP entry, larger size."""
        add_px = float(candidate.get("entry_price") or 0)
        if add_px <= 0:
            return
        add_qty = int(self.commission_cfg.lot_size)
        old_qty = int(trade.get("quantity") or trade.get("lot_size") or add_qty)
        old_px = float(trade.get("entry_price") or 0)
        if old_px <= 0 or old_qty <= 0 or add_qty <= 0:
            return
        total_qty = old_qty + add_qty
        vwap = (old_px * old_qty + add_px * add_qty) / total_qty
        stack_n = int(trade.get("stack_count") or trade.get("lots") or 1) + 1
        agreed = engines_agreed_from_candidate(candidate, orb_no_trade_enabled=orb_on)
        trade.update(
            {
                "quantity": total_qty,
                "lot_size": total_qty,
                "lots": stack_n,
                "entry_price": round(vwap, 2),
                "stop_price": round(vwap * stop_frac, 2),
                "stack_count": stack_n,
                "last_stack_at": self._clock(),
                "last_stack_price": add_px,
                "engines_agreed": agreed,
                "event": "SIGNAL_STACK",
                "status": "OPEN",
            }
        )
        target = float(candidate.get("target_price") or 0) or round(vwap * 1.50, 2)
        trade["target_price"] = target
        enriched = enrich_signal_with_commission(trade, self.commission_cfg)
        trade.update(enriched)
        sync_position_fields(trade, self.commission_cfg)
        trade["event"] = "SIGNAL_STACK"
        trade["status"] = "OPEN"
        key = str(trade.get("signal_key") or "")
        self._last_entry_ts[key] = now_ts
        self.journal.append_engine_paper(
            self.engine,
            {
                "event": "SIGNAL_STACK",
                "id": trade.get("id"),
                "engine": self.engine,
                "signal_key": key,
                "status": "OPEN",
                "stack_count": stack_n,
                "lots": stack_n,
                "lot_size": total_qty,
                "quantity": total_qty,
                "entry_price": trade.get("entry_price"),
                "last_stack_price": add_px,
                "engines_agreed": agreed,
                "recorded_at": self._clock(),
            },
        )

    def _open_trade(
        self,
        candidate: Dict[str, Any],
        *,
        stop_frac: float,
        orb_on: bool,
        now_ts: float,
    ) -> None:
        entry = float(candidate.get("entry_price") or 0)
        if entry <= 0:
            return
        key = str(candidate.get("signal_key") or "")
        agreed = engines_agreed_from_candidate(candidate, orb_no_trade_enabled=orb_on)
        target = float(candidate.get("target_price") or 0) or round(entry * 1.50, 2)
        signal = {
            "id": self._next_id,
            "engine": self.engine,
            "book_role": self.book_role,
            "engines_agreed": agreed,
            "signal_key": key,
            "status": "OPEN",
            "event": "SIGNAL_GENERATED",
            "generated_at": self._clock(),
            "spot": candidate.get("spot"),
            "writer_contract": candidate.get("writer_contract"),
            "writer_side": candidate.get("writer_side"),
            "strike": candidate.get("strike"),
            "decision": candidate.get("decision"),
            "entry_contract": candidate.get("entry_contract"),
            "entry_side": candidate.get("entry_side"),
            "entry_price": entry,
            "current_price": entry,
            "lot_size": self.commission_cfg.lot_size,
            "quantity": self.commission_cfg.lot_size,
            "lots": 1,
            "stack_count": 1,
            "stop_price": round(entry * stop_frac, 2),
            "target_price": target,
            "pnl_pct": 0.0,
            "paper_only": True,
            "confluence_score": candidate.get("total_score"),
            "confluence_grade": candidate.get("grade"),
            "silent_ledger": True,
            "entry_mode": "gate_rising_edge",
            "_opened_ts": now_ts,
        }
        signal = enrich_signal_with_commission(signal, self.commission_cfg)
        sync_position_fields(signal, self.commission_cfg)
        self._next_id += 1
        self.open_trades[key] = signal
        self._last_entry_ts[key] = now_ts
        self.journal.append_engine_paper(self.engine, {"event": "SIGNAL_GENERATED", **signal})

    def _update_open(
        self,
        trade: Dict[str, Any],
        rows_by_contract: Dict[str, Dict[str, Any]],
        *,
        now_ts: float,
        max_hold: float,
    ) -> None:
        contract = str(trade.get("entry_contract") or "")
        row = rows_by_contract.get(contract) or {}
        px = float(row.get("last_price") or 0)
        if px <= 0:
            px = float(trade.get("current_price") or trade.get("entry_price") or 0)
        if px <= 0:
            return
        trade["current_price"] = px
        enriched = enrich_signal_with_commission(trade, self.commission_cfg)
        trade.update(
            {
                "pnl_pct": enriched.get("pnl_pct"),
                "pnl_gross_rupees": enriched.get("pnl_gross_rupees"),
                "pnl_commission_rupees": enriched.get("pnl_commission_rupees"),
                "pnl_net_rupees": enriched.get("pnl_net_rupees"),
            }
        )
        stop = float(trade.get("stop_price") or 0)
        target = float(trade.get("target_price") or 0)
        opened = float(trade.get("_opened_ts") or 0)
        reason = None
        if target and px >= target:
            reason = "TARGET_HIT"
        elif stop and px <= stop:
            reason = "STOP_HIT"
        elif opened and (now_ts - opened) >= max_hold:
            reason = "MAX_HOLD"
        if reason:
            self._close_trade(trade, exit_price=px, reason=reason)
            return
        # periodic update (throttle via minute in dedupe is not needed — append lightly)
        # Only journal update occasionally is fine; skip dense updates to avoid journal blast
        last_journal = str(trade.get("_last_update_minute") or "")
        minute = self._clock()[:16]
        if minute != last_journal:
            trade["_last_update_minute"] = minute
            self.journal.append_engine_paper(
                self.engine,
                {
                    "event": "SIGNAL_UPDATE",
                    "id": trade.get("id"),
                    "engine": self.engine,
                    "signal_key": trade.get("signal_key"),
                    "status": "OPEN",
                    "current_price": px,
                    "pnl_pct": trade.get("pnl_pct"),
                    "engines_agreed": trade.get("engines_agreed"),
                    "recorded_at": self._clock(),
                },
            )

    def _close_trade(self, trade: Dict[str, Any], *, exit_price: float, reason: str) -> None:
        key = str(trade.get("signal_key") or "")
        entry = float(trade.get("entry_price") or 0)
        qty = int(trade.get("quantity") or trade.get("lot_size") or self.commission_cfg.lot_size)
        decision = str(trade.get("decision") or "")
        closed = net_pnl_rupees(entry, exit_price, qty, self.commission_cfg, decision=decision)
        trade.update(
            {
                "status": "CLOSED",
                "event": "SIGNAL_CLOSED",
                "exit_price": exit_price,
                "exit_time": self._clock(),
                "exit_reason": reason,
                "current_price": exit_price,
                "pnl_gross_rupees": closed["gross_rupees"],
                "pnl_commission_rupees": closed["commission_rupees"],
                "pnl_net_rupees": closed["net_rupees"],
            }
        )
        enriched = enrich_signal_with_commission(trade, self.commission_cfg)
        trade.update(
            {
                "pnl_pct": enriched.get("pnl_pct"),
                "commission": enriched.get("commission"),
            }
        )
        self.journal.append_engine_paper(self.engine, dict(trade))
        self.closed_today.append(dict(trade))
        self.open_trades.pop(key, None)

    def close_all_session_end(self, rows_by_contract: Dict[str, Dict[str, Any]]) -> None:
        for trade in list(self.open_trades.values()):
            contract = str(trade.get("entry_contract") or "")
            row = rows_by_contract.get(contract) or {}
            px = float(row.get("last_price") or trade.get("current_price") or trade.get("entry_price") or 0)
            self._close_trade(trade, exit_price=px, reason="SESSION_END")


class EnginePaperBooks:
    """Manager for L1 (attribution mirror) + silent L2/EV1/EV2 ledgers."""

    def __init__(
        self,
        journal: Optional[NiftyJournalStore] = None,
        commission_cfg: Optional[CommissionConfig] = None,
        config_path: Path = CONFIG_PATH,
    ) -> None:
        self.journal = journal or NiftyJournalStore()
        self.config = load_engine_books_config(config_path)
        self.commission_cfg = commission_cfg or CommissionConfig.from_env()
        self.books: Dict[str, EnginePaperBook] = {}
        enabled = [str(e).upper() for e in (self.config.get("enabled") or list(ENGINES))]
        for engine in ENGINES:
            if engine not in enabled and engine != str(self.config.get("primary") or "L1"):
                continue
            book = EnginePaperBook(
                engine,
                journal=self.journal,
                config=self.config,
                commission_cfg=self.commission_cfg,
            )
            book.restore_from_journal()
            self.books[engine] = book

    @property
    def primary(self) -> str:
        return str(self.config.get("primary") or "L1").upper()

    def orb_no_trade_enabled(self) -> bool:
        return bool(self.config.get("orb_no_trade_enabled", True))

    def process(
        self,
        candidates: List[Dict[str, Any]],
        rows: List[Dict[str, Any]],
        *,
        allow_new_entries: bool = True,
    ) -> None:
        rows_by_contract = {str(r.get("tradingsymbol") or ""): r for r in rows}
        for engine, book in self.books.items():
            if engine == self.primary:
                # Primary opens elsewhere; still update any mirrored open L1 silent copy if present
                for trade in list(book.open_trades.values()):
                    book._update_open(
                        trade,
                        rows_by_contract,
                        now_ts=time.time(),
                        max_hold=float(self.config.get("max_hold_minutes") or 90) * 60.0,
                    )
                continue
            book.process_candidates(
                candidates,
                rows_by_contract,
                allow_new_entries=allow_new_entries,
            )

    def tag_primary_open(self, signal: Dict[str, Any], candidate: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Enrich primary L1 fill with attribution fields (journaled via dashboard append)."""
        orb_on = self.orb_no_trade_enabled()
        if candidate:
            agreed = engines_agreed_from_candidate(candidate, orb_no_trade_enabled=orb_on)
        else:
            agreed = list(signal.get("engines_agreed") or ["L1"])
        if "L1" not in agreed:
            agreed = ["L1"] + [e for e in agreed if e != "L1"]
        signal["engine"] = self.primary
        signal["book_role"] = "primary"
        signal["engines_agreed"] = agreed
        book = self.books.get(self.primary)
        if book:
            key = str(signal.get("signal_key") or "")
            if key:
                book.open_trades[key] = dict(signal)
                book.open_trades[key]["status"] = "OPEN"
                try:
                    book._next_id = max(book._next_id, int(signal.get("id") or 0) + 1)
                except (TypeError, ValueError):
                    pass
        return signal

    def tag_primary_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Mirror primary book updates/closes into L1 engine journal with attribution fields."""
        event.setdefault("engine", self.primary)
        event.setdefault("book_role", "primary")
        self.journal.append_engine_paper(self.primary, dict(event))
        book = self.books.get(self.primary)
        if book:
            key = str(event.get("signal_key") or "")
            event_name = str(event.get("event") or "")
            if event_name == "SIGNAL_CLOSED" and key:
                closed = {**book.open_trades.get(key, {}), **event, "status": "CLOSED"}
                book.closed_today.append(closed)
                book.open_trades.pop(key, None)
            elif event_name in {"SIGNAL_GENERATED", "SIGNAL_UPDATE", "SIGNAL_STACK"} and key:
                if key in book.open_trades:
                    book.open_trades[key].update(event)
                    book.open_trades[key]["status"] = "OPEN"
                elif event_name == "SIGNAL_GENERATED":
                    book.open_trades[key] = {**event, "status": "OPEN"}
        return event

    def close_all_session_end(
        self,
        rows_by_contract: Dict[str, Dict[str, Any]],
        *,
        skip_primary: bool = False,
        engines: Optional[tuple] = None,
    ) -> int:
        closed = 0
        for engine, book in self.books.items():
            if skip_primary and engine == self.primary:
                continue
            if engines is not None and engine not in engines:
                continue
            before = len(book.open_trades)
            book.close_all_session_end(rows_by_contract)
            closed += before - len(book.open_trades)
        return closed

    def scoreboard(self) -> Dict[str, Any]:
        return {
            "config": {
                "primary": self.primary,
                "enabled": list(self.books.keys()),
                "orb_no_trade_enabled": self.orb_no_trade_enabled(),
                "max_open_per_book": self.config.get("max_open_per_book"),
                "entry_cooldown_sec": self.config.get("entry_cooldown_sec"),
                "allow_thesis_stack": self.config.get("allow_thesis_stack"),
                "silent_alert": self.config.get("silent_alert"),
            },
            "books": {engine: book.day_stats() for engine, book in self.books.items()},
            "as_of": ist_now(),
        }

    def unified_trade_book(
        self,
        primary_signals: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """One desk book: L1 primary + silent L2/EV1/EV2, each row tagged by engine."""
        rows: List[Dict[str, Any]] = []
        seen: Set[tuple] = set()

        def _add(row: Dict[str, Any], *, engine: str, book_role: str) -> None:
            payload = dict(row)
            payload["engine"] = str(engine or "").upper()
            payload["book_role"] = book_role
            key = (
                payload["engine"],
                payload.get("id"),
                payload.get("signal_key"),
                payload.get("generated_at") or payload.get("recorded_at"),
            )
            if key in seen:
                return
            seen.add(key)
            # Drop internal mark fields from UI payload
            payload.pop("_opened_ts", None)
            payload.pop("_last_update_minute", None)
            rows.append(payload)

        for signal in primary_signals or []:
            _add(
                signal,
                engine=str(signal.get("engine") or self.primary),
                book_role=str(signal.get("book_role") or "primary"),
            )

        for engine, book in self.books.items():
            role = book.book_role
            if engine == self.primary:
                # Primary lifecycle lives in dashboard signals; still surface L1 mirror
                # closes/opens if they somehow diverge.
                for trade in list(book.open_trades.values()) + list(book.closed_today):
                    _add(trade, engine=engine, book_role=role)
                continue
            for trade in list(book.open_trades.values()) + list(book.closed_today):
                _add(trade, engine=engine, book_role=role)

        def _sort_key(row: Dict[str, Any]) -> tuple:
            status = str(row.get("status") or "")
            open_rank = 0 if status == "OPEN" else 1
            return (open_rank, str(row.get("generated_at") or row.get("recorded_at") or ""), str(row.get("engine") or ""))

        rows.sort(key=_sort_key)
        return rows


def _selftest() -> None:
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="engine-paper-book-selftest-"))
    journal = NiftyJournalStore(journal_dir=tmp)
    cfg = CommissionConfig()
    config = dict(DEFAULT_CONFIG)

    # engine_gate_pass: L1 gates on paper_eligible under ORB-on.
    cand_eligible = {"signal_key": "23000:PE:BUY_CE", "paper_eligible": True, "entry_price": 100.0, "spot": 23050.0}
    assert engine_gate_pass(cand_eligible, "L1", orb_no_trade_enabled=True) is True
    assert engine_gate_pass({}, "L1", orb_no_trade_enabled=True) is False

    # L2/EV1/EV2 gracefully return False when their upstream fields don't exist yet.
    assert engine_gate_pass(cand_eligible, "L2", orb_no_trade_enabled=True) is False
    assert engine_gate_pass(cand_eligible, "EV1", orb_no_trade_enabled=True) is False
    assert engine_gate_pass(cand_eligible, "EV2", orb_no_trade_enabled=True) is False
    assert engine_gate_pass(cand_eligible, "UNKNOWN_ENGINE") is False

    # L2 activates once legacy_score_v2 is populated (future upstream capability).
    cand_with_l2 = {**cand_eligible, "legacy_score_v2": {"paper_eligible": True}}
    assert engine_gate_pass(cand_with_l2, "L2", orb_no_trade_enabled=True) is True

    agreed = engines_agreed_from_candidate(cand_with_l2, orb_no_trade_enabled=True)
    assert agreed == ["L1", "L2"]

    # A silent book (L2) opens on rising-edge gate pass, tracks P&L, and closes on target/stop.
    book = EnginePaperBook("L2", journal=journal, config=config, commission_cfg=cfg)
    book.set_clock("2026-07-23 10:00:00")
    rows_by_contract: Dict[str, Dict[str, Any]] = {}
    candidate = {
        "signal_key": "23000:PE:BUY_CE", "entry_price": 100.0, "spot": 23050.0,
        "entry_contract": "NIFTY23000CE", "decision": "BUY_CE", "strike": 23000,
        "legacy_score_v2": {"paper_eligible": True}, "total_score": 80, "grade": "A",
    }
    # `_prev_elig.get(key, False)` defaults False for a never-seen key, same as an
    # explicit prior False -> a candidate eligible on its very first sighting opens
    # immediately (not a genuine two-tick rising edge, since "never seen" and
    # "previously ineligible" are indistinguishable here).
    book.process_candidates([candidate], rows_by_contract, allow_new_entries=True, now_ts=1000.0)
    assert len(book.open_trades) == 1
    trade = next(iter(book.open_trades.values()))
    assert trade["entry_price"] == 100.0
    assert trade["stop_price"] == round(100.0 * 0.70, 2)

    # Second consecutive eligible tick is suppressed by the `was` check itself
    # (elig=True and was=True -> `continue` fires before the open-vs-stack branch).
    book.process_candidates([candidate], rows_by_contract, allow_new_entries=True, now_ts=1001.0)
    assert len(book.open_trades) == 1  # unchanged — no duplicate open

    # Price rallies to target -> auto-closes TARGET_HIT.
    rows_by_contract["NIFTY23000CE"] = {"last_price": trade["target_price"] + 1}
    book.process_candidates([], rows_by_contract, allow_new_entries=True, now_ts=1002.0)
    assert len(book.open_trades) == 0
    assert len(book.closed_today) == 1
    assert book.closed_today[0]["exit_reason"] == "TARGET_HIT"

    stats = book.day_stats()
    assert stats["closed"] == 1 and stats["wins"] == 1

    # restore_from_journal round-trips a fresh book from what was just journaled.
    book2 = EnginePaperBook("L2", journal=journal, config=config, commission_cfg=cfg)
    book2.restore_from_journal()
    assert len(book2.closed_today) == 1

    # EnginePaperBooks manager: L1 primary + silent books, scoreboard + unified book.
    manager = EnginePaperBooks(journal=NiftyJournalStore(journal_dir=Path(tempfile.mkdtemp(prefix="epb2-"))), commission_cfg=cfg)
    assert manager.primary == "L1"
    board = manager.scoreboard()
    assert set(board["books"].keys()) == {"L1", "L2", "EV1", "EV2"}

    tagged = manager.tag_primary_open({"signal_key": "k1", "entry_price": 100.0})
    assert tagged["engine"] == "L1"
    assert "L1" in tagged["engines_agreed"]

    unified = manager.unified_trade_book(primary_signals=[tagged])
    assert any(row["signal_key"] == "k1" for row in unified)

    print("[analytics.engine_paper_book] selftest OK: gate pass, silent book open/close, restore, manager scoreboard")


if __name__ == "__main__":
    _selftest()
