#!/usr/bin/env python3
"""Universal artifact IDs for ATLAS frozen architecture.

Ported faithfully from quant-desk-engine v4/ATLAS's atlas_artifact_ids.py
(mentor-authored). No logic changed. Foundational, dependency-free module:
implements the ID scheme frozen in ATLAS_ARTIFACT_ID_REGISTRY.md
({PREFIX}-{YYYYMMDD}-{SERIAL}, monotonic per prefix per IST session day).

This is the base of a new architectural layer (Evidence Engine ->
Participant Theory Engine -> Evaluation Input adapter) that nifty-dashboard
does not have yet — see nifty/pte/evidence_engine.py and
nifty/pte/theory_engine.py for the next pieces up the chain.

Not yet wired into the live pipeline.
Self-check: python -m nifty.pte.artifact_ids
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import re
from typing import Dict, Tuple

PREFIX_OBS = "OBS"
PREFIX_INT = "INT"
PREFIX_EVD = "EVD"
PREFIX_EVS = "EVS"
PREFIX_PTH = "PTH"
PREFIX_EVI = "EVI"
PREFIX_EVA = "EVA"
PREFIX_DEC = "DEC"
PREFIX_MEM = "MEM"
PREFIX_OBSN = "OBSN"
PREFIX_FEAT = "FEAT"
PREFIX_MNA = "MNA"
PREFIX_META = "META"
PREFIX_DISC = "DISC"
PREFIX_RESQ = "RESQ"

VALID_PREFIXES = {
    PREFIX_OBS,
    PREFIX_INT,
    PREFIX_EVD,
    PREFIX_EVS,
    PREFIX_PTH,
    PREFIX_EVI,
    PREFIX_EVA,
    PREFIX_DEC,
    PREFIX_MEM,
    PREFIX_OBSN,
    PREFIX_FEAT,
    PREFIX_MNA,
    PREFIX_META,
    PREFIX_DISC,
    PREFIX_RESQ,
}

_ID_PATTERN = re.compile(r"^[A-Z]{3,4}-\d{8}-\d{6}$")


def _session_key(session_day: date) -> str:
    return session_day.strftime("%Y%m%d")


def is_valid_artifact_id(value: str) -> bool:
    """Return True when an ID matches frozen ATLAS format."""
    if not value or not _ID_PATTERN.match(value):
        return False
    prefix = value.split("-", 1)[0]
    return prefix in VALID_PREFIXES


@dataclass
class ArtifactIdGenerator:
    """In-memory monotonic ID generator per prefix and session day."""

    _counters: Dict[Tuple[str, str], int] = field(default_factory=dict)

    def new(self, prefix: str, *, session_day: date | None = None) -> str:
        p = (prefix or "").upper()
        if p not in VALID_PREFIXES:
            raise ValueError(f"Unsupported prefix: {prefix}")
        day = session_day or date.today()
        day_key = _session_key(day)
        counter_key = (p, day_key)
        current = self._counters.get(counter_key, 0) + 1
        self._counters[counter_key] = current
        return f"{p}-{day_key}-{current:06d}"


def _selftest() -> None:
    gen = ArtifactIdGenerator()
    first = gen.new(PREFIX_EVD, session_day=date(2026, 7, 23))
    second = gen.new(PREFIX_EVD, session_day=date(2026, 7, 23))
    assert first == "EVD-20260723-000001"
    assert second == "EVD-20260723-000002"

    other_prefix = gen.new(PREFIX_PTH, session_day=date(2026, 7, 23))
    assert other_prefix == "PTH-20260723-000001"  # counters are per-prefix

    other_day = gen.new(PREFIX_EVD, session_day=date(2026, 7, 24))
    assert other_day == "EVD-20260724-000001"  # counters are per-session-day too

    assert is_valid_artifact_id("EVD-20260723-000001") is True
    assert is_valid_artifact_id("evd-20260723-000001") is False  # must be uppercase
    assert is_valid_artifact_id("XYZ-20260723-000001") is False  # unregistered prefix
    assert is_valid_artifact_id("EVD-2026723-1") is False  # wrong digit widths
    assert is_valid_artifact_id("") is False

    try:
        gen.new("NOPE")
        raise AssertionError("expected ValueError for unsupported prefix")
    except ValueError:
        pass

    print("[pte.artifact_ids] selftest OK: monotonic per-prefix-per-day IDs, format validation")


if __name__ == "__main__":
    _selftest()
