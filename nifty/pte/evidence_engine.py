#!/usr/bin/env python3
"""Evidence Engine: builds immutable EvidenceRecords and EvidenceSets.

Ported faithfully from quant-desk-engine v4/ATLAS's evidence_engine.py
(mentor-authored). No logic changed. Only adaptation: imports
ArtifactIdGenerator/PREFIX_EVD/PREFIX_EVS from nifty.pte.artifact_ids
instead of the standalone atlas_artifact_ids module.

Per evidence-engine/EVIDENCE_ENGINE_FREEZE_v1.0.md and
EVIDENCE_SET_SCHEMA.md (mentor's design docs, read in full before this
port): the Evidence Engine's only job is "what evidence exists?" — it
never reasons about participant behaviour (that's
nifty.pte.theory_engine's job, next up the chain). Evidence records are
immutable and deduplicated by (observatory_state_id, field_path,
material_key-or-observation) fingerprint — repeated identical readings
reuse the same EVD-* id rather than growing the ledger; corrections
always mint a new id with a supersedes ref, never an in-place edit.

Not yet wired into the live pipeline.
Self-check: python -m nifty.pte.evidence_engine
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from nifty.pte.artifact_ids import ArtifactIdGenerator, PREFIX_EVD, PREFIX_EVS

QUALITY_ALLOWED = {"observed", "inferred", "indicative"}
POLARITY_ALLOWED = {"neutral", "supportive", "contradictory", "mixed"}
WEIGHT_HINT_ALLOWED = {"weak", "moderate", "strong"}


def ist_now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _norm_list(values: Optional[Iterable[str]]) -> List[str]:
    return [str(v) for v in (values or []) if str(v)]


def _fingerprint(candidate: Dict[str, Any]) -> Tuple[str, str, str]:
    """Identity for reuse. Exclude wall-clock effective_at — it always changes live."""
    source = candidate.get("source") or {}
    # Prefer material_key when present so continuous tick noise can be bucketed
    # without losing human-readable observation text on the record.
    identity = candidate.get("material_key")
    if identity is None or str(identity) == "":
        identity = candidate.get("observation")
    return (
        str(source.get("observatory_state_id") or ""),
        str(source.get("field_path") or ""),
        str(identity or ""),
    )


@dataclass(frozen=True)
class EvidenceRecord:
    id: str
    label: str
    source: Dict[str, Any]
    observation: Any
    interpretation: str | None
    interpretation_state_id: str | None
    quality: str
    polarity: str = "neutral"
    weight_hint: str | None = None
    theory_hints: Dict[str, List[str]] = field(default_factory=dict)
    refs: Dict[str, Any] = field(default_factory=dict)
    supersedes: str | None = None
    effective_at: str | None = None
    observed_at: str | None = None
    published_at: str = field(default_factory=ist_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EvidenceSet:
    id: str
    timestamp: str
    evidence_engine_version: str
    scope: Dict[str, Any]
    upstream_refs: Dict[str, List[str]]
    evidence_record_ids: List[str]
    meta: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class EvidenceEngine:
    """Append-only evidence builder with reuse + immutability semantics."""

    def __init__(
        self,
        *,
        version: str = "1.0",
        id_generator: ArtifactIdGenerator | None = None,
    ) -> None:
        self.version = version
        self.id_gen = id_generator or ArtifactIdGenerator()
        self._fingerprint_to_evd: Dict[Tuple[str, str, str], str] = {}
        self._records_by_id: Dict[str, Dict[str, Any]] = {}

    def _build_record(
        self,
        candidate: Dict[str, Any],
        *,
        session_day: date,
        timestamp: str,
    ) -> EvidenceRecord:
        source = candidate.get("source") or {}
        quality = str(candidate.get("quality") or "observed")
        if quality not in QUALITY_ALLOWED:
            raise ValueError(f"Unsupported quality: {quality}")
        polarity = str(candidate.get("polarity") or "neutral")
        if polarity not in POLARITY_ALLOWED:
            raise ValueError(f"Unsupported polarity: {polarity}")
        weight_hint = candidate.get("weight_hint")
        if weight_hint is not None and str(weight_hint) not in WEIGHT_HINT_ALLOWED:
            raise ValueError(f"Unsupported weight_hint: {weight_hint}")
        return EvidenceRecord(
            id=self.id_gen.new(PREFIX_EVD, session_day=session_day),
            label=str(candidate.get("label") or ""),
            source={
                "observatory_id": str(source.get("observatory_id") or ""),
                "observatory_state_id": str(source.get("observatory_state_id") or ""),
                "field_path": str(source.get("field_path") or ""),
            },
            observation=candidate.get("observation"),
            interpretation=candidate.get("interpretation"),
            interpretation_state_id=candidate.get("interpretation_state_id"),
            quality=quality,
            polarity=polarity,
            weight_hint=weight_hint,
            theory_hints={
                "supports": _norm_list((candidate.get("theory_hints") or {}).get("supports")),
                "contradicts": _norm_list((candidate.get("theory_hints") or {}).get("contradicts")),
            },
            refs=dict(candidate.get("refs") or {}),
            supersedes=candidate.get("supersedes"),
            effective_at=candidate.get("effective_at"),
            observed_at=candidate.get("observed_at"),
            published_at=timestamp,
        )

    def publish(
        self,
        *,
        scope: Dict[str, Any],
        observatory_state_ids: Iterable[str],
        interpretation_state_ids: Iterable[str],
        evidence_candidates: Iterable[Dict[str, Any]],
        publish_reason: str = "material_change",
        session_day: date | None = None,
        timestamp: str | None = None,
    ) -> Tuple[EvidenceSet, List[EvidenceRecord]]:
        """Publish one EvidenceSet and return any newly created EvidenceRecords."""
        day = session_day or date.today()
        ts = timestamp or ist_now_iso()
        new_records: List[EvidenceRecord] = []
        evidence_record_ids: List[str] = []

        for raw in evidence_candidates:
            candidate = dict(raw)
            supersedes = str(candidate.get("supersedes") or "")
            if supersedes:
                # Corrections always create a new immutable record.
                record = self._build_record(candidate, session_day=day, timestamp=ts)
                new_records.append(record)
                evidence_record_ids.append(record.id)
                continue

            fp = _fingerprint(candidate)
            existing = self._fingerprint_to_evd.get(fp)
            if existing:
                evidence_record_ids.append(existing)
                continue

            record = self._build_record(candidate, session_day=day, timestamp=ts)
            self._fingerprint_to_evd[fp] = record.id
            self._records_by_id[record.id] = record.to_dict()
            new_records.append(record)
            evidence_record_ids.append(record.id)

        evidence_set = EvidenceSet(
            id=self.id_gen.new(PREFIX_EVS, session_day=day),
            timestamp=ts,
            evidence_engine_version=self.version,
            scope=dict(scope),
            upstream_refs={
                "observatory_state_ids": _norm_list(observatory_state_ids),
                "interpretation_state_ids": _norm_list(interpretation_state_ids),
            },
            evidence_record_ids=evidence_record_ids,
            meta={
                "publish_reason": publish_reason,
                "evidence_count": len(evidence_record_ids),
                "completeness": str(scope.get("completeness") or "full"),
            },
        )
        return evidence_set, new_records

    def records_for_ids(self, evidence_record_ids: Iterable[str]) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for evd_id in evidence_record_ids:
            row = self._records_by_id.get(str(evd_id))
            if row:
                rows.append(dict(row))
        return rows

    @staticmethod
    def append_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, default=str, separators=(",", ":")) + "\n")


def _selftest() -> None:
    import tempfile

    engine = EvidenceEngine()
    day = date(2026, 7, 23)
    ts = "2026-07-23T10:30:00+05:30"

    candidates = [
        {
            "label": "GEX regime",
            "source": {"observatory_id": "chain", "observatory_state_id": "OBS-1", "field_path": "options_analytics.gex_regime"},
            "observation": "GEX regime=NEGATIVE_GAMMA",
            "interpretation": "Signed gamma environment",
            "quality": "inferred",
            "polarity": "neutral",
            "weight_hint": "strong",
            "theory_hints": {"supports": ["breakout_expansion"], "contradicts": []},
            "effective_at": ts,
            "observed_at": ts,
        },
    ]
    evidence_set, new_records = engine.publish(
        scope={"session_date": "2026-07-23", "instrument": "NIFTY"},
        observatory_state_ids=["OBS-1"],
        interpretation_state_ids=["INT-1"],
        evidence_candidates=candidates,
        session_day=day,
        timestamp=ts,
    )
    assert evidence_set.id == "EVS-20260723-000001"
    assert len(new_records) == 1
    assert new_records[0].id == "EVD-20260723-000001"
    assert evidence_set.evidence_record_ids == ["EVD-20260723-000001"]
    assert evidence_set.meta["evidence_count"] == 1

    # Same candidate published again reuses the existing EVD id (dedup by fingerprint).
    evidence_set2, new_records2 = engine.publish(
        scope={"session_date": "2026-07-23", "instrument": "NIFTY"},
        observatory_state_ids=["OBS-2"],
        interpretation_state_ids=["INT-2"],
        evidence_candidates=candidates,
        session_day=day,
        timestamp=ts,
    )
    assert len(new_records2) == 0  # no new record — reused via fingerprint
    assert evidence_set2.evidence_record_ids == ["EVD-20260723-000001"]
    assert evidence_set2.id == "EVS-20260723-000002"  # but the set itself is new

    # A correction (supersedes set) always mints a new immutable record.
    correction = dict(candidates[0])
    correction["supersedes"] = "EVD-20260723-000001"
    correction["observation"] = "GEX regime=POSITIVE_GAMMA (corrected)"
    _, corrected_records = engine.publish(
        scope={"session_date": "2026-07-23", "instrument": "NIFTY"},
        observatory_state_ids=["OBS-3"],
        interpretation_state_ids=["INT-3"],
        evidence_candidates=[correction],
        session_day=day,
        timestamp=ts,
    )
    assert len(corrected_records) == 1
    assert corrected_records[0].id == "EVD-20260723-000002"
    assert corrected_records[0].supersedes == "EVD-20260723-000001"

    rows = engine.records_for_ids(["EVD-20260723-000001", "does-not-exist"])
    assert len(rows) == 1 and rows[0]["label"] == "GEX regime"

    try:
        engine.publish(
            scope={}, observatory_state_ids=[], interpretation_state_ids=[],
            evidence_candidates=[{"source": {}, "quality": "bogus"}],
            session_day=day, timestamp=ts,
        )
        raise AssertionError("expected ValueError for unsupported quality")
    except ValueError:
        pass

    tmp = Path(tempfile.mkdtemp(prefix="evidence-engine-selftest-")) / "evd.jsonl"
    EvidenceEngine.append_jsonl(tmp, [new_records[0].to_dict()])
    assert tmp.exists()
    assert "EVD-20260723-000001" in tmp.read_text(encoding="utf-8")

    print("[pte.evidence_engine] selftest OK: publish/dedup/correction/append_jsonl")


if __name__ == "__main__":
    _selftest()
