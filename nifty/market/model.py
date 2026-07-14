"""Canonical, provider-neutral market model (Migration Phase 4).

Every provider (Kite today; Dhan/replay/CSV next) normalizes into these
shapes at its edge; the engine consumes only these. The single most
load-bearing rule: **None means the provider did not supply the field** -
never zero, never carry-forward. Dhan delivers OI on only 43% of packets
(measured, API_AND_CALCULATIONS.md §3.2) while Kite sends it on every
option tick; without explicit None the two are indistinguishable and
velocity spikes appear out of thin air.

Self-check: python -m nifty.market.model
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, Literal, Optional


@dataclass(frozen=True, slots=True)
class InstrumentRef:
    """Semantic instrument identity - survives any provider's ID scheme.

    Kite keys by int token, Dhan by (security_id, exchange_segment); both
    map to one of these. Hashable: usable as a dict key by the engine."""

    exchange: str                                # "NSE" | "NFO" | "BSE" | ...
    underlying: str                              # "NIFTY", "BANKNIFTY", ...
    kind: Literal["SPOT", "OPT", "FUT"]
    expiry: Optional[date] = None                # OPT/FUT only
    strike: Optional[int] = None                 # OPT only
    right: Optional[Literal["CE", "PE"]] = None  # OPT only

    def __str__(self) -> str:
        if self.kind == "OPT":
            return f"{self.underlying} {self.expiry} {self.strike}{self.right}"
        if self.kind == "FUT":
            return f"{self.underlying} FUT {self.expiry}"
        return f"{self.underlying} SPOT"


@dataclass(frozen=True, slots=True)
class OHLC:
    open: float
    high: float
    low: float
    prev_close: float


@dataclass(frozen=True, slots=True)
class MarketTick:
    """One canonical observation. None = not provided by this provider."""

    ts_ingest: datetime                 # our clock, always present
    ref: InstrumentRef
    ltp: float
    ts_exchange: Optional[datetime] = None
    oi: Optional[int] = None
    volume: Optional[int] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    ohlc: Optional[OHLC] = None


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What a provider's stream actually carries - drives grader
    renormalization (a dimension whose input the provider cannot supply
    must score UNAVAILABLE, not fail; lands with the Dhan adapter)."""

    has_oi_stream: bool
    has_volume: bool
    depth_levels: int
    has_exchange_ts: bool
    has_greeks_api: bool
    has_chain_api: bool
    headless_auth: bool


KITE_CAPABILITIES = Capabilities(
    has_oi_stream=True,      # OI on every option tick
    has_volume=True,
    depth_levels=5,
    has_exchange_ts=True,    # sent, though the engine historically ignored it
    has_greeks_api=False,    # the desk computes its own (analytics/options.py)
    has_chain_api=False,
    headless_auth=False,     # daily interactive 2FA
)


def from_kite_tick(
    tick: Dict[str, Any],
    ref: InstrumentRef,
    ts_ingest: datetime,
) -> MarketTick:
    """Normalize a KiteTicker MODE_FULL dict. Absent keys become None."""
    depth = tick.get("depth") or {}
    buy = depth.get("buy") or []
    sell = depth.get("sell") or []
    ohlc_raw = tick.get("ohlc") or {}
    ohlc = None
    if ohlc_raw:
        ohlc = OHLC(
            open=float(ohlc_raw.get("open") or 0.0),
            high=float(ohlc_raw.get("high") or 0.0),
            low=float(ohlc_raw.get("low") or 0.0),
            prev_close=float(ohlc_raw.get("close") or 0.0),
        )
    return MarketTick(
        ts_ingest=ts_ingest,
        ts_exchange=tick.get("exchange_timestamp"),
        ref=ref,
        ltp=float(tick.get("last_price") or 0.0),
        oi=int(tick["oi"]) if tick.get("oi") is not None else None,
        volume=int(tick["volume_traded"]) if tick.get("volume_traded") is not None else None,
        bid=float(buy[0]["price"]) if buy else None,
        ask=float(sell[0]["price"]) if sell else None,
        ohlc=ohlc,
    )


def _selftest() -> None:
    ref = InstrumentRef("NFO", "NIFTY", "OPT", date(2026, 6, 16), 23000, "CE")
    now = datetime(2026, 6, 19, 10, 0, 0)

    full = from_kite_tick(
        {
            "instrument_token": 12345,
            "last_price": 687.6,
            "oi": 1002365,
            "volume_traded": 10653825,
            "depth": {"buy": [{"price": 687.0, "quantity": 65, "orders": 1}],
                      "sell": [{"price": 688.0, "quantity": 130, "orders": 2}]},
        },
        ref, now,
    )
    assert full.oi == 1002365 and full.bid == 687.0 and full.ask == 688.0

    # The load-bearing rule: a tick WITHOUT oi yields None, never 0.
    bare = from_kite_tick({"last_price": 100.0}, ref, now)
    assert bare.oi is None and bare.volume is None and bare.bid is None
    assert bare.ltp == 100.0

    spot_ref = InstrumentRef("NSE", "NIFTY", "SPOT")
    spot = from_kite_tick(
        {"last_price": 23085.4, "ohlc": {"open": 23011.2, "high": 23112.0,
                                          "low": 22987.6, "close": 23041.8}},
        spot_ref, now,
    )
    assert spot.ohlc is not None and spot.ohlc.prev_close == 23041.8
    assert {ref: 1, spot_ref: 2}[spot_ref] == 2  # hashable / dict-keyable
    print("[market.model] selftest OK: nullable semantics, depth top, ohlc, hashability")


if __name__ == "__main__":
    _selftest()
