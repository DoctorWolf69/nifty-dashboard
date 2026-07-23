#!/usr/bin/env python3
"""
Opposite conviction — observe-only falsification lane beside legacy entry watch.

v1 (T-013): seller-winning pressure on faded OI + mirror trade read (BUY_PE <-> BUY_CE).
Does not affect paper gates, §21, or legacy conviction scoring.

Ported faithfully from quant-desk-engine v4/ATLAS's nifty_opposite_conviction.py
(mentor-authored). No logic changed. Only adaptation: imports point at
nifty.analytics.entry_conviction.classify_participant_tick and
nifty.analytics.velocity_normalizer.compute_contract_velocity - both
already ported this session (the latter is the narrow, quarantined revival
of the reverted OI-velocity-normalization engine, reused here only for
this new observe-only lane, same as entry_conviction.py already does;
still not re-wired into confluence.py per the user's earlier decision).

Genuinely new capability, explicitly observe-only by the source's own
docstring: watches whether the faded (written-against) OI side is
"winning" against the legacy trade thesis, and mirrors the opposite
decision (BUY_PE <-> BUY_CE) through the same tick-classification logic
used for the primary conviction watch, purely for falsification /
research signal - it does not touch paper_eligible, confluence scoring,
or any gate.

Not yet wired into the live pipeline.
Self-check: python -m nifty.analytics.opposite_conviction
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from nifty.analytics.entry_conviction import classify_participant_tick
from nifty.analytics.velocity_normalizer import compute_contract_velocity

OPPOSITE_JOURNAL_SEC = 60

PRESSURE_WINNING = "SELLERS_WINNING"
PRESSURE_NEUTRAL = "NEUTRAL"
PRESSURE_CONFIRMING = "CONFIRMING_LEGACY"


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


def mirror_decision(decision: str) -> str:
    if decision == "BUY_PE":
        return "BUY_CE"
    if decision == "BUY_CE":
        return "BUY_PE"
    return ""


def mirror_sides(decision: str) -> Tuple[str, str]:
    """Return (mirror_writer_side, mirror_entry_side) at same strike."""
    if decision == "BUY_PE":
        return "PE", "CE"
    if decision == "BUY_CE":
        return "CE", "PE"
    return "", ""


def _norm_5m(row: Optional[Dict[str, Any]], ctx: Dict[str, Any]) -> float:
    if not row:
        return 0.0
    prof = row.get("oi_velocity") or compute_contract_velocity(row, ctx)
    return _as_float((prof.get("windows_norm") or {}).get("5m"))


def classify_seller_pressure(
    *,
    legacy_decision: str,
    writer_row: Optional[Dict[str, Any]],
    spot_v5: Dict[str, Any],
    velocity_ctx: Dict[str, Any],
    pe_analysis: Dict[str, Any],
    prev_writer_norm: float,
) -> Tuple[str, int, List[str]]:
    """
    Is the faded OI side winning against the legacy thesis? (Laws 1-2: OI fact, not identity)
    BUY_PE -> CE OI; BUY_CE -> PE OI.
    """
    spot_delta = _as_float(spot_v5.get("delta"))
    behavior = str(pe_analysis.get("behavior") or "")
    writer_norm = _norm_5m(writer_row, velocity_ctx)
    evidence: List[str] = []

    if legacy_decision == "BUY_PE":
        if behavior in {"CE_DOMINANT", "PE_ADD_SPOT_UP"} and spot_delta > 3:
            evidence.extend(["CE OI dominant", f"Spot +{spot_delta:.1f}"])
            return PRESSURE_WINNING, 82, evidence
        if writer_norm > prev_writer_norm + 0.08 and spot_delta > 2:
            evidence.extend(["CE OI velocity ↑", f"Spot +{spot_delta:.1f}"])
            return PRESSURE_WINNING, 75, evidence
        if behavior in {"PE_UNWIND", "PE_ADD_SPOT_DOWN"} or (
            writer_norm < prev_writer_norm - 0.1 and spot_delta <= 0
        ):
            evidence.extend(["CE slowing / spot weak", "Fade thesis holding"])
            return PRESSURE_CONFIRMING, 28, evidence
        if spot_delta > 5:
            evidence.append(f"Spot ripping +{spot_delta:.1f} vs fade")
            return PRESSURE_WINNING, 68, evidence
        return PRESSURE_NEUTRAL, 48, ["Mixed — no clear seller win"]

    if legacy_decision == "BUY_CE":
        if behavior in {"PE_ADD_SPOT_UP", "PE_ADD_SPOT_FLAT"} and spot_delta > 3:
            evidence.extend(["PE OI adding", f"Spot +{spot_delta:.1f}"])
            return PRESSURE_WINNING, 78, evidence
        if writer_norm > prev_writer_norm + 0.08 and spot_delta > 2:
            evidence.extend(["PE OI velocity ↑", f"Spot +{spot_delta:.1f}"])
            return PRESSURE_WINNING, 74, evidence
        if behavior in {"CE_DOMINANT"} or (writer_norm < prev_writer_norm - 0.1 and spot_delta < 0):
            evidence.extend(["PE slowing / spot fading", "Long CE thesis intact"])
            return PRESSURE_CONFIRMING, 30, evidence
        if spot_delta < -5:
            evidence.append(f"Spot weak {spot_delta:.1f} vs CE long")
            return PRESSURE_CONFIRMING, 35, evidence
        return PRESSURE_NEUTRAL, 48, ["Mixed — no clear seller win"]

    return PRESSURE_NEUTRAL, 50, ["Unknown legacy decision"]


def start_opposite_watch(*, thesis: Dict[str, Any]) -> Dict[str, Any]:
    legacy_decision = str(thesis.get("decision") or "")
    mirror = mirror_decision(legacy_decision)
    mw, me = mirror_sides(legacy_decision)
    return {
        "legacy_signal_key": str(thesis.get("signal_key") or ""),
        "legacy_decision": legacy_decision,
        "mirror_decision": mirror,
        "strike": _as_int(thesis.get("strike")),
        "writer_side": str(thesis.get("writer_side") or ""),
        "mirror_writer_side": mw,
        "mirror_entry_side": me,
        "pressure": PRESSURE_NEUTRAL,
        "pressure_score": 45,
        "seller_evidence": ["Opposite lane open — watching faded OI"],
        "mirror_tick_class": "STABLE",
        "mirror_evidence": [],
        "read": "Observe only — falsify legacy thesis",
        "tick_count": 0,
        "writer_norm_5m": 0.0,
        "last_journal_ts": 0.0,
        "active": True,
    }


def update_opposite_watch(
    watch: Dict[str, Any],
    *,
    writer_row: Optional[Dict[str, Any]],
    mirror_writer_row: Optional[Dict[str, Any]],
    mirror_entry_row: Optional[Dict[str, Any]],
    pair: Optional[Dict[str, Any]],
    spot_v5: Dict[str, Any],
    futures_layer: Optional[Dict[str, Any]],
    velocity_ctx: Dict[str, Any],
    pe_analysis: Dict[str, Any],
    mirror_pe_analysis: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not watch or not watch.get("active"):
        return watch

    prev_norm = _as_float(watch.get("writer_norm_5m"))
    pressure, score, seller_ev = classify_seller_pressure(
        legacy_decision=str(watch.get("legacy_decision") or ""),
        writer_row=writer_row,
        spot_v5=spot_v5,
        velocity_ctx=velocity_ctx,
        pe_analysis=pe_analysis,
        prev_writer_norm=prev_norm,
    )
    watch["pressure"] = pressure
    watch["pressure_score"] = score
    watch["seller_evidence"] = seller_ev
    watch["writer_norm_5m"] = _norm_5m(writer_row, velocity_ctx)
    watch["tick_count"] = _as_int(watch.get("tick_count")) + 1

    mirror_dec = str(watch.get("mirror_decision") or "")
    mirror_analysis = mirror_pe_analysis or pe_analysis
    prev_mirror = {"writer_norm_5m": watch.get("mirror_writer_norm_5m")}
    mirror_class, mirror_ev = classify_participant_tick(
        decision=mirror_dec,
        writer_row=mirror_writer_row,
        entry_row=mirror_entry_row,
        pair=pair,
        spot_v5=spot_v5,
        futures_layer=futures_layer,
        velocity_ctx=velocity_ctx,
        pe_analysis=mirror_analysis,
        prev_tick=prev_mirror,
    )
    watch["mirror_tick_class"] = mirror_class
    watch["mirror_evidence"] = mirror_ev
    watch["mirror_writer_norm_5m"] = _norm_5m(mirror_writer_row, velocity_ctx)

    if pressure == PRESSURE_WINNING:
        watch["read"] = f"Faded OI side winning — mirror {mirror_dec} footprint {mirror_class}"
    elif pressure == PRESSURE_CONFIRMING:
        watch["read"] = f"Legacy side intact — mirror {mirror_dec} {mirror_class}"
    else:
        watch["read"] = f"Neutral pressure — mirror {mirror_dec} {mirror_class}"

    return watch


def should_journal_opposite(watch: Dict[str, Any], now_ts: float) -> bool:
    last = _as_float(watch.get("last_journal_ts"))
    return (now_ts - last) >= OPPOSITE_JOURNAL_SEC


def opposite_to_api(watch: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not watch or not watch.get("active"):
        return None
    return dict(watch)


def _selftest() -> None:
    assert mirror_decision("BUY_PE") == "BUY_CE"
    assert mirror_decision("BUY_CE") == "BUY_PE"
    assert mirror_decision("") == ""

    assert mirror_sides("BUY_PE") == ("PE", "CE")
    assert mirror_sides("BUY_CE") == ("CE", "PE")
    assert mirror_sides("") == ("", "")

    # classify_seller_pressure: CE dominant + spot up while legacy is BUY_PE -> sellers winning.
    pressure, score, evidence = classify_seller_pressure(
        legacy_decision="BUY_PE",
        writer_row={"oi": 500000, "volume": 20000},
        spot_v5={"delta": 5.0},
        velocity_ctx={},
        pe_analysis={"behavior": "CE_DOMINANT"},
        prev_writer_norm=0.1,
    )
    assert pressure == PRESSURE_WINNING and score == 82
    assert evidence

    # PE unwind while legacy BUY_PE -> confirming (fade thesis holding).
    pressure2, score2, _ = classify_seller_pressure(
        legacy_decision="BUY_PE",
        writer_row=None,
        spot_v5={"delta": -1.0},
        velocity_ctx={},
        pe_analysis={"behavior": "PE_UNWIND"},
        prev_writer_norm=0.1,
    )
    assert pressure2 == PRESSURE_CONFIRMING

    # Unknown legacy decision -> neutral default.
    pressure3, score3, evidence3 = classify_seller_pressure(
        legacy_decision="",
        writer_row=None,
        spot_v5={},
        velocity_ctx={},
        pe_analysis={},
        prev_writer_norm=0.0,
    )
    assert pressure3 == PRESSURE_NEUTRAL and "Unknown" in evidence3[0]

    watch = start_opposite_watch(thesis={"decision": "BUY_PE", "signal_key": "sig1", "strike": 25000, "writer_side": "PE"})
    assert watch["mirror_decision"] == "BUY_CE"
    assert watch["mirror_writer_side"] == "PE" and watch["mirror_entry_side"] == "CE"
    assert watch["active"] is True
    assert watch["tick_count"] == 0

    updated = update_opposite_watch(
        dict(watch),
        writer_row={"oi": 500000, "volume": 20000},
        mirror_writer_row=None,
        mirror_entry_row=None,
        pair=None,
        spot_v5={"delta": 5.0},
        futures_layer=None,
        velocity_ctx={},
        pe_analysis={"behavior": "CE_DOMINANT"},
    )
    assert updated["pressure"] == PRESSURE_WINNING
    assert updated["tick_count"] == 1
    assert "mirror" in updated["read"].lower()

    inactive = update_opposite_watch({"active": False}, writer_row=None, mirror_writer_row=None, mirror_entry_row=None, pair=None, spot_v5={}, futures_layer=None, velocity_ctx={}, pe_analysis={})
    assert inactive == {"active": False}  # no-op when inactive

    assert should_journal_opposite({"last_journal_ts": 0.0}, 61.0) is True
    assert should_journal_opposite({"last_journal_ts": 0.0}, 30.0) is False

    assert opposite_to_api(None) is None
    assert opposite_to_api({"active": False}) is None
    api_row = opposite_to_api(updated)
    assert api_row is not None and api_row["pressure"] == PRESSURE_WINNING

    print("[analytics.opposite_conviction] selftest OK: mirror sides, seller pressure, watch lifecycle")


if __name__ == "__main__":
    _selftest()
