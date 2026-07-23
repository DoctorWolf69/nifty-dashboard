#!/usr/bin/env python3
"""
Pre-entry conviction building — wait for participant continuation before paper open.

Ported faithfully from quant-desk-engine's nifty_entry_conviction.py
(mentor-authored). No formula, threshold, or branch changed.

State flow:
  TRADE_ELIGIBLE -> WAITING_FOR_CONVICTION -> CONVICTION_BUILDING -> ENTRY_CONFIRMED -> enter

This is the ONE module in the porting batch that genuinely needs the
context-normalized OI velocity (compute_contract_velocity), not a null-safe
substitute: its build-up state machine compares normalized deltas tick over
tick (`writer_norm > prev_writer_norm + 0.08`), and those thresholds only
make sense on the normalized scale. Imports from
nifty.analytics.velocity_normalizer — a deliberately narrow, private revival
of the otherwise-reverted normalization engine; see that module's docstring
for the full scope boundary (it does NOT feed the alert gate, confluence
dimensions, gamma monitor, or ranking — those stay on raw ΔOI, unchanged).

Not yet wired into the live pipeline.
Self-check: python -m nifty.analytics.entry_conviction
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from nifty.analytics.velocity_normalizer import compute_contract_velocity

# Conviction score gates
CONVICTION_ENTRY_THRESHOLD = 85
CONVICTION_BROKEN_THRESHOLD = 30
CONVICTION_MIN_BUILD_TICKS = 3
CONVICTION_BUILD_DELTA = 5
CONVICTION_WEAKEN_DELTA = -5

ENTRY_STATUS_WAITING = "WAITING_FOR_CONVICTION"
ENTRY_STATUS_BUILDING = "CONVICTION_BUILDING"
ENTRY_STATUS_CONFIRMED = "ENTRY_CONFIRMED"
ENTRY_STATUS_BROKEN = "BROKEN"
ENTRY_STATUS_REJECTED = "CANDIDATE_REJECTED"
ENTRY_STATUS_IDLE = "IDLE"


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


def _norm_5m(row: Optional[Dict[str, Any]], ctx: Dict[str, Any]) -> float:
    if not row:
        return 0.0
    prof = row.get("oi_velocity") or compute_contract_velocity(row, ctx)
    return _as_float((prof.get("windows_norm") or {}).get("5m"))


def _volume_trend(entry_row: Optional[Dict[str, Any]], prev: Optional[Dict[str, Any]]) -> str:
    if not entry_row or not prev:
        return "UNKNOWN"
    recent = entry_row.get("recent_1m_deltas") or []
    vol_pos = sum(1 for r in recent if _as_int(r.get("volume_delta")) > 0)
    prev_recent = prev.get("recent_1m_deltas") or []
    prev_vol = sum(1 for r in prev_recent if _as_int(r.get("volume_delta")) > 0)
    if vol_pos > prev_vol:
        return "UP"
    if vol_pos < prev_vol:
        return "DOWN"
    return "FLAT"


def _futures_supports(decision: str, futures_layer: Optional[Dict[str, Any]], spot_v5: Dict[str, Any]) -> bool:
    fl = futures_layer or {}
    behavior = str(fl.get("front_behavior") or "FLAT")
    spot_delta = _as_float(spot_v5.get("delta"))
    if decision == "BUY_CE":
        return behavior in {"LONG_BUILD", "SHORT_COVER", "FLAT"} or spot_delta > 0
    if decision == "BUY_PE":
        return behavior in {"SHORT_BUILD", "LONG_UNWIND", "FLAT"} or spot_delta <= 0
    return False


def seed_conviction_score(
    *,
    decision: str,
    candidate: Dict[str, Any],
    pe_analysis: Dict[str, Any],
) -> int:
    """Initial conviction when trade becomes eligible — not entry-ready yet."""
    base = 52
    trade_score = _as_int(candidate.get("total_score"))
    base += min(12, max(0, (trade_score - 65) // 3))
    oi_vel = candidate.get("oi_velocity") or {}
    base += min(8, int(_as_float(oi_vel.get("velocity_score")) / 15))
    behavior = str(pe_analysis.get("behavior") or "")
    if decision == "BUY_CE" and behavior == "PE_ADD_SPOT_UP":
        base += 8
    elif decision == "BUY_PE" and behavior in {"PE_ADD_SPOT_FLAT", "PE_ADD_SPOT_DOWN", "CE_DOMINANT"}:
        base += 6
    return int(_clamp(base, 45, 72))


def classify_participant_tick(
    *,
    decision: str,
    writer_row: Optional[Dict[str, Any]],
    entry_row: Optional[Dict[str, Any]],
    pair: Optional[Dict[str, Any]],
    spot_v5: Dict[str, Any],
    futures_layer: Optional[Dict[str, Any]],
    velocity_ctx: Dict[str, Any],
    pe_analysis: Dict[str, Any],
    prev_tick: Optional[Dict[str, Any]],
) -> Tuple[str, List[str]]:
    """
    Is the same participant continuing to build the position?
    Returns: BUILDING | STABLE | WEAKENING | BROKEN, evidence lines
    """
    evidence: List[str] = []
    spot_delta = _as_float(spot_v5.get("delta"))
    behavior = str(pe_analysis.get("behavior") or "")

    writer_norm = _norm_5m(writer_row, velocity_ctx)
    prev_writer_norm = _as_float((prev_tick or {}).get("writer_norm_5m"))
    entry_norm = _norm_5m(entry_row, velocity_ctx)
    vol_trend = _volume_trend(entry_row, prev_tick or {})

    if decision == "BUY_CE":
        # Thesis: PE support building → long CE
        if behavior in {"PE_UNWIND", "PE_ADD_SPOT_DOWN"}:
            return "BROKEN", ["PE unwind / spot failing — thesis broken"]
        if behavior == "CE_DOMINANT" and spot_delta > 5:
            return "BROKEN", ["CE dominance + spot up — fade invalid"]

        if writer_norm > prev_writer_norm + 0.08 and spot_delta > 0:
            evidence.extend(["PE velocity ↑", "Spot ↑"])
            if _futures_supports(decision, futures_layer, spot_v5):
                evidence.append("Futures ↑")
            if vol_trend == "UP":
                evidence.append("Volume ↑")
            return "BUILDING", evidence

        if writer_norm >= prev_writer_norm - 0.05 and behavior == "PE_ADD_SPOT_UP":
            evidence.extend(["PE steady", "Spot holding"])
            return "STABLE", evidence

        if writer_norm < prev_writer_norm - 0.12 or (spot_delta <= 0 and behavior != "PE_ADD_SPOT_UP"):
            evidence.extend(["PE slowing", "Spot stalled"])
            return "WEAKENING", evidence

        return "STABLE", ["Mixed footprint"]

    if decision == "BUY_PE":
        # Thesis: CE writers adding → fade with long PE
        ce = (pair or {}).get("ce") or writer_row or {}
        ce_unwind = _as_float((ce.get("velocity_5m") or {}).get("pct")) <= -1.5
        if ce_unwind and spot_delta > 3:
            return "BROKEN", ["CE unwind + spot rising — thesis broken"]
        if behavior == "PE_ADD_SPOT_UP":
            return "BROKEN", ["PE+spot rising — contradicts fade"]

        if writer_norm > prev_writer_norm + 0.08 and spot_delta <= 3:
            evidence.extend(["CE velocity ↑", "Spot flat/weak"])
            if _futures_supports(decision, futures_layer, spot_v5):
                evidence.append("Futures ↓")
            if vol_trend == "UP":
                evidence.append("Volume ↑")
            return "BUILDING", evidence

        if writer_norm >= prev_writer_norm - 0.05:
            evidence.extend(["CE steady", "Spot steady"])
            return "STABLE", evidence

        if writer_norm < prev_writer_norm - 0.12 or spot_delta > 5:
            evidence.extend(["CE slowing", "Spot opposite"])
            return "WEAKENING", evidence

        return "STABLE", ["Mixed footprint"]

    return "STABLE", ["Unknown decision"]


def start_entry_watch(
    *,
    candidate: Dict[str, Any],
    pe_analysis: Dict[str, Any],
    writer_row: Optional[Dict[str, Any]],
    velocity_ctx: Dict[str, Any],
) -> Dict[str, Any]:
    decision = str(candidate.get("decision") or "")
    conviction = seed_conviction_score(
        decision=decision,
        candidate=candidate,
        pe_analysis=pe_analysis,
    )
    writer_norm = _norm_5m(writer_row, velocity_ctx)
    return {
        "signal_key": str(candidate.get("signal_key") or ""),
        "decision": decision,
        "strike": _as_int(candidate.get("strike")),
        "writer_contract": candidate.get("writer_contract"),
        "entry_contract": candidate.get("entry_contract"),
        "trade_score": _as_int(candidate.get("total_score")),
        "trade_grade": candidate.get("grade"),
        "entry_status": ENTRY_STATUS_WAITING,
        "conviction_score": conviction,
        "conviction_label": "SEED",
        "conviction_history": [conviction],
        "build_ticks": 0,
        "tick_count": 0,
        "entry_confirmed": False,
        "rejected": False,
        "evidence": ["Trade eligible — watching participant continuation"],
        "message": "Waiting for continuation…",
        "writer_norm_5m": writer_norm,
        "last_tick_class": "STABLE",
    }


def update_entry_watch(
    watch: Dict[str, Any],
    *,
    writer_row: Optional[Dict[str, Any]],
    entry_row: Optional[Dict[str, Any]],
    pair: Optional[Dict[str, Any]],
    spot_v5: Dict[str, Any],
    futures_layer: Optional[Dict[str, Any]],
    velocity_ctx: Dict[str, Any],
    pe_analysis: Dict[str, Any],
) -> Dict[str, Any]:
    """Advance conviction one tick; mutates watch in place and returns it."""
    if not watch or watch.get("rejected") or watch.get("entry_confirmed"):
        return watch

    prev_snapshot = {
        "writer_norm_5m": watch.get("writer_norm_5m"),
        "recent_1m_deltas": (entry_row or {}).get("recent_1m_deltas"),
    }
    tick_class, evidence = classify_participant_tick(
        decision=str(watch.get("decision") or ""),
        writer_row=writer_row,
        entry_row=entry_row,
        pair=pair,
        spot_v5=spot_v5,
        futures_layer=futures_layer,
        velocity_ctx=velocity_ctx,
        pe_analysis=pe_analysis,
        prev_tick=prev_snapshot,
    )

    score = _as_int(watch.get("conviction_score"), 50)
    if tick_class == "BUILDING":
        score = int(_clamp(score + CONVICTION_BUILD_DELTA, 0, 100))
        watch["build_ticks"] = _as_int(watch.get("build_ticks")) + 1
        watch["conviction_label"] = "BUILDING"
    elif tick_class == "WEAKENING":
        score = int(_clamp(score + CONVICTION_WEAKEN_DELTA, 0, 100))
        watch["conviction_label"] = "WEAKENING"
    elif tick_class == "BROKEN":
        watch["entry_status"] = ENTRY_STATUS_BROKEN
        watch["rejected"] = True
        watch["conviction_label"] = "BROKEN"
        watch["message"] = "Participant thesis broken — candidate rejected"
        watch["evidence"] = evidence
        watch["last_tick_class"] = tick_class
        return watch
    else:
        watch["conviction_label"] = "STABLE"

    watch["conviction_score"] = score
    history = list(watch.get("conviction_history") or [])
    history.append(score)
    watch["conviction_history"] = history[-12:]
    watch["tick_count"] = _as_int(watch.get("tick_count")) + 1
    watch["last_tick_class"] = tick_class
    watch["evidence"] = evidence
    watch["writer_norm_5m"] = _norm_5m(writer_row, velocity_ctx)

    if watch.get("entry_status") == ENTRY_STATUS_WAITING:
        watch["entry_status"] = ENTRY_STATUS_BUILDING

    if score <= CONVICTION_BROKEN_THRESHOLD and tick_class == "WEAKENING":
        watch["entry_status"] = ENTRY_STATUS_REJECTED
        watch["rejected"] = True
        watch["message"] = "Conviction collapsed — candidate rejected"
        return watch

    if (
        score >= CONVICTION_ENTRY_THRESHOLD
        and _as_int(watch.get("build_ticks")) >= CONVICTION_MIN_BUILD_TICKS
    ):
        watch["entry_status"] = ENTRY_STATUS_CONFIRMED
        watch["entry_confirmed"] = True
        watch["conviction_label"] = "CONFIRMED"
        watch["message"] = "Entry confirmed — participant continuation proven"
    else:
        watch["message"] = "Waiting for continuation…"

    return watch


def conviction_pass(watch: Optional[Dict[str, Any]]) -> bool:
    return bool(watch and watch.get("entry_confirmed"))


def attach_entry_fields(candidate: Dict[str, Any], watch: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge entry conviction into candidate for scoreboard / API."""
    w = watch or {}
    same_key = str(candidate.get("signal_key") or "") == str(w.get("signal_key") or "")
    if not same_key:
        candidate["entry_status"] = ENTRY_STATUS_IDLE if candidate.get("paper_eligible") else "NOT_ELIGIBLE"
        candidate["conviction_score"] = None
        candidate["entry_confirmed"] = False
        return candidate
    candidate["entry_status"] = w.get("entry_status", ENTRY_STATUS_IDLE)
    candidate["conviction_score"] = w.get("conviction_score")
    candidate["conviction_label"] = w.get("conviction_label")
    candidate["conviction_history"] = w.get("conviction_history")
    candidate["entry_evidence"] = w.get("evidence")
    candidate["entry_message"] = w.get("message")
    candidate["entry_confirmed"] = bool(w.get("entry_confirmed"))
    candidate["conviction_pass"] = conviction_pass(w)
    return candidate


