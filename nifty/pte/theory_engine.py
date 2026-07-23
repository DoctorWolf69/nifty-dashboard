#!/usr/bin/env python3
"""Participant Theory Engine (PTE v1): evidence-only scientific inference.

Ported faithfully from quant-desk-engine v4/ATLAS's participant_theory_engine.py
(mentor-authored, PTE v1 — the simpler, live-wired version; the domain-agnostic
"v2" re-architecture in participant_theory_engine_v2.py was deliberately left
unported: the mentor's own v4 survey shows v2 isn't imported/live-wired
anywhere even in the source repo, and its extra purity (plugin architecture,
its own architecture_tests/invariants.py runtime enforcement) is more
engineering than a NIFTY-only dashboard needs. v1 is the actual running
system this ports from). No logic changed. Only adaptation: imports
ArtifactIdGenerator/PREFIX_PTH from nifty.pte.artifact_ids.

Per participant-theory-engine/PTE_CONSTITUTION.md and PTE_FREEZE_v1.0.md
(mentor's design docs, read in full before this port): PTE answers exactly
one question — "given the current evidence, what participant behaviours
best explain it?" It consumes EvidenceSet/EvidenceRecord only (never raw
market state), maintains 3-5 competing theories from an 8-entry catalog
(premium_writing, premium_buying, dealer_hedging, short_covering,
long_unwinding, inventory_transfer, range_compression, breakout_expansion),
and never scores tradability, EV, or generates trades — understanding is
complete at ParticipantTheorySet. "Strength" (0-100, absolute evidence
support) and "share" (0-100, relative weight vs active competitors, sums to
100) answer different questions and both are frozen as required fields.
Theory lifecycle (CREATED -> OBSERVING -> STRENGTHENING <-> WEAKENING ->
RESOLVED | REJECTED) is a separate namespace from nifty-dashboard's existing
"Active Trade Thesis" lifecycle — the two must never be conflated per the
freeze doc's own explicit note.

Not yet wired into the live pipeline.
Self-check: python -m nifty.pte.theory_engine
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional

from nifty.pte.artifact_ids import ArtifactIdGenerator, PREFIX_PTH

STATE_CREATED = "CREATED"
STATE_OBSERVING = "OBSERVING"
STATE_STRENGTHENING = "STRENGTHENING"
STATE_WEAKENING = "WEAKENING"
STATE_RESOLVED = "RESOLVED"
STATE_REJECTED = "REJECTED"

# Keep a short in-memory ring of transitions. The dated JSONL journal is the
# durable timeline; embedding unbounded history[] in every line caused quadratic disk growth.
HISTORY_MAX_LEN = 24


def _id_set(values: Optional[Iterable[str]]) -> set[str]:
    return {str(v) for v in (values or []) if str(v)}


def _evidence_delta(
    *,
    supporting: Iterable[str],
    contradicting: Iterable[str],
    prev: Optional[Dict[str, Any]],
) -> Dict[str, List[str]]:
    cur_sup = _id_set(supporting)
    cur_con = _id_set(contradicting)
    if not prev:
        return {
            "added_supporting": sorted(cur_sup),
            "added_contradicting": sorted(cur_con),
            "removed_supporting": [],
            "removed_contradicting": [],
        }
    prev_sup = _id_set(prev.get("supporting_evidence_ids"))
    prev_con = _id_set(prev.get("contradicting_evidence_ids"))
    return {
        "added_supporting": sorted(cur_sup - prev_sup),
        "added_contradicting": sorted(cur_con - prev_con),
        "removed_supporting": sorted(prev_sup - cur_sup),
        "removed_contradicting": sorted(prev_con - cur_con),
    }


def slim_participant_theory_set_for_journal(
    participant_theory_set: Dict[str, Any],
    *,
    history_keep: int = 0,
) -> Dict[str, Any]:
    """Copy PTH for JSONL: drop nested history (journal lines are the timeline)."""
    slim = dict(participant_theory_set)
    theories: List[Dict[str, Any]] = []
    for row in participant_theory_set.get("theories") or []:
        t = dict(row)
        hist = list(t.get("history") or [])
        if history_keep <= 0:
            t["history"] = []
        else:
            t["history"] = hist[-history_keep:]
        theories.append(t)
    slim["theories"] = theories
    return slim

DEFAULT_THEORY_CATALOG: Dict[str, Dict[str, Any]] = {
    "premium_writing": {
        "name": "Premium Writing",
        "description": "Net option premium selling behaviour.",
        "invalidation_conditions": [
            "Premium expands while OI continues rising.",
            "OI collapses without unwind profile.",
        ],
    },
    "premium_buying": {
        "name": "Premium Buying",
        "description": "Net option premium buying behaviour.",
        "invalidation_conditions": [
            "Premium decays while OI build persists.",
            "Volume participation fades materially.",
        ],
    },
    "dealer_hedging": {
        "name": "Dealer Hedging",
        "description": "Flow driven primarily by dealer hedge mechanics.",
        "invalidation_conditions": [
            "Dealer delta/spot coupling breaks.",
            "Gamma regime flips opposite for sustained interval.",
        ],
    },
    "short_covering": {
        "name": "Short Covering",
        "description": "Short exposure closeout dominates intraday flow.",
        "invalidation_conditions": [
            "Fresh same-side OI expansion resumes.",
            "Cover signature reverses into new build.",
        ],
    },
    "long_unwinding": {
        "name": "Long Unwinding",
        "description": "Long exposure exit dominates intraday flow.",
        "invalidation_conditions": [
            "Fresh long build appears at same zone.",
            "Premium re-expands with sustained continuation.",
        ],
    },
    "inventory_transfer": {
        "name": "Inventory Transfer / Repositioning",
        "description": "Roll/rebalance behaviour dominates one-direction intent.",
        "invalidation_conditions": [
            "One-sided directional dominance emerges.",
            "Transfer pattern resolves into trend continuation.",
        ],
    },
    "range_compression": {
        "name": "Range Compression",
        "description": "Flow implies reduced movement and pin-like structure.",
        "invalidation_conditions": [
            "Volatility expansion with range escape.",
            "Acceptance outside value persists.",
        ],
    },
    "breakout_expansion": {
        "name": "Breakout Expansion",
        "description": "Flow implies range expansion and displacement.",
        "invalidation_conditions": [
            "Price returns and accepts inside prior value.",
            "IV crush follows failed break behaviour.",
        ],
    },
}

_WEIGHT_SCORE = {"weak": 1, "moderate": 2, "strong": 3}


def ist_now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class ParticipantTheory:
    theory_id: str
    catalog_id: str
    name: str
    description: str
    strength: int
    share: int
    state: str
    supporting_evidence_ids: List[str] = field(default_factory=list)
    contradicting_evidence_ids: List[str] = field(default_factory=list)
    invalidation_conditions: List[Dict[str, Any]] = field(default_factory=list)
    competing_theory_ids: List[str] = field(default_factory=list)
    history: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ParticipantTheorySet:
    id: str
    timestamp: str
    pte_version: str
    scope: Dict[str, Any]
    evidence_set_ref: Dict[str, Any]
    theories: List[Dict[str, Any]]
    set_state: str
    resolution: Dict[str, Any]
    meta: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _strength_from_evidence(
    catalog_id: str,
    evidence_records: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    support_ids: List[str] = []
    contradict_ids: List[str] = []
    support_score = 0
    contradict_score = 0
    for ev in evidence_records:
        ev_id = str(ev.get("id") or "")
        hint = ev.get("theory_hints") or {}
        weight_hint = str(ev.get("weight_hint") or "moderate")
        weight = _WEIGHT_SCORE.get(weight_hint, 2)
        supports = {str(x) for x in (hint.get("supports") or [])}
        contradicts = {str(x) for x in (hint.get("contradicts") or [])}
        if catalog_id in supports:
            support_ids.append(ev_id)
            support_score += weight
        if catalog_id in contradicts:
            contradict_ids.append(ev_id)
            contradict_score += weight
    strength = max(0, min(100, 50 + (support_score * 8) - (contradict_score * 10)))
    return {
        "strength": int(strength),
        "supporting_evidence_ids": support_ids,
        "contradicting_evidence_ids": contradict_ids,
    }


class ParticipantTheoryEngine:
    """Evidence-only inference engine with independent theory strengths."""

    def __init__(
        self,
        *,
        version: str = "1.0",
        id_generator: ArtifactIdGenerator | None = None,
    ) -> None:
        self.version = version
        self.id_gen = id_generator or ArtifactIdGenerator()

    def infer(
        self,
        *,
        evidence_set_id: str,
        evidence_engine_version: str,
        evidence_records: Iterable[Dict[str, Any]],
        scope: Dict[str, Any],
        previous_set: Optional[Dict[str, Any]] = None,
        catalog: Optional[Dict[str, Dict[str, Any]]] = None,
        active_theory_count: int = 5,
        resolution_reason: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> ParticipantTheorySet:
        ts = timestamp or ist_now_iso()
        cat = catalog or DEFAULT_THEORY_CATALOG
        day = date.today()
        if scope.get("session_date"):
            day = date.fromisoformat(str(scope["session_date"]))

        scored: List[Dict[str, Any]] = []
        for catalog_id, config in cat.items():
            row = _strength_from_evidence(catalog_id, evidence_records)
            scored.append(
                {
                    "catalog_id": catalog_id,
                    "name": str(config.get("name") or catalog_id),
                    "description": str(config.get("description") or ""),
                    "invalidation_conditions": list(config.get("invalidation_conditions") or []),
                    **row,
                }
            )
        scored.sort(key=lambda x: x["strength"], reverse=True)
        active = scored[: max(3, min(active_theory_count, 5))]
        total_strength = sum(max(0, r["strength"]) for r in active) or len(active)

        prev_map: Dict[str, Dict[str, Any]] = {}
        if previous_set:
            for t in previous_set.get("theories") or []:
                prev_map[str(t.get("catalog_id") or "")] = dict(t)

        theory_rows: List[Dict[str, Any]] = []
        for row in active:
            share = int(round((max(0, row["strength"]) / total_strength) * 100))
            prev = prev_map.get(row["catalog_id"])
            state = STATE_CREATED
            history_entry = {
                "timestamp": ts,
                "strength": row["strength"],
                "share": share,
                "state": state,
                "evidence_delta": _evidence_delta(
                    supporting=row["supporting_evidence_ids"],
                    contradicting=row["contradicting_evidence_ids"],
                    prev=prev,
                ),
                "invalidation_events": [],
                "pte_version": self.version,
            }
            if prev:
                prev_strength = int(prev.get("strength") or 0)
                delta = row["strength"] - prev_strength
                if delta > 2:
                    state = STATE_STRENGTHENING
                elif delta < -2:
                    state = STATE_WEAKENING
                else:
                    state = STATE_OBSERVING
                history_entry["state"] = state
                history = list(prev.get("history") or []) + [history_entry]
            else:
                history = [history_entry]
            if len(history) > HISTORY_MAX_LEN:
                history = history[-HISTORY_MAX_LEN:]

            invalidation_rows = [
                {
                    "condition_id": f"{row['catalog_id']}:inv:{i + 1}",
                    "description": text,
                    "evidence_pattern": None,
                    "status": "active",
                    "triggered_at": None,
                    "triggered_by_evidence_ids": [],
                }
                for i, text in enumerate(row["invalidation_conditions"])
            ]
            theory_rows.append(
                ParticipantTheory(
                    theory_id=f"{row['catalog_id']}:{ts[:19]}",
                    catalog_id=row["catalog_id"],
                    name=row["name"],
                    description=row["description"],
                    strength=row["strength"],
                    share=share,
                    state=state,
                    supporting_evidence_ids=list(row["supporting_evidence_ids"]),
                    contradicting_evidence_ids=list(row["contradicting_evidence_ids"]),
                    invalidation_conditions=invalidation_rows,
                    competing_theory_ids=[],
                    history=history,
                ).to_dict()
            )

        # force share to sum 100 exactly
        if theory_rows:
            diff = 100 - sum(int(t["share"]) for t in theory_rows)
            theory_rows[0]["share"] += diff

        ids = [str(t["theory_id"]) for t in theory_rows]
        for t in theory_rows:
            t["competing_theory_ids"] = [x for x in ids if x != t["theory_id"]]

        set_state = "ACTIVE"
        resolution = {
            "resolved_at": None,
            "resolution_reason": None,
            "evidence_plateau_minutes": None,
        }
        if resolution_reason:
            set_state = "RESOLVED"
            resolution = {
                "resolved_at": ts,
                "resolution_reason": resolution_reason,
                "evidence_plateau_minutes": 0 if resolution_reason == "session_boundary" else 5,
            }
            for t in theory_rows:
                if t["state"] != STATE_REJECTED:
                    t["state"] = STATE_RESOLVED

        return ParticipantTheorySet(
            id=self.id_gen.new(PREFIX_PTH, session_day=day),
            timestamp=ts,
            pte_version=self.version,
            scope=dict(scope),
            evidence_set_ref={
                "evidence_set_id": evidence_set_id,
                "evidence_engine_version": evidence_engine_version,
            },
            theories=theory_rows,
            set_state=set_state,
            resolution=resolution,
            meta={
                "publish_reason": "evidence_update",
                "active_theory_count": len(theory_rows),
                "dominant_catalog_id": theory_rows[0]["catalog_id"] if theory_rows else None,
                "dominant_share": theory_rows[0]["share"] if theory_rows else None,
            },
        )


def _selftest() -> None:
    engine = ParticipantTheoryEngine()
    day = date(2026, 7, 23)
    ts = "2026-07-23T10:30:00+05:30"

    evidence_records = [
        {"id": "EVD-1", "weight_hint": "strong", "theory_hints": {"supports": ["dealer_hedging", "breakout_expansion"], "contradicts": ["range_compression"]}},
        {"id": "EVD-2", "weight_hint": "moderate", "theory_hints": {"supports": ["dealer_hedging"], "contradicts": []}},
        {"id": "EVD-3", "weight_hint": "weak", "theory_hints": {"supports": ["premium_writing"], "contradicts": ["premium_buying"]}},
    ]

    pth = engine.infer(
        evidence_set_id="EVS-20260723-000001",
        evidence_engine_version="1.0",
        evidence_records=evidence_records,
        scope={"session_date": "2026-07-23", "instrument": "NIFTY"},
        timestamp=ts,
    )
    assert pth.id == "PTH-20260723-000001"
    assert pth.set_state == "ACTIVE"
    assert 3 <= len(pth.theories) <= 5
    shares = [t["share"] for t in pth.theories]
    assert sum(shares) == 100  # share must sum to exactly 100 (frozen rule)

    dealer = next(t for t in pth.theories if t["catalog_id"] == "dealer_hedging")
    assert dealer["strength"] > 50  # two supporting hints, no contradictions -> above baseline
    assert set(dealer["supporting_evidence_ids"]) == {"EVD-1", "EVD-2"}
    assert dealer["state"] == STATE_CREATED  # first observation, no prior set
    assert len(dealer["invalidation_conditions"]) == 2  # every theory must be falsifiable

    range_comp = next((t for t in pth.theories if t["catalog_id"] == "range_compression"), None)
    if range_comp:
        assert range_comp["strength"] < 50  # contradicted -> below baseline

    # Second inference cycle with the same dominant theory gaining more support
    # should transition CREATED -> STRENGTHENING (delta > 2).
    more_records = evidence_records + [
        {"id": "EVD-4", "weight_hint": "strong", "theory_hints": {"supports": ["dealer_hedging"], "contradicts": []}},
    ]
    pth2 = engine.infer(
        evidence_set_id="EVS-20260723-000002",
        evidence_engine_version="1.0",
        evidence_records=more_records,
        scope={"session_date": "2026-07-23", "instrument": "NIFTY"},
        previous_set=pth.to_dict(),
        timestamp="2026-07-23T10:35:00+05:30",
    )
    dealer2 = next(t for t in pth2.theories if t["catalog_id"] == "dealer_hedging")
    assert dealer2["state"] == STATE_STRENGTHENING
    assert len(dealer2["history"]) == 2  # accumulated across cycles

    # competing_theory_ids excludes self and references all other active theories
    for t in pth2.theories:
        assert t["theory_id"] not in t["competing_theory_ids"]
        assert len(t["competing_theory_ids"]) == len(pth2.theories) - 1

    # Session-boundary resolution forces RESOLVED without declaring a winner.
    resolved = engine.infer(
        evidence_set_id="EVS-20260723-000003",
        evidence_engine_version="1.0",
        evidence_records=more_records,
        scope={"session_date": "2026-07-23", "instrument": "NIFTY"},
        previous_set=pth2.to_dict(),
        resolution_reason="session_boundary",
        timestamp="2026-07-23T15:30:00+05:30",
    )
    assert resolved.set_state == "RESOLVED"
    assert all(t["state"] == STATE_RESOLVED for t in resolved.theories)
    assert resolved.resolution["resolution_reason"] == "session_boundary"

    slim = slim_participant_theory_set_for_journal(pth2.to_dict())
    assert all(t["history"] == [] for t in slim["theories"])  # history dropped by default
    slim_keep1 = slim_participant_theory_set_for_journal(pth2.to_dict(), history_keep=1)
    assert all(len(t["history"]) <= 1 for t in slim_keep1["theories"])

    print("[pte.theory_engine] selftest OK: 8-theory catalog, strength/share, lifecycle, resolution")


if __name__ == "__main__":
    _selftest()
