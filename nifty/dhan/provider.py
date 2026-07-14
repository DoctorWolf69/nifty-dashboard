"""Dhan market-feed adapter - the hot-standby provider.

Bridge design (until Migration P4's InstrumentRef re-key lands): the engine
is keyed by Kite instrument tokens, so this adapter resolves each tracked
contract's Dhan security_id from the scrip master, subscribes on Dhan's
websocket, and emits **Kite-shaped tick dicts carrying the Kite token** -
exactly the impersonation trick replay already uses. The eventual re-key
converts both to canonical MarketTick emission.

Packet shapes verified against 473k archived packets (TRADING/DHAN
ticks.db; see API_AND_CALCULATIONS.md §3.2):
  Quote Data  - LTP/volume/OHLC, no OI, no depth (indices)
  Full Data   - everything incl. OI + 5-level combined depth (prices are
                STRINGS; both sides in one level dict)
  OI Data     - standalone {security_id, OI} - emitted as an OI-only tick;
                the engine's carry-forward handles the merge

Self-check: python -m nifty.dhan.provider  (uses real packet fixtures)
"""

from __future__ import annotations

import csv
import threading
import urllib.request
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from nifty.paths import DATA_DIR

MASTER_CSV = DATA_DIR / "dhan_scrip_master.csv"
MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"

NIFTY_INDEX_SID = "13"        # Dhan well-known id for NIFTY 50
IDX_FEED_SEGMENT = 0          # IDX_I
NSE_FNO_FEED_SEGMENT = 2      # NSE_FNO


def _f(value: Any) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _i(value: Any) -> Optional[int]:
    try:
        return int(float(value)) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def resolve_security_ids(instruments: Iterable[Any]) -> Dict[str, int]:
    """Map Dhan security_id -> Kite token for the tracked NIFTY options.

    Matches on (expiry ISO, strike, CE/PE) against the detailed scrip
    master (downloaded once, cached on disk). Contracts that fail to map
    are skipped with a log line - failover covers whatever maps.
    """
    if not MASTER_CSV.exists() or MASTER_CSV.stat().st_size == 0:
        print("[dhan] downloading scrip master...")
        urllib.request.urlretrieve(MASTER_URL, MASTER_CSV)

    wanted: Dict[Tuple[str, int, str], int] = {}
    for item in instruments:
        if str(getattr(item, "option_type", "")) in {"CE", "PE"}:
            wanted[(str(item.expiry)[:10], int(item.strike), str(item.option_type))] = int(item.token)

    mapping: Dict[str, int] = {}
    with MASTER_CSV.open(newline="", encoding="utf-8", errors="replace") as handle:
        for row in csv.DictReader(handle):
            if (row.get("UNDERLYING_SYMBOL") or "").strip().upper() != "NIFTY":
                continue
            if (row.get("EXCH_ID") or "").strip().upper() != "NSE":
                continue
            opt_type = (row.get("OPTION_TYPE") or "").strip().upper()
            if opt_type not in {"CE", "PE"}:
                continue
            strike = _i(row.get("STRIKE_PRICE"))
            expiry = (row.get("SM_EXPIRY_DATE") or "").strip()[:10]
            sid = (row.get("SECURITY_ID") or "").strip()
            if strike is None or not sid:
                continue
            key = (expiry, strike, opt_type)
            if key in wanted:
                mapping[sid] = wanted.pop(key)
            if not wanted:
                break
    for (expiry, strike, opt_type) in wanted:
        print(f"[dhan] no scrip-master match for NIFTY {expiry} {strike}{opt_type} - skipped")
    return mapping


def normalize_packet(
    packet: Dict[str, Any],
    sid_to_token: Dict[str, int],
    spot_token: int,
) -> Optional[Dict[str, Any]]:
    """One Dhan packet -> one Kite-shaped tick dict (or None if untracked).

    Absent fields are OMITTED, not zeroed - the engine's per-field
    carry-forward then behaves identically to a partial Kite tick.
    """
    sid = str(packet.get("security_id") or packet.get("securityId") or "")
    segment = packet.get("exchange_segment")
    if sid == NIFTY_INDEX_SID and segment in (IDX_FEED_SEGMENT, str(IDX_FEED_SEGMENT)):
        ltp = _f(packet.get("LTP") or packet.get("last_price"))
        if ltp is None:
            return None
        tick: Dict[str, Any] = {"instrument_token": spot_token, "last_price": ltp}
        ohlc = {
            "open": _f(packet.get("open")),
            "high": _f(packet.get("high")),
            "low": _f(packet.get("low")),
            "close": _f(packet.get("close")),   # Dhan close == prev close, as Kite
        }
        if any(v is not None for v in ohlc.values()):
            tick["ohlc"] = {k: v for k, v in ohlc.items() if v is not None}
        return tick

    token = sid_to_token.get(sid)
    if token is None:
        return None

    tick = {"instrument_token": token}
    ltp = _f(packet.get("LTP") or packet.get("last_price"))
    if ltp is not None:
        tick["last_price"] = ltp
    oi = _i(packet.get("OI") or packet.get("open_interest"))
    if oi is not None:
        tick["oi"] = oi
    volume = _i(packet.get("volume") or packet.get("volume_traded"))
    if volume is not None:
        tick["volume_traded"] = volume
    ltq = _i(packet.get("LTQ"))
    if ltq is not None:
        tick["last_traded_quantity"] = ltq
    tbq = _i(packet.get("total_buy_quantity"))
    if tbq is not None:
        tick["total_buy_quantity"] = tbq
    tsq = _i(packet.get("total_sell_quantity"))
    if tsq is not None:
        tick["total_sell_quantity"] = tsq

    depth = packet.get("depth")
    if isinstance(depth, list) and depth:
        buy, sell = [], []
        for level in depth[:5]:
            buy.append({
                "price": _f(level.get("bid_price")) or 0.0,
                "quantity": _i(level.get("bid_quantity")) or 0,
                "orders": _i(level.get("bid_orders")) or 0,
            })
            sell.append({
                "price": _f(level.get("ask_price")) or 0.0,
                "quantity": _i(level.get("ask_quantity")) or 0,
                "orders": _i(level.get("ask_orders")) or 0,
            })
        tick["depth"] = {"buy": buy, "sell": sell}

    # A packet with a token but nothing else useful (e.g. unparsable) -> drop.
    return tick if len(tick) > 1 else None


