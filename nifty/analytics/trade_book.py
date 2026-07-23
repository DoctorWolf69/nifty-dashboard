#!/usr/bin/env python3
"""
Broker-standard paper trade book (NSE F&O / Zerodha-style).

Ported faithfully from quant-desk-engine v4/ATLAS's nifty_trade_book.py
(mentor-authored). No logic changed. Only adaptation: imports
CommissionConfig/net_pnl_rupees/option_pnl_points from
nifty.core.commission (already updated this session to add the
decision-aware option_pnl_points this file needs) instead of the
standalone nifty_commission module.

One row per open or closed position with explicit lots x lot_size =
quantity. P&L: gross = premium_points x quantity; net = gross - round-trip
charges. `aggregate_engine_book_stats`/`aggregate_open_capital_summary`
assume up to 4 parallel "engine" ledgers (L1/L2/EV1/EV2) — this is the
mentor's shadow-model-comparison pattern (also seen in nifty_journal_store.py's
evolution and nifty_opportunity_ranking.py's shadow_comparison): running
alternate scoring engines side by side against the same tape without
letting them affect the live paper book, so their hypothetical performance
can be compared later. nifty-dashboard only runs one live engine today, so
rows without an explicit "engine" field will simply not match any of the
four buckets — a documented no-op, not an error.

Not yet wired into the live pipeline.
Self-check: python -m nifty.analytics.trade_book
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd

from nifty.core.commission import CommissionConfig, net_pnl_rupees, option_pnl_points

# Zerodha / NSE blotter column order
TRADE_BOOK_COLUMNS: List[str] = [
    "trade_id",
    "engine",
    "status",
    "symbol",
    "side",
    "strike",
    "product",
    "lots",
    "lot_size",
    "quantity",
    "entry_time",
    "exit_time",
    "entry_price",
    "exit_price",
    "mark_price",
    "points_pnl",
    "gross_pnl",
    "charges",
    "net_pnl",
    "pnl_pct",
    "exit_reason",
    "engines_agreed",
    "book_role",
]


@dataclass(frozen=True)
class TradeBookRow:
    trade_id: int
    engine: str
    status: str
    symbol: str
    side: str
    strike: int
    product: str
    lots: int
    lot_size: int
    quantity: int
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    mark_price: float
    points_pnl: float
    gross_pnl: float
    charges: float
    net_pnl: float
    pnl_pct: float
    exit_reason: str
    engines_agreed: str
    book_role: str


def sync_position_fields(
    trade: Dict[str, Any],
    cfg: Optional[CommissionConfig] = None,
) -> Dict[str, Any]:
    """
    Normalize position sizing on a live signal dict.

    `quantity` is authoritative (total units). `lots` = quantity // lot_size.
    Keeps legacy `lot_size` field equal to total quantity for journal compat.
    """
    config = cfg or CommissionConfig.from_env()
    contract_lot_size = int(config.lot_size)
    quantity = int(trade.get("quantity") or trade.get("lot_size") or contract_lot_size)
    if quantity >= contract_lot_size and quantity % contract_lot_size == 0:
        lots = quantity // contract_lot_size
    else:
        stacks = int(trade.get("stack_count") or 0)
        lots = stacks if stacks > 0 else max(1, round(quantity / contract_lot_size))
        quantity = lots * contract_lot_size
    trade["contract_lot_size"] = contract_lot_size
    trade["quantity"] = quantity
    trade["lots"] = lots
    trade["stack_count"] = lots
    trade["lot_size"] = quantity  # legacy journal field = total qty
    return trade


def normalize_paper_trade_row(
    signal: Dict[str, Any],
    cfg: Optional[CommissionConfig] = None,
) -> Dict[str, Any]:
    """Full trade-book row with sizing + mark-to-market P&L."""
    config = cfg or CommissionConfig.from_env()
    out = sync_position_fields(dict(signal), config)
    entry = float(out.get("entry_price") or 0)
    decision = str(out.get("decision") or "")
    status = str(out.get("status") or "")
    exit_px = float(out.get("exit_price") or 0)
    mark = float(out.get("current_price") or entry)
    mark_or_exit = exit_px if status == "CLOSED" and exit_px > 0 else mark
    qty = int(out["quantity"])
    out["mark_price"] = round(mark, 2)
    if entry > 0 and mark_or_exit > 0:
        pts = option_pnl_points(entry, mark_or_exit, decision)
        pnl = net_pnl_rupees(entry, mark_or_exit, qty, config, decision=decision)
        out["pnl_points"] = round(pts, 2)
        out["points_pnl"] = out["pnl_points"]
        out["pnl_gross_rupees"] = pnl["gross_rupees"]
        out["gross_pnl"] = pnl["gross_rupees"]
        out["pnl_commission_rupees"] = pnl["commission_rupees"]
        out["charges"] = pnl["commission_rupees"]
        out["pnl_net_rupees"] = pnl["net_rupees"]
        out["net_pnl"] = pnl["net_rupees"]
        out["pnl_pct"] = round((pts / entry) * 100, 2)
        if status == "CLOSED" and exit_px > 0:
            out["exit_price"] = round(exit_px, 2)
    qty_cap = int(out.get("quantity") or 0)
    entry_px = float(out.get("entry_price") or 0)
    mark_px = float(out.get("mark_price") or out.get("current_price") or entry_px)
    if qty_cap > 0 and entry_px > 0:
        out["capital_at_entry_rupees"] = round(entry_px * qty_cap, 2)
        out["capital_at_mark_rupees"] = round(mark_px * qty_cap, 2)
    side = str(out.get("decision") or out.get("entry_side") or "")
    out["side"] = side
    out["symbol"] = str(out.get("entry_contract") or "")
    out["product"] = "NRML"
    agreed = out.get("engines_agreed") or []
    if isinstance(agreed, str):
        out["engines_agreed"] = [x.strip() for x in agreed.split(",") if x.strip()]
    elif not isinstance(agreed, list):
        out["engines_agreed"] = [str(agreed)] if agreed else []
    return out


def to_trade_book_row(signal: Dict[str, Any], cfg: Optional[CommissionConfig] = None) -> TradeBookRow:
    row = normalize_paper_trade_row(signal, cfg)
    return TradeBookRow(
        trade_id=int(row.get("id") or 0),
        engine=str(row.get("engine") or ""),
        status=str(row.get("status") or ""),
        symbol=str(row.get("symbol") or row.get("entry_contract") or ""),
        side=str(row.get("side") or row.get("decision") or ""),
        strike=int(row.get("strike") or 0),
        product=str(row.get("product") or "NRML"),
        lots=int(row.get("lots") or 1),
        lot_size=int(row.get("contract_lot_size") or 65),
        quantity=int(row.get("quantity") or 0),
        entry_time=str(row.get("generated_at") or row.get("recorded_at") or ""),
        exit_time=str(row.get("exit_time") or ""),
        entry_price=float(row.get("entry_price") or 0),
        exit_price=float(row.get("exit_price") or 0),
        mark_price=float(row.get("mark_price") or row.get("current_price") or 0),
        points_pnl=float(row.get("points_pnl") or row.get("pnl_points") or 0),
        gross_pnl=float(row.get("gross_pnl") or row.get("pnl_gross_rupees") or 0),
        charges=float(row.get("charges") or row.get("pnl_commission_rupees") or 0),
        net_pnl=float(row.get("net_pnl") or row.get("pnl_net_rupees") or 0),
        pnl_pct=float(row.get("pnl_pct") or 0),
        exit_reason=str(row.get("exit_reason") or ""),
        engines_agreed=(
            ",".join(str(x) for x in agreed)
            if isinstance(agreed := row.get("engines_agreed"), list)
            else str(agreed or "")
        ),
        book_role=str(row.get("book_role") or ""),
    )


def build_trade_book_dataframe(
    rows: Sequence[Dict[str, Any]],
    cfg: Optional[CommissionConfig] = None,
) -> pd.DataFrame:
    """pandas DataFrame in broker-standard column order."""
    records = [asdict(to_trade_book_row(row, cfg)) for row in rows]
    if not records:
        return pd.DataFrame(columns=TRADE_BOOK_COLUMNS)
    df = pd.DataFrame(records)
    for col in TRADE_BOOK_COLUMNS:
        if col not in df.columns:
            df[col] = None
    return df[TRADE_BOOK_COLUMNS]


def build_trade_book_payload(
    rows: Sequence[Dict[str, Any]],
    cfg: Optional[CommissionConfig] = None,
) -> List[Dict[str, Any]]:
    """API-friendly dict rows after normalization."""
    return [normalize_paper_trade_row(dict(row), cfg) for row in rows]


def attach_live_execution_prices(
    rows: Sequence[Dict[str, Any]],
    fills_by_signal_id: Optional[Dict[int, Dict[str, Any]]] = None,
    *,
    broker_avg_by_symbol: Optional[Dict[str, float]] = None,
) -> List[Dict[str, Any]]:
    """
    Side-by-side paper vs Kite entry for the trade book UI.

    paper_entry = engine LTP at signal fire; kite_fill = broker average_price when known.
    slippage_pts = kite_fill - paper_entry (positive = paid more than paper assumed).
    """
    fills = fills_by_signal_id or {}
    broker = {str(k or "").upper(): float(v) for k, v in (broker_avg_by_symbol or {}).items() if v}
    out: List[Dict[str, Any]] = []
    for row in rows:
        payload = dict(row)
        paper = float(payload.get("entry_price") or 0)
        payload["paper_entry"] = round(paper, 2) if paper > 0 else None

        sid = int(payload.get("id") or 0)
        fill_info = fills.get(sid) or {}
        kite_raw = fill_info.get("kite_fill")
        fill_source = None
        if kite_raw in (None, "", 0):
            live_exec = payload.get("live_execution") or {}
            if isinstance(live_exec, dict):
                kite_raw = live_exec.get("kite_fill") or live_exec.get("fill_price")
                if kite_raw:
                    fill_source = "atlas_execution"
        if kite_raw not in (None, "", 0):
            fill_source = fill_source or "atlas_execution"
        contract = str(payload.get("entry_contract") or "").upper()
        if kite_raw in (None, "", 0) and contract and contract in broker:
            kite_raw = broker[contract]
            fill_source = "broker_position"
        kite = float(kite_raw) if kite_raw not in (None, "", 0) else 0.0
        payload["kite_fill"] = round(kite, 2) if kite > 0 else None
        payload["kite_fill_source"] = fill_source

        if paper > 0 and kite > 0:
            slip_pts = round(kite - paper, 2)
            payload["slippage_pts"] = slip_pts
            payload["slippage_pct"] = round(slip_pts / paper * 100, 2)
        else:
            payload["slippage_pts"] = None
            payload["slippage_pct"] = None

        if fill_info.get("status"):
            exec_status = fill_info.get("status")
        elif fill_info.get("dry_run"):
            exec_status = "DRY_RUN"
        elif payload.get("kite_fill"):
            exec_status = "BROKER" if fill_source == "broker_position" else "LIVE"
        elif payload.get("paper_only") or str(payload.get("book_role") or "") == "silent":
            exec_status = "PAPER_ONLY"
        else:
            exec_status = "PAPER_ONLY"
        payload["live_execution_status"] = exec_status
        out.append(payload)
    return out


def _dedupe_trade_rows(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One row per engine trade — prefer OPEN over CLOSED duplicate mirrors."""
    picked: Dict[tuple, Dict[str, Any]] = {}
    for row in rows:
        eng = str(row.get("engine") or "").upper()
        trade_id = int(row.get("id") or 0)
        key = (eng, trade_id) if trade_id > 0 else (eng, str(row.get("signal_key") or ""))
        prev = picked.get(key)
        if prev is None:
            picked[key] = row
            continue
        if str(row.get("status") or "") == "OPEN" and str(prev.get("status") or "") != "OPEN":
            picked[key] = row
    return list(picked.values())


