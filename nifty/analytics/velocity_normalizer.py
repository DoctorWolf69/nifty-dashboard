#!/usr/bin/env python3
"""Context-normalized OI velocity — PRIVATE input for entry_conviction.py ONLY.

============================================================================
SCOPE WARNING — read before importing this anywhere else
============================================================================
This is a deliberately narrow revival of quant-desk-engine's
nifty_oi_velocity_engine.py. That full engine (context-normalized z-scores
replacing raw ΔOI across the alert gate, the `oi_velocity` confluence
dimension, the gamma monitor, writer-add ranking, and intent_filter's chain
bias) was tried in this repo and explicitly reverted — commits
67e60ea..137efd2 — after it measurably worsened results on the one archived
day it was tested against (see tests/BASELINES.md / CHANGE_REPORT_OIV.md).
That decision stands everywhere except one place.

nifty_entry_conviction.py's tick-by-tick build-up state machine
(WAITING_FOR_CONVICTION -> CONVICTION_BUILDING -> ENTRY_CONFIRMED) compares
normalized velocity deltas tick-over-tick (`writer_norm > prev_writer_norm +
0.08`). Fed raw deltas instead, every comparison collapses to `0.0 > 0.08`
forever — the state machine wouldn't error, it would just silently never
confirm an entry via this path. That is worse than reviving one function.

So: ONLY compute_contract_velocity (+ the small helpers it needs) is ported
here, for entry_conviction's exclusive use. Everything that fed the
REVERTED paths is deliberately NOT included, so it cannot be accidentally
re-wired:
  - evaluate_alert_velocity        (fed the alert gate — reverted)
  - velocity_percentile            (fed the alert gate — reverted)
  - score_oi_velocity_pass         (fed the oi_velocity confluence dim — reverted)
  - score_oi_sustained_pass        (fed the oi_sustained confluence dim — reverted)
  - compute_chain_bias_normalized  (fed intent_filter's chain bias — reverted;
                                     intent_filter.py's port already omits this)
  - opportunity_velocity_factors   (already ported — inlined in
                                     probability_engine.py, which needs it
                                     null-safe, not this file)
============================================================================

No formula changed from the original for what IS included here.
Not yet wired into the live pipeline.
Self-check: python -m nifty.analytics.velocity_normalizer
"""

from __future__ import annotations

import math
from datetime import datetime, time
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

WINDOWS_SEC: Dict[str, int] = {
    "30s": 30,
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
}


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def time_of_day_factor(now: Optional[datetime] = None) -> float:
    """Higher expected OI churn -> larger raw deltas needed for same signal."""
    dt = now.astimezone(IST) if now else datetime.now(IST)
    t = dt.time()
    if time(9, 15) <= t < time(9, 45):
        return 1.35
    if time(9, 45) <= t < time(11, 30):
        return 1.15
    if time(11, 30) <= t < time(14, 0):
        return 0.85
    if time(14, 0) <= t < time(15, 30):
        return 1.20
    return 1.0


def days_to_expiry_factor(days_to_expiry: Optional[int]) -> float:
    if days_to_expiry is None or days_to_expiry < 0:
        return 1.0
    if days_to_expiry <= 1:
        return 1.45
    if days_to_expiry <= 3:
        return 1.25
    if days_to_expiry <= 7:
        return 1.10
    return 0.95


def strike_liquidity_factor(*, oi: int, volume: int) -> float:
    """Deep strikes need larger absolute ΔOI to matter."""
    oi_ref = max(50_000, oi)
    vol_ref = max(1_000, volume)
    depth = math.log10(oi_ref) + 0.25 * math.log10(vol_ref)
    return _clamp(0.75 + (depth - 5.0) * 0.12, 0.65, 1.85)


def atr_factor(atr_pts: float) -> float:
    """Scale normalization to expected daily range."""
    if atr_pts <= 0:
        return 1.0
    return _clamp(atr_pts / 295.0, 0.55, 1.65)


def chain_median_positive_delta(chain_rows: Optional[List[Dict[str, Any]]], window_key: str = "5m") -> float:
    """Historical average positive OI change for outlier detection."""
    attr = f"velocity_{window_key}" if window_key != "30s" else "velocity_30s"
    if window_key == "3m":
        attr = "velocity_3m"
    positives: List[float] = []
    for row in chain_rows or []:
        if _as_int(row.get("oi")) <= 0:
            continue
        vel = row.get(attr) or {}
        delta = _as_float(vel.get("delta"))
        if delta > 0:
            positives.append(delta)
    if not positives:
        return 75_000.0
    positives.sort()
    mid = len(positives) // 2
    return positives[mid] if len(positives) % 2 else (positives[mid - 1] + positives[mid]) / 2.0


def normalize_raw_delta(
    raw_delta: float,
    *,
    oi_baseline: int,
    atr_pts: float,
    tod_factor: float,
    dte_factor: float,
    liquidity_factor: float,
    hist_avg_delta: float,
) -> float:
    """
    Context-normalized OI velocity (signed).
    Positive = OI building; magnitude = significance vs context.
    """
    if raw_delta == 0:
        return 0.0
    oi_scale = max(25_000.0, abs(oi_baseline) * 0.0025)
    hist_scale = max(40_000.0, hist_avg_delta * 2.5)
    denom = oi_scale * atr_factor(atr_pts) * tod_factor * dte_factor * liquidity_factor
    denom = max(denom, hist_scale * 0.35)
    norm = raw_delta / denom
    return round(norm * 100.0, 4)


def _window_delta(row: Dict[str, Any], window: str) -> float:
    key_map = {
        "30s": "velocity_30s",
        "1m": "velocity_1m",
        "3m": "velocity_3m",
        "5m": "velocity_5m",
        "15m": "velocity_15m",
    }
    vel = row.get(key_map[window]) or {}
    return _as_float(vel.get("delta"))


