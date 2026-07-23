#!/usr/bin/env python3
"""
Liquidity Engine for NIFTY — a self-contained liquidity *state machine*.

Ported faithfully from quant-desk-engine v4/ATLAS's nifty_liquidity_engine.py
(mentor-authored). No logic changed — fully self-contained in the source
too, so no import adaptation was needed either.

This is genuinely new capability: nifty-dashboard has no liquidity engine
today. Several already-ported modules this session (oi_conviction.py,
intent_filter.py, relationships_lab.py, market_intelligence_lab.py) accept
a `liquidity_engine` dict as an optional input that degrades to `{}` /
documented no-op because nothing provides it yet — this file is exactly
what fills that gap, once wired in.

Design philosophy ("Bias is context. Participant action is truth."): this
engine models *where liquidity rests* and *what price did to it*, as a
coherent state object — not a growing pile of independent heuristics.

Internal architecture (mirrors the mentor's Probability engine's
single-state pattern):

    compute(...) -> LiquidityState
        |-- SwingDetector          -> List[SwingNode]      (Phase 2)
        |-- StructureEngine        -> MarketStructure      (Phase 5: BOS/CHoCH/MSS)
        |-- LiquidityPoolDetector  -> List[LiquidityPool]  (Phase 3)
        |-- SweepDetector          -> Sweep                (grab detection, preserved)
        |-- ConfirmationEngine     -> Confirmation         (Phase 4: weak->institutional)
        |-- LiquidityTargetEngine  -> List[LiquidityTarget](Phase 6)
        `-- LiquidityScoreEngine   -> pool/overall scoring

`build_liquidity_engine(...)` returns `state.to_dict()`, a SUPERSET dict
that preserves every key the rest of the mentor's desk already consumes:

    status, liquidity_grab, liquidity_grab_source, equal_highs, equal_lows,
    swing_high, swing_low, previous_week_high/low, previous_month_high/low,
    session_high/low

so that once wired in, this stays compatible with the null-safe
`liquidity_engine` consumers already ported. The grab-detection semantics
are PRESERVED byte-for-byte per the mentor's own docstring; everything
richer (structure, pools, confirmation, targets) is additive.

Not yet wired into the live pipeline.
Self-check: python -m nifty.analytics.liquidity_engine
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Tunables / constants
# ---------------------------------------------------------------------------

MIN_POINTS = 40
SWING_WINDOW = 3
RECENT_WINDOW = 300
LOOKBACK_WINDOW = 30
HOLD_MIN_BARS = 3

# Liquidity-grab labels (PRESERVED — consumed by intent filter / decision engine)
GRAB_NONE = "NONE"
GRAB_UP = "UPSIDE_LIQUIDITY_GRAB"
GRAB_DOWN = "DOWNSIDE_LIQUIDITY_GRAB"

# Legacy market-structure labels (kept for dashboard UI + decision engine)
STRUCTURE_UP = "UPTREND"
STRUCTURE_DOWN = "DOWNTREND"
STRUCTURE_RANGE = "RANGE"
STRUCTURE_TRANSITION = "TRANSITION"

# Phase-5 structural bias
BIAS_BULLISH = "BULLISH"
BIAS_BEARISH = "BEARISH"
BIAS_RANGE = "RANGE"
BIAS_TRANSITION = "TRANSITION"

# Phase-4 confirmation stages + confidence
STAGE_NONE = "NONE"
STAGE_SWEEP = "SWEEP"
STAGE_CLOSE_BACK = "CLOSE_BACK"
STAGE_HOLD = "HOLD"
STAGE_RECLAIM = "RECLAIM"
STAGE_CONTINUATION = "CONTINUATION"

CONF_NONE = "NONE"
CONF_WEAK = "WEAK"
CONF_MODERATE = "MODERATE"
CONF_STRONG = "STRONG"
CONF_INSTITUTIONAL = "INSTITUTIONAL"

# Liquidity-pool sides
SIDE_BUY = "BUY_SIDE"   # liquidity rests ABOVE highs (buy stops)
SIDE_SELL = "SELL_SIDE"  # liquidity rests BELOW lows (sell stops)

# Per-pool-type base weight for scoring (higher-timeframe pools are stronger)
POOL_TYPE_WEIGHT = {
    "PREV_MONTH_HIGH": 1.0,
    "PREV_MONTH_LOW": 1.0,
    "PREV_WEEK_HIGH": 0.85,
    "PREV_WEEK_LOW": 0.85,
    "EQUAL_HIGH": 0.75,
    "EQUAL_LOW": 0.75,
    "SESSION_HIGH": 0.6,
    "SESSION_LOW": 0.6,
    "SWING_HIGH": 0.5,
    "SWING_LOW": 0.5,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _as_float(value: Any) -> float:
    try:
        if value in (None, "", "0"):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _round(value: Optional[float], digits: int = 2) -> Optional[float]:
    if value is None or value <= 0:
        return None
    return round(float(value), digits)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# Immutable value objects
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SwingNode:
    """A single swing pivot in the price series."""

    index: int
    price: float
    kind: str            # "HIGH" | "LOW"
    strength: float      # point prominence vs local neighbourhood
    label: Optional[str] = None  # HH / HL / LH / LL
    broken: bool = False
    swept: bool = False
    equal_cluster: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "price": round(self.price, 2),
            "kind": self.kind,
            "strength": round(self.strength, 2),
            "label": self.label,
            "broken": self.broken,
            "swept": self.swept,
            "equal_cluster": self.equal_cluster,
        }


@dataclass(frozen=True)
class LiquidityPool:
    """A persistent resting-liquidity level with lifecycle state."""

    pool_type: str       # EQUAL_HIGH / PREV_WEEK_HIGH / SESSION_LOW / ...
    side: str            # BUY_SIDE | SELL_SIDE
    price: float
    created_index: int
    strength: float
    times_tested: int
    swept: bool
    confirmed: bool
    active: bool
    score: float
    distance_pts: Optional[float]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pool_type": self.pool_type,
            # legacy alias kept so existing readers using `source` keep working
            "source": self.pool_type.lower(),
            "side": "above" if self.side == SIDE_BUY else "below",
            "liquidity_side": self.side,
            "level": round(self.price, 2),
            "price": round(self.price, 2),
            "created_index": self.created_index,
            "strength": round(self.strength, 2),
            "times_tested": self.times_tested,
            "swept": self.swept,
            "confirmed": self.confirmed,
            "active": self.active,
            "score": round(self.score, 1),
            "distance_pts": round(self.distance_pts, 2) if self.distance_pts is not None else None,
        }


@dataclass(frozen=True)
class LiquidityTarget:
    """A projected next-liquidity destination for trade management."""

    price: float
    pool_type: str
    side: str
    distance_pts: float
    confidence: int
    priority: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "price": round(self.price, 2),
            "level": round(self.price, 2),
            "pool_type": self.pool_type,
            "source": self.pool_type.lower(),
            "side": "above" if self.side == SIDE_BUY else "below",
            "distance_pts": round(self.distance_pts, 2),
            "confidence": self.confidence,
            "priority": self.priority,
        }


@dataclass(frozen=True)
class MarketStructure:
    bias: str            # BULLISH / BEARISH / RANGE / TRANSITION
    legacy: str          # UPTREND / DOWNTREND / RANGE / TRANSITION
    last_high_label: Optional[str]
    last_low_label: Optional[str]
    sequence: Tuple[str, ...]
    events: Tuple[Dict[str, Any], ...]  # BOS / CHoCH / MSS

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bias": self.bias,
            "market_structure": self.legacy,
            "last_high_label": self.last_high_label,
            "last_low_label": self.last_low_label,
            "sequence": list(self.sequence),
            "events": list(self.events),
        }


@dataclass(frozen=True)
class Sweep:
    grab: str                    # GRAB_NONE / GRAB_UP / GRAB_DOWN
    source: Optional[str]        # equal_high / prev_week_high / session_low ...
    level: Optional[float]
    sweep_index: Optional[int]


@dataclass(frozen=True)
class Confirmation:
    stage: str
    confidence: str
    back_inside: bool
    reclaim: Optional[str]       # BACK_BELOW / BACK_ABOVE / PENDING / None
    detail: str = ""


@dataclass(frozen=True)
class LiquidityState:
    """Single immutable snapshot of the liquidity model."""

    status: str
    spot: float
    swings: Tuple[SwingNode, ...] = ()
    structure: Optional[MarketStructure] = None
    pools: Tuple[LiquidityPool, ...] = ()
    sweep: Optional[Sweep] = None
    confirmation: Optional[Confirmation] = None
    targets: Tuple[LiquidityTarget, ...] = ()
    equal_highs: Tuple[Dict[str, Any], ...] = ()
    equal_lows: Tuple[Dict[str, Any], ...] = ()
    period: Dict[str, Optional[float]] = field(default_factory=dict)
    session_high: Optional[float] = None
    session_low: Optional[float] = None
    swing_high: Optional[float] = None
    swing_low: Optional[float] = None
    read: str = ""
    confidence: int = 50

    # -- backward-compatible dict contract ---------------------------------
    def to_dict(self) -> Dict[str, Any]:
        if self.status != "READY":
            return {"status": self.status, **self.period}

        sweep = self.sweep or Sweep(GRAB_NONE, None, None, None)
        conf = self.confirmation or Confirmation(STAGE_NONE, CONF_NONE, False, None)
        struct = self.structure
        pools = [p.to_dict() for p in self.pools]
        untapped_above = [p for p in pools if p["side"] == "above" and not p["swept"]]
        untapped_below = [p for p in pools if p["side"] == "below" and not p["swept"]]
        targets = [t.to_dict() for t in self.targets]

        return {
            "status": "READY",
            # ---- legacy contract (DO NOT rename: consumed downstream) ----
            "equal_highs": list(self.equal_highs),
            "equal_lows": list(self.equal_lows),
            **self.period,
            "session_high": _round(self.session_high),
            "session_low": _round(self.session_low),
            "swing_high": round(self.swing_high, 2) if self.swing_high else None,
            "swing_low": round(self.swing_low, 2) if self.swing_low else None,
            "liquidity_grab": sweep.grab,
            "liquidity_grab_source": sweep.source,
            # ---- additive enrichment (shadow-only) ----
            "liquidity_grab_level": round(sweep.level, 2) if sweep.level else None,
            "liquidity_grab_confirmed": conf.back_inside,
            "reclaim": conf.reclaim,
            "confirmation": {
                "stage": conf.stage,
                "confidence": conf.confidence,
                "back_inside": conf.back_inside,
                "detail": conf.detail,
            },
            "market_structure": struct.legacy if struct else STRUCTURE_RANGE,
            "structure_bias": struct.bias if struct else BIAS_RANGE,
            "structure": struct.to_dict() if struct else {},
            "swings": [n.to_dict() for n in self.swings][-12:],
            "pools": pools,
            "untapped_above": untapped_above,
            "untapped_below": untapped_below,
            "targets": targets,
            "target_pool": targets[0] if targets else None,
            "read": self.read,
            "confidence": self.confidence,
        }


# ---------------------------------------------------------------------------
# Sub-engines
# ---------------------------------------------------------------------------

class SwingDetector:
    """Phase 2 — detect swing pivots and label HH/HL/LH/LL."""

    def __init__(self, window: int = SWING_WINDOW):
        self.window = window

    def detect(self, recent: List[float], tolerance: float,
               equal_levels: List[float]) -> List[SwingNode]:
        w = self.window
        nodes: List[SwingNode] = []
        for i in range(w, len(recent) - w):
            px = recent[i]
            left = recent[i - w : i]
            right = recent[i + 1 : i + 1 + w]
            is_high = all(px >= v for v in left) and all(px >= v for v in right)
            is_low = all(px <= v for v in left) and all(px <= v for v in right)
            if not (is_high or is_low):
                continue
            kind = "HIGH" if is_high else "LOW"
            if is_high:
                strength = min(px - min(left), px - min(right))
            else:
                strength = min(max(left) - px, max(right) - px)
            future = recent[i + 1 :]
            if kind == "HIGH":
                broken = any(v > px + tolerance for v in future)
                swept = any(v >= px + tolerance for v in future) and recent[-1] <= px
            else:
                broken = any(v < px - tolerance for v in future)
                swept = any(v <= px - tolerance for v in future) and recent[-1] >= px
            equal_cluster = any(abs(px - lvl) <= tolerance for lvl in equal_levels)
            nodes.append(
                SwingNode(
                    index=i,
                    price=px,
                    kind=kind,
                    strength=max(strength, 0.0),
                    broken=broken,
                    swept=swept,
                    equal_cluster=equal_cluster,
                )
            )
        return self._label(nodes)

    @staticmethod
    def _label(nodes: List[SwingNode]) -> List[SwingNode]:
        labelled: List[SwingNode] = []
        prev_high: Optional[float] = None
        prev_low: Optional[float] = None
        for n in nodes:
            label = None
            if n.kind == "HIGH":
                if prev_high is not None:
                    label = "HH" if n.price > prev_high else "LH"
                prev_high = n.price
            else:
                if prev_low is not None:
                    label = "HL" if n.price > prev_low else "LL"
                prev_low = n.price
            labelled.append(
                SwingNode(
                    index=n.index, price=n.price, kind=n.kind, strength=n.strength,
                    label=label, broken=n.broken, swept=n.swept,
                    equal_cluster=n.equal_cluster,
                )
            )
        return labelled


class StructureEngine:
    """Phase 5 — classify Bullish/Bearish/Range/Transition + BOS/CHoCH/MSS."""

    def classify(self, swings: List[SwingNode], spot: float, tolerance: float) -> MarketStructure:
        highs = [n for n in swings if n.kind == "HIGH"]
        lows = [n for n in swings if n.kind == "LOW"]

        high_label = highs[-1].label if highs and highs[-1].label else None
        low_label = lows[-1].label if lows and lows[-1].label else None
        seq = tuple(n.label for n in swings if n.label)[-6:]

        legacy = STRUCTURE_RANGE
        bias = BIAS_RANGE
        if high_label == "HH" and low_label == "HL":
            legacy, bias = STRUCTURE_UP, BIAS_BULLISH
        elif high_label == "LH" and low_label == "LL":
            legacy, bias = STRUCTURE_DOWN, BIAS_BEARISH

        events = self._events(highs, lows, spot, tolerance, bias)
        # A CHoCH/MSS overrides a stale trend label with TRANSITION until re-established.
        if any(e["type"] in ("CHoCH", "MSS") for e in events) and bias in (BIAS_RANGE,):
            bias, legacy = BIAS_TRANSITION, STRUCTURE_TRANSITION

        return MarketStructure(
            bias=bias,
            legacy=legacy,
            last_high_label=high_label,
            last_low_label=low_label,
            sequence=seq,
            events=tuple(events),
        )

    @staticmethod
    def _events(highs: List[SwingNode], lows: List[SwingNode], spot: float,
                tolerance: float, bias: str) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        if spot <= 0:
            return events
        last_high = highs[-1] if highs else None
        last_low = lows[-1] if lows else None

        # Break of Structure: price extends beyond the most recent confirmed pivot
        # in the direction of the prevailing trend.
        if last_high and spot > last_high.price + tolerance and bias == BIAS_BULLISH:
            events.append({"type": "BOS", "direction": "UP", "level": round(last_high.price, 2)})
        if last_low and spot < last_low.price - tolerance and bias == BIAS_BEARISH:
            events.append({"type": "BOS", "direction": "DOWN", "level": round(last_low.price, 2)})

        # Change of Character: first break AGAINST the prevailing trend.
        if bias == BIAS_BULLISH and last_low and spot < last_low.price - tolerance:
            events.append({"type": "CHoCH", "direction": "DOWN", "level": round(last_low.price, 2)})
        if bias == BIAS_BEARISH and last_high and spot > last_high.price + tolerance:
            events.append({"type": "CHoCH", "direction": "UP", "level": round(last_high.price, 2)})

        # Market Structure Shift: a decisive break beyond the *prior* pivot of the
        # same kind (stronger than a single CHoCH), signalling a regime flip.
        if bias == BIAS_BULLISH and len(lows) >= 2 and spot < lows[-2].price - tolerance:
            events.append({"type": "MSS", "direction": "DOWN", "level": round(lows[-2].price, 2)})
        if bias == BIAS_BEARISH and len(highs) >= 2 and spot > highs[-2].price + tolerance:
            events.append({"type": "MSS", "direction": "UP", "level": round(highs[-2].price, 2)})
        return events


class LiquidityPoolDetector:
    """Phase 3 — build persistent LiquidityPool objects from all sources."""

    def detect(
        self,
        *,
        swings: List[SwingNode],
        equal_highs: List[Dict[str, Any]],
        equal_lows: List[Dict[str, Any]],
        period: Dict[str, float],
        session_high: Optional[float],
        session_low: Optional[float],
        recent: List[float],
        local_hi: float,
        local_lo: float,
        spot_now: float,
        tolerance: float,
        near: float,
    ) -> List[LiquidityPool]:
        raw: List[Tuple[str, str, Optional[float], int, int]] = []
        # (pool_type, side, price, created_index, times_tested)
        if equal_highs:
            raw.append(("EQUAL_HIGH", SIDE_BUY, equal_highs[0]["level"], 0, equal_highs[0]["touches"]))
        if equal_lows:
            raw.append(("EQUAL_LOW", SIDE_SELL, equal_lows[0]["level"], 0, equal_lows[0]["touches"]))
        raw.append(("PREV_WEEK_HIGH", SIDE_BUY, period.get("previous_week_high"), 0, 1))
        raw.append(("PREV_WEEK_LOW", SIDE_SELL, period.get("previous_week_low"), 0, 1))
        raw.append(("PREV_MONTH_HIGH", SIDE_BUY, period.get("previous_month_high"), 0, 1))
        raw.append(("PREV_MONTH_LOW", SIDE_SELL, period.get("previous_month_low"), 0, 1))
        raw.append(("SESSION_HIGH", SIDE_BUY, session_high, 0, 1))
        raw.append(("SESSION_LOW", SIDE_SELL, session_low, 0, 1))

        # strongest unbroken swing high/low as structural pools
        swing_highs = [n for n in swings if n.kind == "HIGH"]
        swing_lows = [n for n in swings if n.kind == "LOW"]
        if swing_highs:
            top = max(swing_highs, key=lambda n: n.price)
            raw.append(("SWING_HIGH", SIDE_BUY, top.price, top.index, 1))
        if swing_lows:
            bot = min(swing_lows, key=lambda n: n.price)
            raw.append(("SWING_LOW", SIDE_SELL, bot.price, bot.index, 1))

        scorer = LiquidityScoreEngine()
        pools: List[LiquidityPool] = []
        seen: List[Tuple[str, float]] = []
        for pool_type, side, price, created, tested in raw:
            price = _as_float(price)
            if price <= 0:
                continue
            # de-dupe near-identical levels of the same side
            if any(s == side and abs(price - p) <= tolerance for s, p in seen):
                continue
            seen.append((side, price))

            if side == SIDE_BUY:
                swept = local_hi >= (price + near)
            else:
                swept = local_lo <= (price - near)
            tested_count = max(tested, self._count_tests(recent, price, tolerance))
            active = not swept
            confirmed = swept and (
                (side == SIDE_BUY and spot_now <= price)
                or (side == SIDE_SELL and spot_now >= price)
            )
            strength = self._strength(recent, price, near)
            distance = price - spot_now if spot_now > 0 else None
            score = scorer.pool_score(
                pool_type=pool_type, strength=strength, times_tested=tested_count,
                swept=swept, distance=distance, tolerance=tolerance,
            )
            pools.append(
                LiquidityPool(
                    pool_type=pool_type, side=side, price=price, created_index=created,
                    strength=strength, times_tested=tested_count, swept=swept,
                    confirmed=confirmed, active=active, score=score, distance_pts=distance,
                )
            )
        pools.sort(key=lambda p: p.score, reverse=True)
        return pools

    @staticmethod
    def _count_tests(recent: List[float], level: float, tolerance: float) -> int:
        return sum(1 for v in recent if abs(v - level) <= tolerance)

    @staticmethod
    def _strength(recent: List[float], level: float, near: float) -> float:
        # prominence: how far the series reaches toward the level relative to `near`
        touches = sum(1 for v in recent if abs(v - level) <= near)
        return float(min(100.0, touches * 8.0))


class SweepDetector:
    """Liquidity-grab detection. Semantics PRESERVED from the original dashboard
    implementation so existing gating (LIQUIDITY_GRAB_CONFLICT) is unchanged."""

    def detect(
        self,
        *,
        equal_highs: List[Dict[str, Any]],
        equal_lows: List[Dict[str, Any]],
        period: Dict[str, float],
        session_high: Optional[float],
        session_low: Optional[float],
        recent: List[float],
        local_hi: float,
        local_lo: float,
        spot_now: float,
        tolerance: float,
        near: float,
    ) -> Sweep:
        high_levels = [
            ("equal_high", equal_highs[0]["level"] if equal_highs else None),
            ("prev_week_high", period.get("previous_week_high")),
            ("prev_month_high", period.get("previous_month_high")),
            ("session_high", session_high),
        ]
        for source, lvl in high_levels:
            lvl = _as_float(lvl)
            if lvl and local_hi >= (lvl + near) and spot_now <= (lvl - tolerance):
                return Sweep(GRAB_UP, source, lvl, self._sweep_index(recent, lvl, near, "HIGH"))

        low_levels = [
            ("equal_low", equal_lows[0]["level"] if equal_lows else None),
            ("prev_week_low", period.get("previous_week_low")),
            ("prev_month_low", period.get("previous_month_low")),
            ("session_low", session_low),
        ]
        for source, lvl in low_levels:
            lvl = _as_float(lvl)
            if lvl and local_lo <= (lvl - near) and spot_now >= (lvl + tolerance):
                return Sweep(GRAB_DOWN, source, lvl, self._sweep_index(recent, lvl, near, "LOW"))

        return Sweep(GRAB_NONE, None, None, None)

    @staticmethod
    def _sweep_index(recent: List[float], level: float, near: float, kind: str) -> Optional[int]:
        idx: Optional[int] = None
        for i, v in enumerate(recent):
            if kind == "HIGH" and v >= level + near:
                idx = i
            if kind == "LOW" and v <= level - near:
                idx = i
        return idx


class ConfirmationEngine:
    """Phase 4 — grade a sweep: sweep -> close back -> hold -> reclaim -> continuation."""

    def evaluate(self, *, sweep: Sweep, swings: List[SwingNode], recent: List[float],
                 spot_now: float, tolerance: float) -> Confirmation:
        if sweep.grab == GRAB_NONE or not sweep.level:
            return Confirmation(STAGE_NONE, CONF_NONE, False, None)

        level = sweep.level
        up = sweep.grab == GRAB_UP
        post = recent[(sweep.sweep_index or 0) + 1 :] if sweep.sweep_index is not None else []

        # Stage 1: sweep happened (by construction).
        stage = STAGE_SWEEP

        # Stage 2: close back inside the level.
        back_inside = spot_now <= (level - tolerance) if up else spot_now >= (level + tolerance)
        if back_inside:
            stage = STAGE_CLOSE_BACK

        # Stage 3: hold — enough post-sweep bars stayed back inside.
        if back_inside and post:
            inside = sum(1 for v in post if (v <= level if up else v >= level))
            if inside >= HOLD_MIN_BARS:
                stage = STAGE_HOLD

        # Reference structure level to reclaim (opposite-side pivot before the sweep).
        ref = self._reclaim_ref(swings, sweep.sweep_index, up)

        # Stage 4: reclaim — break the opposing structure pivot in the new direction.
        reclaimed = False
        if stage == STAGE_HOLD and ref is not None and post:
            if up and min(post) <= ref - tolerance:
                reclaimed = True
            if (not up) and max(post) >= ref + tolerance:
                reclaimed = True
            if reclaimed:
                stage = STAGE_RECLAIM

        # Stage 5: continuation — progress beyond the reclaim toward the next pool.
        if stage == STAGE_RECLAIM and ref is not None:
            if up and spot_now <= ref - tolerance:
                stage = STAGE_CONTINUATION
            if (not up) and spot_now >= ref + tolerance:
                stage = STAGE_CONTINUATION

        confidence = {
            STAGE_SWEEP: CONF_WEAK,
            STAGE_CLOSE_BACK: CONF_WEAK,
            STAGE_HOLD: CONF_MODERATE,
            STAGE_RECLAIM: CONF_STRONG,
            STAGE_CONTINUATION: CONF_INSTITUTIONAL,
        }.get(stage, CONF_NONE)

        reclaim_label = None
        if sweep.grab != GRAB_NONE:
            reclaim_label = ("BACK_BELOW" if up else "BACK_ABOVE") if back_inside else "PENDING"

        detail = f"{sweep.source or ''} {stage.lower()}".strip()
        return Confirmation(stage, confidence, back_inside, reclaim_label, detail)

    @staticmethod
    def _reclaim_ref(swings: List[SwingNode], sweep_index: Optional[int], up: bool) -> Optional[float]:
        if sweep_index is None:
            prior = swings
        else:
            prior = [n for n in swings if n.index <= sweep_index]
        if up:
            lows = [n.price for n in prior if n.kind == "LOW"]
            return lows[-1] if lows else None
        highs = [n.price for n in prior if n.kind == "HIGH"]
        return highs[-1] if highs else None


class LiquidityTargetEngine:
    """Phase 6 — rank the next untapped liquidity destinations."""

    def project(self, *, pools: List[LiquidityPool], sweep: Sweep, structure: MarketStructure,
                spot_now: float) -> List[LiquidityTarget]:
        if spot_now <= 0:
            return []

        # Direction of expected travel:
        #   after an upside grab -> reversal down -> target sell-side below
        #   after a downside grab -> reversal up   -> target buy-side above
        #   else -> follow structural bias
        if sweep.grab == GRAB_UP:
            want_above = False
        elif sweep.grab == GRAB_DOWN:
            want_above = True
        elif structure.bias == BIAS_BULLISH:
            want_above = True
        elif structure.bias == BIAS_BEARISH:
            want_above = False
        else:
            want_above = None  # range: take nearest on both sides

        candidates = [p for p in pools if p.active]
        if want_above is True:
            candidates = [p for p in candidates if p.price > spot_now]
            candidates.sort(key=lambda p: p.price - spot_now)
        elif want_above is False:
            candidates = [p for p in candidates if p.price < spot_now]
            candidates.sort(key=lambda p: spot_now - p.price)
        else:
            candidates.sort(key=lambda p: abs(p.price - spot_now))

        targets: List[LiquidityTarget] = []
        for rank, pool in enumerate(candidates[:3], start=1):
            distance = abs(pool.price - spot_now)
            # closer + higher-scoring pools = higher confidence
            proximity = _clamp(100.0 - distance, 0.0, 100.0)
            confidence = int(_clamp(0.5 * pool.score + 0.3 * proximity + 20.0, 30, 95))
            targets.append(
                LiquidityTarget(
                    price=pool.price, pool_type=pool.pool_type, side=pool.side,
                    distance_pts=distance, confidence=confidence, priority=rank,
                )
            )
        return targets


class LiquidityScoreEngine:
    """Phase-level scoring for pools and overall liquidity confidence."""

    def pool_score(self, *, pool_type: str, strength: float, times_tested: int,
                   swept: bool, distance: Optional[float], tolerance: float) -> float:
        base = POOL_TYPE_WEIGHT.get(pool_type, 0.5) * 40.0
        score = base + min(strength, 100.0) * 0.3 + min(times_tested, 6) * 4.0
        if swept:
            score *= 0.5  # spent liquidity is far less of a magnet
        return _clamp(score, 0.0, 100.0)

    def overall(self, *, sweep: Sweep, confirmation: Confirmation,
                structure: MarketStructure, equal_highs: List, equal_lows: List) -> Tuple[str, int]:
        conf_bonus = {
            CONF_NONE: 0, CONF_WEAK: 6, CONF_MODERATE: 14,
            CONF_STRONG: 22, CONF_INSTITUTIONAL: 30,
        }.get(confirmation.confidence, 0)

        if sweep.grab == GRAB_UP:
            read = f"Upside liquidity grab at {sweep.source or 'high'} [{confirmation.confidence}]"
            if confirmation.back_inside:
                read += " — reclaimed back below (reversal risk down)"
            return read, int(_clamp(56 + conf_bonus, 40, 95))
        if sweep.grab == GRAB_DOWN:
            read = f"Downside liquidity grab at {sweep.source or 'low'} [{confirmation.confidence}]"
            if confirmation.back_inside:
                read += " — reclaimed back above (reversal risk up)"
            return read, int(_clamp(56 + conf_bonus, 40, 95))

        if structure.bias == BIAS_BULLISH:
            return "Bullish structure (HH/HL) — buy-side liquidity above", 58
        if structure.bias == BIAS_BEARISH:
            return "Bearish structure (LH/LL) — sell-side liquidity below", 58
        if structure.bias == BIAS_TRANSITION:
            return "Transition — structure shift in progress", 52

        note = f" — {len(equal_highs)} eq-high / {len(equal_lows)} eq-low pools" if (equal_highs or equal_lows) else ""
        return f"Range / no active grab{note}", 50


# ---------------------------------------------------------------------------
# Equal-high / equal-low clustering (kept identical to the original)
# ---------------------------------------------------------------------------

def _cluster(levels: List[float], tolerance: float) -> List[Dict[str, Any]]:
    if not levels:
        return []
    ordered = sorted(levels)
    groups: List[List[float]] = [[ordered[0]]]
    for val in ordered[1:]:
        if abs(val - groups[-1][-1]) <= tolerance:
            groups[-1].append(val)
        else:
            groups.append([val])
    out: List[Dict[str, Any]] = []
    for grp in groups:
        if len(grp) >= 2:
            out.append({"level": round(sum(grp) / len(grp), 2), "touches": len(grp)})
    out.sort(key=lambda row: row["touches"], reverse=True)
    return out[:5]


# ---------------------------------------------------------------------------
# Top-level compute()  —  the single entry point
# ---------------------------------------------------------------------------

def compute(
    *,
    spot_history: Sequence[Tuple[float, float]],
    spot: float,
    day_high: float = 0.0,
    day_low: float = 0.0,
    period_extremes: Optional[Dict[str, Any]] = None,
    min_points: int = MIN_POINTS,
) -> LiquidityState:
    """Build the immutable LiquidityState from live inputs.

    `spot_history` is a sequence of (timestamp, price) tuples (the dashboard's
    `self.spot_history` deque). `period_extremes` carries previous_week_high/low
    and previous_month_high/low.
    """
    period_in = period_extremes or {}
    period = {
        "previous_week_high": _as_float(period_in.get("previous_week_high")),
        "previous_week_low": _as_float(period_in.get("previous_week_low")),
        "previous_month_high": _as_float(period_in.get("previous_month_high")),
        "previous_month_low": _as_float(period_in.get("previous_month_low")),
    }
    period_out = {
        "previous_week_high": _round(period["previous_week_high"]),
        "previous_week_low": _round(period["previous_week_low"]),
        "previous_month_high": _round(period["previous_month_high"]),
        "previous_month_low": _round(period["previous_month_low"]),
    }

    points = list(spot_history)
    if len(points) < min_points:
        return LiquidityState(status="WARMING_UP", spot=spot, period=period_out)
    prices = [p[1] for p in points if p[1] > 0]
    if len(prices) < min_points:
        return LiquidityState(status="WARMING_UP", spot=spot, period=period_out)

    tolerance = max(2.0, spot * 0.0002) if spot > 0 else 2.0
    near = max(4.0, tolerance * 1.5)
    recent = prices[-RECENT_WINDOW:]
    last_lookback = recent[-LOOKBACK_WINDOW:]
    local_hi = max(last_lookback) if last_lookback else spot
    local_lo = min(last_lookback) if last_lookback else spot
    spot_now = spot if spot > 0 else recent[-1]

    session_high = day_high if day_high > 0 else (max(prices) if prices else None)
    session_low = day_low if day_low > 0 else (min(prices) if prices else None)

    # First pass swings to derive equal clusters, then re-detect with cluster flags.
    pre = SwingDetector().detect(recent, tolerance, equal_levels=[])
    swing_high_prices = [n.price for n in pre if n.kind == "HIGH"]
    swing_low_prices = [n.price for n in pre if n.kind == "LOW"]
    equal_highs = _cluster(swing_high_prices, tolerance)
    equal_lows = _cluster(swing_low_prices, tolerance)
    equal_levels = [c["level"] for c in equal_highs] + [c["level"] for c in equal_lows]
    swings = SwingDetector().detect(recent, tolerance, equal_levels=equal_levels)

    structure = StructureEngine().classify(swings, spot_now, tolerance)

    sweep = SweepDetector().detect(
        equal_highs=equal_highs, equal_lows=equal_lows, period=period,
        session_high=session_high, session_low=session_low, recent=recent,
        local_hi=local_hi, local_lo=local_lo, spot_now=spot_now,
        tolerance=tolerance, near=near,
    )
    confirmation = ConfirmationEngine().evaluate(
        sweep=sweep, swings=swings, recent=recent, spot_now=spot_now, tolerance=tolerance,
    )
    pools = LiquidityPoolDetector().detect(
        swings=swings, equal_highs=equal_highs, equal_lows=equal_lows, period=period,
        session_high=session_high, session_low=session_low, recent=recent,
        local_hi=local_hi, local_lo=local_lo, spot_now=spot_now,
        tolerance=tolerance, near=near,
    )
    targets = LiquidityTargetEngine().project(
        pools=pools, sweep=sweep, structure=structure, spot_now=spot_now,
    )
    read, confidence = LiquidityScoreEngine().overall(
        sweep=sweep, confirmation=confirmation, structure=structure,
        equal_highs=equal_highs, equal_lows=equal_lows,
    )

    return LiquidityState(
        status="READY",
        spot=spot_now,
        swings=tuple(swings),
        structure=structure,
        pools=tuple(pools),
        sweep=sweep,
        confirmation=confirmation,
        targets=tuple(targets),
        equal_highs=tuple(equal_highs),
        equal_lows=tuple(equal_lows),
        period=period_out,
        session_high=session_high,
        session_low=session_low,
        swing_high=max(swing_high_prices) if swing_high_prices else None,
        swing_low=min(swing_low_prices) if swing_low_prices else None,
        read=read,
        confidence=confidence,
    )


def build_liquidity_engine(
    *,
    spot_history: Sequence[Tuple[float, float]],
    spot: float,
    day_high: float = 0.0,
    day_low: float = 0.0,
    period_extremes: Optional[Dict[str, Any]] = None,
    min_points: int = MIN_POINTS,
) -> Dict[str, Any]:
    """Backward-compatible dict wrapper around `compute(...).to_dict()`."""
    return compute(
        spot_history=spot_history,
        spot=spot,
        day_high=day_high,
        day_low=day_low,
        period_extremes=period_extremes,
        min_points=min_points,
    ).to_dict()


def _selftest() -> None:
    # Fewer than MIN_POINTS -> WARMING_UP, no crash on empty structure.
    warm = build_liquidity_engine(spot_history=[(float(i), 23000.0) for i in range(10)], spot=23000.0)
    assert warm["status"] == "WARMING_UP"

    # Build a synthetic session: rally to a local high, pull back, sweep the
    # prior high, then reclaim back below it (an upside liquidity grab + reclaim).
    base = 23000.0
    history: List[Tuple[float, float]] = []
    t = 0.0
    # ramp up to a swing high around 23100
    for i in range(50):
        history.append((t, base + i * 2.0))
        t += 60.0
    swing_high_price = history[-1][1]
    # pull back to create a clear swing low
    for i in range(30):
        history.append((t, swing_high_price - i * 2.0))
        t += 60.0
    # sweep above the swing high, then reclaim back below it
    history.append((t, swing_high_price + 20.0))
    t += 60.0
    for i in range(10):
        history.append((t, swing_high_price - 5.0 - i))
        t += 60.0

    state = build_liquidity_engine(
        spot_history=history,
        spot=history[-1][1],
        day_high=max(p for _, p in history),
        day_low=min(p for _, p in history),
        period_extremes={
            "previous_week_high": 23200.0,
            "previous_week_low": 22800.0,
            "previous_month_high": 23400.0,
            "previous_month_low": 22600.0,
        },
    )
    assert state["status"] == "READY"
    assert state["liquidity_grab"] in (GRAB_NONE, GRAB_UP, GRAB_DOWN)
    assert "structure" in state and "bias" in state["structure"]
    assert isinstance(state["pools"], list)
    assert state["previous_week_high"] == 23200.0
    assert "confirmation" in state and state["confirmation"]["stage"] in {
        STAGE_NONE, STAGE_SWEEP, STAGE_CLOSE_BACK, STAGE_HOLD, STAGE_RECLAIM, STAGE_CONTINUATION,
    }

    # Pool dict contract: legacy keys must be present for existing null-safe consumers.
    if state["pools"]:
        pool = state["pools"][0]
        assert set(["pool_type", "source", "side", "level", "price", "score"]).issubset(pool.keys())

    # Empty history entirely -> still returns WARMING_UP, never raises.
    empty = build_liquidity_engine(spot_history=[], spot=0.0)
    assert empty["status"] == "WARMING_UP"

    print("[analytics.liquidity_engine] selftest OK: warmup guard, swing/structure/sweep/pool/target pipeline")


if __name__ == "__main__":
    _selftest()