def aggregate_engine_book_stats(
    rows: Sequence[Dict[str, Any]],
    *,
    engines: Sequence[str] = ("L1", "L2", "EV1", "EV2"),
) -> Dict[str, Dict[str, Any]]:
    """
    Live book summary: closed realized + open mark-to-market net per engine.
    """
    unique = _dedupe_trade_rows(rows)
    out: Dict[str, Dict[str, Any]] = {}
    for eng in engines:
        eng_u = str(eng).upper()
        eng_rows = [r for r in unique if str(r.get("engine") or "").upper() == eng_u]
        opens = [r for r in eng_rows if str(r.get("status") or "") == "OPEN"]
        closed = [r for r in eng_rows if str(r.get("status") or "") != "OPEN"]

        def _net(row: Dict[str, Any]) -> float:
            return float(row.get("net_pnl") or row.get("pnl_net_rupees") or 0)

        closed_net = sum(_net(r) for r in closed)
        open_mtm = sum(_net(r) for r in opens)

        def _cap_entry(row: Dict[str, Any]) -> float:
            return float(
                row.get("capital_at_entry_rupees")
                or (float(row.get("entry_price") or 0) * int(row.get("quantity") or 0))
            )

        def _cap_mark(row: Dict[str, Any]) -> float:
            return float(
                row.get("capital_at_mark_rupees")
                or (
                    float(row.get("mark_price") or row.get("current_price") or row.get("entry_price") or 0)
                    * int(row.get("quantity") or 0)
                )
            )

        open_capital_entry = sum(_cap_entry(r) for r in opens)
        open_capital_mark = sum(_cap_mark(r) for r in opens)
        wins = sum(1 for r in closed if _net(r) > 0)
        losses = sum(1 for r in closed if _net(r) <= 0)
        out[eng_u] = {
            "engine": eng_u,
            "open": len(opens),
            "closed": len(closed),
            "wins": wins,
            "losses": losses,
            "closed_net_pnl_rupees": round(closed_net, 2),
            "open_mtm_rupees": round(open_mtm, 2),
            "net_pnl_rupees": round(closed_net + open_mtm, 2),
            "open_capital_entry_rupees": round(open_capital_entry, 2),
            "open_capital_mark_rupees": round(open_capital_mark, 2),
        }
    return out


