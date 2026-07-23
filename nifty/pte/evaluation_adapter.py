#!/usr/bin/env python3
"""Evaluation adapter firewall: ParticipantTheorySet (PTH) -> EvaluationInput (EVI).

Ported faithfully from quant-desk-engine v4/ATLAS's evaluation_input_adapter.py
(mentor-authored). No logic changed. Only adaptation: imports
ArtifactIdGenerator/PREFIX_EVI from nifty.pte.artifact_ids.

Per evaluation-layer/EVALUATION_INPUT_SCHEMA.md (mentor's design doc, read
in full before this port): PTE answers "what explanations fit the
evidence?"; evaluation engines answer "is capital deployment justified?" —
those questions must never mix, so EvaluationInput is the only object an
evaluation engine may consume from the theory layer. The adapter selects
the top-N theories by share, carries only EVD-*/PTH-* references (never
duplicates evidence payloads or raw observation text), and explicitly
redacts history[] and invalidation_conditions[] detail — an evaluation
engine reads share/strength/state, not the full falsification reasoning.
The adapter must never recompute theory strength or set paper_eligible;
that stays out of scope here by construction (no such method exists on
this class).

Not yet wired into the live pipeline — there is no evaluation engine
downstream of this yet in nifty-dashboard.
Self-check: python -m nifty.pte.evaluation_adapter
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from nifty.pte.artifact_ids import ArtifactIdGenerator, PREFIX_EVI


def ist_now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class EvaluationInput:
    id: str
    timestamp: str
    adapter_version: str
    scope: Dict[str, Any]
    source_refs: Dict[str, Any]
    theory_summary: List[Dict[str, Any]]
    evidence_refs: List[str]
    interpretation_state_ids: List[str]
    meta: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class EvaluationInputAdapter:
    """Builds filtered evaluation inputs from participant theory sets."""

    def __init__(
        self,
        *,
        version: str = "1.0",
        id_generator: ArtifactIdGenerator | None = None,
    ) -> None:
        self.version = version
        self.id_gen = id_generator or ArtifactIdGenerator()

    def build(
        self,
        *,
        participant_theory_set: Dict[str, Any],
        evidence_set_id: str,
        top_n: int = 5,
        adapter_policy: str = "shadow_v1",
        interpretation_state_ids: Optional[List[str]] = None,
        timestamp: Optional[str] = None,
    ) -> EvaluationInput:
        ts = timestamp or ist_now_iso()
        scope = dict(participant_theory_set.get("scope") or {})
        day = date.today()
        if scope.get("session_date"):
            day = date.fromisoformat(str(scope["session_date"]))

        theories = list(participant_theory_set.get("theories") or [])
        theories.sort(key=lambda t: int(t.get("share") or 0), reverse=True)
        chosen = theories[: max(1, min(top_n, len(theories) or 1))]

        theory_summary: List[Dict[str, Any]] = []
        ev_refs: set[str] = set()
        for t in chosen:
            sup = [str(x) for x in (t.get("supporting_evidence_ids") or []) if str(x)]
            con = [str(x) for x in (t.get("contradicting_evidence_ids") or []) if str(x)]
            ev_refs.update(sup)
            ev_refs.update(con)
            theory_summary.append(
                {
                    "catalog_id": str(t.get("catalog_id") or ""),
                    "name": str(t.get("name") or ""),
                    "strength": int(t.get("strength") or 0),
                    "share": int(t.get("share") or 0),
                    "state": str(t.get("state") or ""),
                    "supporting_evidence_ids": sup,
                    "contradicting_evidence_ids": con,
                }
            )

        return EvaluationInput(
            id=self.id_gen.new(PREFIX_EVI, session_day=day),
            timestamp=ts,
            adapter_version=self.version,
            scope=scope,
            source_refs={
                "participant_theory_set_id": str(participant_theory_set.get("id") or ""),
                "pte_version": str(participant_theory_set.get("pte_version") or ""),
                "evidence_set_id": evidence_set_id,
            },
            theory_summary=theory_summary,
            evidence_refs=sorted(ev_refs),
            interpretation_state_ids=[str(x) for x in (interpretation_state_ids or []) if str(x)],
            meta={
                "adapter_policy": adapter_policy,
                "fields_redacted": ["history", "invalidation_conditions", "observation"],
            },
        )


def _selftest() -> None:
    from nifty.pte.theory_engine import ParticipantTheoryEngine

    engine = ParticipantTheoryEngine()
    pth = engine.infer(
        evidence_set_id="EVS-20260723-000001",
        evidence_engine_version="1.0",
        evidence_records=[
            {"id": "EVD-1", "weight_hint": "strong", "theory_hints": {"supports": ["dealer_hedging"], "contradicts": []}},
            {"id": "EVD-2", "weight_hint": "moderate", "theory_hints": {"supports": ["premium_writing"], "contradicts": []}},
        ],
        scope={"session_date": "2026-07-23", "instrument": "NIFTY"},
        timestamp="2026-07-23T10:30:00+05:30",
    )

    adapter = EvaluationInputAdapter()
    evi = adapter.build(
        participant_theory_set=pth.to_dict(),
        evidence_set_id="EVS-20260723-000001",
        top_n=2,
        timestamp="2026-07-23T10:31:00+05:30",
    )
    assert evi.id == "EVI-20260723-000001"
    assert evi.adapter_version == "1.0"
    assert len(evi.theory_summary) == 2  # top_n respected
    assert evi.theory_summary[0]["share"] >= evi.theory_summary[1]["share"]  # sorted by share desc
    assert evi.source_refs["participant_theory_set_id"] == pth.id
    assert evi.source_refs["evidence_set_id"] == "EVS-20260723-000001"
    assert "EVD-1" in evi.evidence_refs or "EVD-2" in evi.evidence_refs

    # Redaction contract: raw history/invalidation detail must never leak through.
    assert "fields_redacted" in evi.meta
    assert set(evi.meta["fields_redacted"]) == {"history", "invalidation_conditions", "observation"}
    for row in evi.theory_summary:
        assert "history" not in row
        assert "invalidation_conditions" not in row

    # top_n larger than available theories clamps to what exists, no crash.
    evi_all = adapter.build(
        participant_theory_set=pth.to_dict(),
        evidence_set_id="EVS-20260723-000001",
        top_n=99,
        timestamp="2026-07-23T10:32:00+05:30",
    )
    assert len(evi_all.theory_summary) == len(pth.theories)

    print("[pte.evaluation_adapter] selftest OK: PTH->EVI firewall, top-N selection, field redaction")


if __name__ == "__main__":
    _selftest()