def build_velocity_context(
    *,
    atr_pts: float = 295.0,
    days_to_expiry: Optional[int] = None,
    chain_rows: Optional[List[Dict[str, Any]]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    return {
        "atr_pts": atr_pts,
        "days_to_expiry": days_to_expiry,
        "chain_rows": chain_rows or [],
        "tod_factor": time_of_day_factor(now),
        "dte_factor": days_to_expiry_factor(days_to_expiry),
        "hist_avg_5m": chain_median_positive_delta(chain_rows, "5m"),
        "hist_avg_1m": chain_median_positive_delta(chain_rows, "1m"),
    }


def compute_contract_velocity(
    row: Dict[str, Any],
    ctx: Dict[str, Any],
) -> Dict[str, Any]:
    """Normalize all windows for one contract row or alert."""
    oi = _as_int(row.get("oi"))
    volume = _as_int(row.get("volume"))
    liq = strike_liquidity_factor(oi=oi, volume=volume)
    tod = _as_float(ctx.get("tod_factor"), 1.0)
    dte = _as_float(ctx.get("dte_factor"), 1.0)
    atr = _as_float(ctx.get("atr_pts"), 295.0)
    hist_5m = _as_float(ctx.get("hist_avg_5m"), 75_000.0)
    hist_1m = _as_float(ctx.get("hist_avg_1m"), 35_000.0)

    windows_norm: Dict[str, float] = {}
    windows_raw: Dict[str, float] = {}
    for label, _sec in WINDOWS_SEC.items():
        raw = _window_delta(row, label)
        windows_raw[label] = raw
        hist = hist_1m if label in {"30s", "1m"} else hist_5m
        windows_norm[label] = normalize_raw_delta(
            raw,
            oi_baseline=oi,
            atr_pts=atr,
            tod_factor=tod,
            dte_factor=dte,
            liquidity_factor=liq,
            hist_avg_delta=hist,
        )

    v1 = windows_norm.get("1m", 0.0)
    v3 = windows_norm.get("3m", 0.0)
    v5 = windows_norm.get("5m", 0.0)
    v15 = windows_norm.get("15m", 0.0)
    v30 = windows_norm.get("30s", 0.0)

    # Primary score: weighted blend of normalized windows (5m heaviest)
    velocity_score = _clamp(
        abs(v5) * 0.40
        + abs(v1) * 0.25
        + abs(v3) * 0.15
        + abs(v15) * 0.10
        + abs(v30) * 0.10,
        0.0,
        100.0,
    )
    signed_score = round(math.copysign(velocity_score, v5 if v5 != 0 else v1), 2)

    short_avg = (v30 + v1) / 2.0
    long_avg = (v3 + v5) / 2.0
    acceleration = round(short_avg - long_avg, 4)
    deceleration = round(long_avg - short_avg, 4) if long_avg > short_avg else 0.0

    recent = row.get("recent_1m_deltas") or []
    sustained_norm = 0
    for entry in recent:
        raw_min = _as_float(entry.get("oi_delta"))
        norm_min = normalize_raw_delta(
            raw_min,
            oi_baseline=oi,
            atr_pts=atr,
            tod_factor=tod,
            dte_factor=dte,
            liquidity_factor=liq,
            hist_avg_delta=hist_1m,
        )
        if norm_min >= 0.45:  # SUSTAINED_NORM_MIN_SCORE
            sustained_norm += 1

    return {
        "windows_raw": windows_raw,
        "windows_norm": windows_norm,
        "velocity_score": round(velocity_score, 2),
        "signed_velocity_score": signed_score,
        "acceleration": acceleration,
        "deceleration": deceleration,
        "sustained_norm_minutes": sustained_norm,
        "liquidity_factor": round(liq, 3),
        "normalization": {
            "atr_pts": atr,
            "tod_factor": tod,
            "dte_factor": dte,
            "hist_avg_5m": hist_5m,
            "hist_avg_1m": hist_1m,
        },
    }


def _selftest() -> None:
    assert time_of_day_factor(datetime(2026, 6, 19, 9, 20)) == 1.35  # ORB window
    assert time_of_day_factor(datetime(2026, 6, 19, 12, 30)) == 0.85  # midday lull

    assert days_to_expiry_factor(0) == 1.45
    assert days_to_expiry_factor(10) == 0.95

    liq = strike_liquidity_factor(oi=1_000_000, volume=500_000)
    assert 0.65 <= liq <= 1.85

    assert normalize_raw_delta(
        0, oi_baseline=500_000, atr_pts=200, tod_factor=1.0, dte_factor=1.0,
        liquidity_factor=1.0, hist_avg_delta=75_000,
    ) == 0.0

    ctx = build_velocity_context(atr_pts=250, days_to_expiry=2, chain_rows=[])
    assert ctx["dte_factor"] == 1.25
    assert ctx["hist_avg_5m"] == 75_000.0  # empty chain -> default fallback

    row = {"oi": 600_000, "volume": 200_000, "velocity_5m": {"delta": 90_000}, "velocity_1m": {"delta": 30_000}}
    profile = compute_contract_velocity(row, ctx)
    assert profile["windows_raw"]["5m"] == 90_000
    assert profile["velocity_score"] >= 0
    assert "acceleration" in profile

    # Zero-activity row normalizes to a flat zero everywhere — the honest
    # "nothing happened" reading, not an error.
    flat_row = {"oi": 600_000, "volume": 0}
    flat = compute_contract_velocity(flat_row, ctx)
    assert all(v == 0.0 for v in flat["windows_norm"].values())

    print("[analytics.velocity_normalizer] selftest OK: factors, normalization, contract velocity")


if __name__ == "__main__":
    _selftest()