def aggregate_open_capital_summary(
    rows: Sequence[Dict[str, Any]],
    *,
    engines: Sequence[str] = ("L1", "L2", "EV1", "EV2"),
) -> Dict[str, Any]:
    """
    Total capital tied up in all open positions (premium x qty).

    Each engine book counts separately — four parallel books = four capital pools.
    entry = VWAP premium paid; mark = current premium x qty (MTM book value).
    """
    books = aggregate_engine_book_stats(rows, engines=engines)
    open_rows = [
        r
        for r in _dedupe_trade_rows(rows)
        if str(r.get("status") or "") == "OPEN"
    ]
    total_entry = sum(float(b.get("open_capital_entry_rupees") or 0) for b in books.values())
    total_mark = sum(float(b.get("open_capital_mark_rupees") or 0) for b in books.values())
    return {
        "open_positions": len(open_rows),
        "total_entry_rupees": round(total_entry, 2),
        "total_mark_rupees": round(total_mark, 2),
        "by_engine": {
            eng: {
                "open": books.get(eng, {}).get("open", 0),
                "entry_rupees": books.get(eng, {}).get("open_capital_entry_rupees", 0),
                "mark_rupees": books.get(eng, {}).get("open_capital_mark_rupees", 0),
            }
            for eng in engines
        },
        "formula": "capital = premium × quantity (NIFTY lot = 65 qty per lot)",
    }