class DhanFeed(threading.Thread):
    """Standby websocket feed: normalizes packets and hands Kite-shaped tick
    batches to the router sink. Reconnects with backoff; 429s wait longer."""

    def __init__(
        self,
        client_id: str,
        access_token: str,
        sid_to_token: Dict[str, int],
        spot_token: int,
        on_ticks: Callable[[List[Dict[str, Any]]], None],
    ) -> None:
        super().__init__(daemon=True, name="dhan-feed")
        self.client_id = client_id
        self.access_token = access_token
        self.sid_to_token = sid_to_token
        self.spot_token = spot_token
        self.on_ticks = on_ticks
        self.stop_event = threading.Event()

    def _subscriptions(self) -> List[Tuple[int, str, Any]]:
        from dhanhq import MarketFeed

        subs: List[Tuple[int, str, Any]] = [
            (IDX_FEED_SEGMENT, NIFTY_INDEX_SID, MarketFeed.Quote)
        ]
        subs.extend(
            (NSE_FNO_FEED_SEGMENT, sid, MarketFeed.Full) for sid in self.sid_to_token
        )
        return subs

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:  # pragma: no cover - network loop; logic in normalize_packet
        import asyncio

        from dhanhq import DhanContext, MarketFeed

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        context = DhanContext(self.client_id, self.access_token)
        feed = MarketFeed(context, self._subscriptions(), version="v2")

        async def pump() -> None:
            backoff = 5
            while not self.stop_event.is_set():
                try:
                    await feed.connect()
                    backoff = 5
                    print("[dhan] feed connected (standby)")
                    while not self.stop_event.is_set():
                        message = await feed.get_instrument_data()
                        packets = message if isinstance(message, list) else [message]
                        ticks = []
                        for packet in packets:
                            if isinstance(packet, dict):
                                tick = normalize_packet(packet, self.sid_to_token, self.spot_token)
                                if tick is not None:
                                    ticks.append(tick)
                        if ticks:
                            self.on_ticks(ticks)
                except Exception as error:
                    rate_limited = any(
                        text in repr(error).lower() for text in ("429", "too many", "blocked")
                    )
                    wait = 120 if rate_limited else backoff
                    if not rate_limited:
                        backoff = min(backoff * 2, 60)
                    print(f"[dhan] feed disconnected: {error!r}; retry in {wait}s")
                    await asyncio.sleep(wait)

        try:
            loop.run_until_complete(pump())
        finally:
            loop.close()


def _selftest() -> None:
    sid_map = {"50549": 111222}
    spot = 256265

    # Real archived shapes (TRADING/DHAN ticks.db).
    full = {
        "type": "Full Data", "exchange_segment": 2, "security_id": "50549",
        "LTP": "687.60", "LTQ": 65, "LTT": "15:29:59", "avg_price": "438.85",
        "volume": 10653825, "total_sell_quantity": 14170, "total_buy_quantity": 112710,
        "OI": 1002365, "oi_day_high": 1534065, "oi_day_low": 1002365,
        "open": "411.05", "close": "253.00", "high": "697.30", "low": "343.10",
        "depth": [{"bid_quantity": 130, "ask_quantity": 520, "bid_orders": 1,
                   "ask_orders": 1, "bid_price": "682.70", "ask_price": "687.35"}],
    }
    tick = normalize_packet(full, sid_map, spot)
    assert tick["instrument_token"] == 111222
    assert tick["last_price"] == 687.60 and tick["oi"] == 1002365
    assert tick["depth"]["buy"][0]["price"] == 682.70          # str -> float
    assert tick["depth"]["sell"][0]["quantity"] == 520          # combined -> split

    oi_only = {"type": "OI Data", "exchange_segment": 2, "security_id": "50549", "OI": 999}
    tick = normalize_packet(oi_only, sid_map, spot)
    assert tick == {"instrument_token": 111222, "oi": 999}      # partial tick, engine carries rest

    quote = {"type": "Quote Data", "exchange_segment": 0, "security_id": "13",
             "LTP": "23085.40", "open": "23011.20", "close": "23041.80",
             "high": "23112.00", "low": "22987.60", "volume": 0}
    tick = normalize_packet(quote, sid_map, spot)
    assert tick["instrument_token"] == spot and tick["ohlc"]["close"] == 23041.80

    assert normalize_packet({"security_id": "999", "exchange_segment": 2, "LTP": "1"}, sid_map, spot) is None
    print("[dhan.provider] selftest OK: full/oi-only/spot normalization, untracked dropped")


if __name__ == "__main__":
    _selftest()