def _selftest() -> None:
    from datetime import datetime as _dt

    from nifty.analytics.velocity_normalizer import build_velocity_context

    candidate = {"signal_key": "23000:PE:BUY_CE", "decision": "BUY_CE", "strike": 23000,
                 "writer_contract": "NIFTY23JUN23000PE", "entry_contract": "NIFTY23JUN23000CE",
                 "total_score": 78, "grade": "B"}
    pe_analysis = {"behavior": "PE_ADD_SPOT_UP"}
    # Pinned `now` — makes the test deterministic (velocity_normalizer's
    # time_of_day_factor otherwise reads the wall clock).
    ctx = build_velocity_context(atr_pts=250, days_to_expiry=3, chain_rows=[], now=_dt(2026, 6, 19, 10, 0))

    seed = seed_conviction_score(decision="BUY_CE", candidate=candidate, pe_analysis=pe_analysis)
    assert 45 <= seed <= 72

    watch = start_entry_watch(candidate=candidate, pe_analysis=pe_analysis, writer_row=None, velocity_ctx=ctx)
    assert watch["entry_status"] == ENTRY_STATUS_WAITING
    assert watch["build_ticks"] == 0

    # Feed escalating PE-writer deltas — normalized velocity must keep climbing
    # tick over tick (>0.08 each step) for BUILDING to fire repeatedly; a flat
    # delta only ever fires BUILDING once (correctly — nothing is accelerating).
    spot_v5 = {"delta": 10.0}
    for delta in (20_000, 45_000, 80_000, 130_000, 190_000):
        writer_row = {"oi": 500_000, "volume": 100_000, "velocity_5m": {"delta": delta}}
        watch = update_entry_watch(
            watch, writer_row=writer_row, entry_row=None, pair=None, spot_v5=spot_v5,
            futures_layer=None, velocity_ctx=ctx, pe_analysis=pe_analysis,
        )
        if watch["entry_confirmed"]:
            break

    assert watch["build_ticks"] >= CONVICTION_MIN_BUILD_TICKS, watch
    assert watch["entry_confirmed"] is True, watch
    assert watch["entry_status"] == ENTRY_STATUS_CONFIRMED
    assert conviction_pass(watch) is True

    # A flat (non-accelerating) delta should NOT sustain BUILDING streaks.
    flat_watch = start_entry_watch(candidate=candidate, pe_analysis=pe_analysis, writer_row=None, velocity_ctx=ctx)
    flat_row = {"oi": 500_000, "volume": 100_000, "velocity_5m": {"delta": 50_000}}
    for _ in range(5):
        flat_watch = update_entry_watch(
            flat_watch, writer_row=flat_row, entry_row=None, pair=None, spot_v5=spot_v5,
            futures_layer=None, velocity_ctx=ctx, pe_analysis=pe_analysis,
        )
    assert flat_watch["entry_confirmed"] is False, flat_watch
    assert flat_watch["build_ticks"] <= 1, flat_watch  # only the first tick reads as BUILDING

    # A BROKEN case: PE unwinding invalidates a BUY_CE thesis outright.
    broken_watch = start_entry_watch(candidate=candidate, pe_analysis=pe_analysis, writer_row=None, velocity_ctx=ctx)
    broken_watch = update_entry_watch(
        broken_watch, writer_row=writer_row, entry_row=None, pair=None, spot_v5=spot_v5,
        futures_layer=None, velocity_ctx=ctx, pe_analysis={"behavior": "PE_UNWIND"},
    )
    assert broken_watch["entry_status"] == ENTRY_STATUS_BROKEN
    assert broken_watch["rejected"] is True

    attached = attach_entry_fields(dict(candidate), watch)
    assert attached["conviction_pass"] is True
    stale = attach_entry_fields({"signal_key": "other", "paper_eligible": True}, watch)
    assert stale["entry_status"] == ENTRY_STATUS_IDLE

    print("[analytics.entry_conviction] selftest OK: seed, build-up to CONFIRMED, BROKEN thesis, field attach")


if __name__ == "__main__":
    _selftest()
