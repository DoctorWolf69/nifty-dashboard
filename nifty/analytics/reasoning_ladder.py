#!/usr/bin/env python3
"""Desk Reasoning Ladder v1.0 — observational explanation of how a candidate formed.

Ported faithfully from quant-desk-engine v4/ATLAS's desk_reasoning_ladder.py
(mentor-authored). No logic changed. Fully self-contained in the source
too, so no import adaptation was needed.

1A: positioning hypothesis only (never participant identity).
2A: observability only — does not modify scores, blockers, paper_eligible, or
EV gates.

Causality:
  Evidence evaluation -> Positioning inference -> (desk policy) Candidate / Paper

Never: paper_eligible -> inference.

This is likely the source for nifty-dashboard's deferred "Why" scoreboard
column (see the porting-effort-status memory / MIGRATION notes) — a
human-readable causality chain (`ladder_summary_line`) built from a
candidate's own confluence dimensions, with a hard invariant
(`assert_no_identity_labels`) that forbids ever claiming participant
identity ("likely writer", "hedger") — only a positioning hypothesis tied
to OI side.

Not yet wired into the live pipeline.
Self-check: python -m nifty.analytics.reasoning_ladder
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

DESK_REASONING_VERSION = "1.0"

REASONING_STATUS = frozenset(
    {"UNKNOWN", "BUILDING", "SUPPORTED", "CONTRADICTED", "INCOMPLETE"}
)

ALLOWED_HYPOTHESIS_IDS = frozenset(
    {"CALL_SIDE_POSITIONING", "PUT_SIDE_POSITIONING", "UNKNOWN_POSITIONING"}
)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def positioning_hypothesis_for_side(oi_side: str) -> Dict[str, Any]:
    side = str(oi_side or "").upper()
    if side == "CE":
        return {
            "id": "CALL_SIDE_POSITIONING",
            "label": "Call-side positioning",
            "oi_side": "CE",
            "execution_policy_if_confirmed": "BUY_PE",
            "statement": "CE OI adding → call-side positioning hypothesis (identity unknown)",
        }
    if side == "PE":
        return {
            "id": "PUT_SIDE_POSITIONING",
            "label": "Put-side positioning",
            "oi_side": "PE",
            "execution_policy_if_confirmed": "BUY_CE",
            "statement": "PE OI adding → put-side positioning hypothesis (identity unknown)",
        }
    return {
        "id": "UNKNOWN_POSITIONING",
        "label": "Unknown positioning",
        "oi_side": side or None,
        "execution_policy_if_confirmed": None,
        "statement": "OI side unknown — no positioning hypothesis",
    }


def _evidence_from_dimensions(
    dimensions: Optional[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    supporting: List[Dict[str, Any]] = []
    contradicting: List[Dict[str, Any]] = []
    for key, dim in (dimensions or {}).items():
        if not isinstance(dim, dict):
            continue
        row = {
            "id": str(key),
            "pass": bool(dim.get("pass")),
            "detail": str(dim.get("detail") or ""),
            "score": _as_int(dim.get("score")),
            "max": _as_int(dim.get("max")),
        }
        if row["pass"]:
            supporting.append(row)
        else:
            contradicting.append(row)
    return {"supporting": supporting, "contradicting": contradicting}


def _merge_extra_evidence(
    base: Dict[str, List[Dict[str, Any]]],
    extra: Optional[Sequence[Dict[str, Any]]],
) -> Dict[str, List[Dict[str, Any]]]:
    if not extra:
        return base
    supporting = list(base.get("supporting") or [])
    contradicting = list(base.get("contradicting") or [])
    for item in extra:
        if not isinstance(item, dict):
            continue
        row = {
            "id": str(item.get("id") or item.get("evidence") or "extra"),
            "pass": bool(item.get("pass", True)),
            "detail": str(item.get("detail") or item.get("evidence") or ""),
            "score": _as_int(item.get("score")),
            "max": _as_int(item.get("max")),
            "source": str(item.get("source") or "extra"),
        }
        if row["pass"]:
            supporting.append(row)
        else:
            contradicting.append(row)
    return {"supporting": supporting, "contradicting": contradicting}


def _infer_from_evidence(
    *,
    hypothesis: Dict[str, Any],
    evidence: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """Positioning inference from evidence balance only — never paper_eligible."""
    supporting = evidence.get("supporting") or []
    contradicting = evidence.get("contradicting") or []
    n_sup = len(supporting)
    n_con = len(contradicting)
    total = n_sup + n_con

    if hypothesis.get("id") == "UNKNOWN_POSITIONING":
        state = "INCOMPLETE"
        reasoning_status = "UNKNOWN"
        confidence_pct = 0.0
    elif total == 0:
        state = "INCOMPLETE"
        reasoning_status = "INCOMPLETE"
        confidence_pct = 0.0
    else:
        share = n_sup / total
        confidence_pct = round(share * 100.0, 1)
        if n_sup == 0 and n_con > 0:
            state = "REJECTED"
            reasoning_status = "CONTRADICTED"
        elif share >= 0.70 and n_sup >= 4:
            state = "SUPPORTED"
            reasoning_status = "SUPPORTED"
        elif share <= 0.35 and n_con >= 3:
            state = "REJECTED"
            reasoning_status = "CONTRADICTED"
        elif n_sup < 3 or total < 4:
            state = "INCOMPLETE"
            reasoning_status = "BUILDING"
        else:
            state = "WEAK"
            reasoning_status = "BUILDING"

    return {
        "state": state,
        "label": hypothesis.get("id"),
        "confidence_pct": confidence_pct,
        "supporting_count": n_sup,
        "contradicting_count": n_con,
        "note": (
            "Positioning inference from evidence only. "
            "Participant identity remains UNKNOWN (RQ-PO-IDENT)."
        ),
        "reasoning_status": reasoning_status,
    }


def build_desk_reasoning(
    *,
    oi_side: str,
    direction: str = "",
    strike: Any = None,
    decision: str = "",
    dimensions: Optional[Dict[str, Any]] = None,
    extra_evidence: Optional[Sequence[Dict[str, Any]]] = None,
    engine: str = "legacy_v1",
) -> Dict[str, Any]:
    """Build Desk Reasoning Ladder v1.0. Pure observability — no policy fields as inputs."""
    hyp = positioning_hypothesis_for_side(oi_side)
    evidence = _evidence_from_dimensions(dimensions)
    evidence = _merge_extra_evidence(evidence, extra_evidence)
    inference = _infer_from_evidence(hypothesis=hyp, evidence=evidence)
    reasoning_status = str(inference.pop("reasoning_status"))

    return {
        "version": DESK_REASONING_VERSION,
        "engine": engine,
        "oi_fact": {
            "side": str(oi_side or "").upper() or None,
            "direction": str(direction or "") or None,
            "strike": _as_int(strike) if strike is not None else None,
            "known": "new_contracts",
            "unknown": "participant_identity",
        },
        "unknown": {
            "participant_identity": True,
            "intent": True,
        },
        "positioning_hypothesis": hyp,
        "evidence_evaluation": {
            "supporting": evidence["supporting"],
            "contradicting": evidence["contradicting"],
            "supporting_count": len(evidence["supporting"]),
            "contradicting_count": len(evidence["contradicting"]),
        },
        "positioning_inference": inference,
        "reasoning_status": reasoning_status,
        "outcome": "OBSERVATION_COMPLETE",
        "policy_note": (
            "Ladder explains formation only. Candidate/paper eligibility remain "
            "desk confluence + conviction policy — not derived from this ladder."
        ),
        "decision_context": str(decision or "") or None,
    }


def build_desk_reasoning_from_candidate(
    candidate: Dict[str, Any],
    *,
    engine: str = "legacy_v1",
    extra_evidence: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    alert = candidate.get("source_alert") or {}
    return build_desk_reasoning(
        oi_side=str(candidate.get("writer_side") or alert.get("option_type") or ""),
        direction=str(alert.get("direction") or ""),
        strike=candidate.get("strike"),
        decision=str(candidate.get("decision") or ""),
        dimensions=candidate.get("dimensions"),
        extra_evidence=extra_evidence,
        engine=engine,
    )


def assert_no_identity_labels(reasoning: Dict[str, Any]) -> None:
    """Raise if hypothesis/inference claim participant identity."""
    hyp = reasoning.get("positioning_hypothesis") or {}
    inf = reasoning.get("positioning_inference") or {}
    hyp_id = str(hyp.get("id") or "")
    if hyp_id and hyp_id not in ALLOWED_HYPOTHESIS_IDS:
        raise AssertionError(f"unexpected hypothesis id: {hyp_id}")
    inf_label = str(inf.get("label") or "")
    if inf_label and inf_label not in ALLOWED_HYPOTHESIS_IDS:
        raise AssertionError(f"unexpected inference label: {inf_label}")
    blob = " ".join(
        str(x).upper()
        for x in (hyp.get("id"), hyp.get("label"), inf.get("label"), reasoning.get("outcome"))
    )
    for token in ("LIKELY_WRITER", "LIKELY_BUYER", "HEDGER"):
        if token in blob:
            raise AssertionError(f"identity label leaked: {token}")


def ladder_summary_line(reasoning: Optional[Dict[str, Any]]) -> str:
    if not reasoning:
        return "—"
    oi = (reasoning.get("oi_fact") or {}).get("side") or "?"
    hyp = (reasoning.get("positioning_hypothesis") or {}).get("id") or "?"
    status = reasoning.get("reasoning_status") or "?"
    outcome = reasoning.get("outcome") or "?"
    return f"OI {oi} → UNKNOWN → {hyp} → EVIDENCE → {status} → {outcome}"


def _selftest() -> None:
    ce_hyp = positioning_hypothesis_for_side("CE")
    assert ce_hyp["id"] == "CALL_SIDE_POSITIONING"
    assert ce_hyp["execution_policy_if_confirmed"] == "BUY_PE"
    unknown_hyp = positioning_hypothesis_for_side("")
    assert unknown_hyp["id"] == "UNKNOWN_POSITIONING"

    # Strong supporting evidence -> SUPPORTED.
    dims_strong = {
        "oi_velocity": {"pass": True, "score": 10, "max": 10},
        "options_surface": {"pass": True, "score": 8, "max": 10},
        "liquidity_align": {"pass": True, "score": 5, "max": 5},
        "spot_confirm": {"pass": True, "score": 7, "max": 10},
        "market_profile": {"pass": False, "score": 0, "max": 10, "detail": "outside value"},
    }
    reasoning = build_desk_reasoning(oi_side="PE", direction="OI ADDING", strike=23000, decision="BUY_CE", dimensions=dims_strong)
    assert reasoning["positioning_hypothesis"]["id"] == "PUT_SIDE_POSITIONING"
    assert reasoning["evidence_evaluation"]["supporting_count"] == 4
    assert reasoning["evidence_evaluation"]["contradicting_count"] == 1
    assert reasoning["positioning_inference"]["state"] == "SUPPORTED"
    assert reasoning["reasoning_status"] == "SUPPORTED"
    assert reasoning["decision_context"] == "BUY_CE"
    assert_no_identity_labels(reasoning)  # must not raise

    # All-contradicting evidence -> REJECTED/CONTRADICTED.
    dims_weak = {
        "oi_velocity": {"pass": False, "score": 0, "max": 10},
        "options_surface": {"pass": False, "score": 0, "max": 10},
    }
    rejected = build_desk_reasoning(oi_side="CE", dimensions=dims_weak)
    assert rejected["positioning_inference"]["state"] == "REJECTED"
    assert rejected["reasoning_status"] == "CONTRADICTED"

    # No dimensions at all -> INCOMPLETE.
    incomplete = build_desk_reasoning(oi_side="CE", dimensions=None)
    assert incomplete["positioning_inference"]["state"] == "INCOMPLETE"
    assert incomplete["reasoning_status"] == "INCOMPLETE"

    # Unknown OI side -> UNKNOWN regardless of evidence.
    unknown = build_desk_reasoning(oi_side="", dimensions=dims_strong)
    assert unknown["reasoning_status"] == "UNKNOWN"

    # extra_evidence merges alongside dimension-derived evidence.
    with_extra = build_desk_reasoning(
        oi_side="PE", dimensions=dims_strong,
        extra_evidence=[{"id": "velocity_ctx", "pass": True, "detail": "sustained add"}],
    )
    assert with_extra["evidence_evaluation"]["supporting_count"] == 5

    # from_candidate wrapper pulls oi_side/strike/decision/dimensions off a candidate dict.
    candidate = {
        "writer_side": "PE", "strike": 23100, "decision": "BUY_CE",
        "dimensions": dims_strong, "source_alert": {"direction": "OI ADDING"},
    }
    from_cand = build_desk_reasoning_from_candidate(candidate)
    assert from_cand["oi_fact"]["strike"] == 23100
    assert from_cand["oi_fact"]["side"] == "PE"

    # The identity-leak guard must actually catch a bad payload.
    bad = dict(reasoning)
    bad["positioning_hypothesis"] = {**bad["positioning_hypothesis"], "id": "LIKELY_WRITER"}
    try:
        assert_no_identity_labels(bad)
        raise AssertionError("expected identity-leak detection to raise")
    except AssertionError as exc:
        assert "unexpected hypothesis id" in str(exc)

    line = ladder_summary_line(reasoning)
    assert "PE" in line and "PUT_SIDE_POSITIONING" in line and "SUPPORTED" in line
    assert ladder_summary_line(None) == "—"

    print("[analytics.reasoning_ladder] selftest OK: positioning hypothesis, evidence inference, identity guard")


if __name__ == "__main__":
    _selftest()