def _selftest() -> None:
    cfg = CommissionConfig()

    # sync_position_fields: quantity authoritative, lots derived.
    trade = {"quantity": 130, "lot_size": 65}
    synced = sync_position_fields(dict(trade), cfg)
    assert synced["lots"] == 2 and synced["quantity"] == 130

    # Odd quantity not a clean multiple falls back to stack_count / rounding.
    odd = sync_position_fields({"quantity": 40, "stack_count": 0}, cfg)
    assert odd["lots"] == 1 and odd["quantity"] == 65  # rounds up to 1 lot

    open_signal = {
        "id": 1, "decision": "BUY_CE", "entry_price": 100.0, "current_price": 130.0,
        "status": "OPEN", "lot_size": 65, "entry_contract": "NIFTY23000CE",
        "generated_at": "2026-07-23 10:00:00",
    }
    row = normalize_paper_trade_row(open_signal, cfg)
    assert row["pnl_points"] == 30.0
    assert row["side"] == "BUY_CE"
    assert row["symbol"] == "NIFTY23000CE"
    assert row["capital_at_entry_rupees"] == 100.0 * 65

    trade_row = to_trade_book_row(open_signal, cfg)
    assert trade_row.trade_id == 1
    assert trade_row.points_pnl == 30.0

    df = build_trade_book_dataframe([open_signal], cfg)
    assert list(df.columns) == TRADE_BOOK_COLUMNS
    assert len(df) == 1

    empty_df = build_trade_book_dataframe([], cfg)
    assert list(empty_df.columns) == TRADE_BOOK_COLUMNS and len(empty_df) == 0

    payload = build_trade_book_payload([open_signal], cfg)
    assert payload[0]["pnl_pct"] == 30.0

    # attach_live_execution_prices: broker fill known -> slippage computed.
    attached = attach_live_execution_prices(
        [open_signal], broker_avg_by_symbol={"NIFTY23000CE": 101.5},
    )
    assert attached[0]["kite_fill"] == 101.5
    assert attached[0]["slippage_pts"] == round(101.5 - 100.0, 2)
    assert attached[0]["kite_fill_source"] == "broker_position"

    no_fill = attach_live_execution_prices([open_signal])
    assert no_fill[0]["kite_fill"] is None
    assert no_fill[0]["live_execution_status"] == "PAPER_ONLY"

    # Engine book aggregation across L1/L2 with dedup on OPEN-over-CLOSED mirrors.
    rows = [
        {"id": 1, "engine": "L1", "status": "OPEN", "entry_price": 100.0, "current_price": 130.0,
         "quantity": 65, "net_pnl": 1500.0},
        {"id": 1, "engine": "L1", "status": "CLOSED", "entry_price": 100.0, "exit_price": 90.0,
         "quantity": 65, "net_pnl": -700.0},  # older closed mirror of the same trade id — should lose to OPEN
        {"id": 2, "engine": "L2", "status": "CLOSED", "entry_price": 50.0, "exit_price": 80.0,
         "quantity": 65, "net_pnl": 1800.0},
    ]
    stats = aggregate_engine_book_stats(rows)
    assert set(stats.keys()) == {"L1", "L2", "EV1", "EV2"}
    assert stats["L1"]["open"] == 1 and stats["L1"]["closed"] == 0  # OPEN wins the dedup
    assert stats["L2"]["closed"] == 1 and stats["L2"]["wins"] == 1
    assert stats["EV1"]["open"] == 0 and stats["EV1"]["closed"] == 0  # no rows -> harmless zeroed bucket

    capital = aggregate_open_capital_summary(rows)
    assert capital["open_positions"] == 1
    assert capital["by_engine"]["L1"]["open"] == 1

    print("[analytics.trade_book] selftest OK: sizing, P&L normalization, dataframe, engine book aggregation")


if __name__ == "__main__":
    _selftest()
