#!/usr/bin/env python3
"""Atlas Evidence Cube v0.2 — observation frame storage and projection feed.

Ported faithfully from quant-desk-engine v4/ATLAS's atlas_evidence_cube.py
(mentor-authored). No logic changed. Fully self-contained in the source
too (stdlib only), so no import adaptation was needed.

NAMING NOTE: despite the shared word "Evidence", this is UNRELATED to
nifty.pte.evidence_engine's EvidenceRecord/EvidenceSet (the Participant
Theory Engine's evidence layer). This "Evidence Cube" is a confluence-score
ladder visualization built directly from journaled SIGNAL_CANDIDATE rows —
a UI/Timeline projection feed, not part of the PTE inference chain. Kept in
nifty/analytics/ rather than nifty/pte/ to avoid conflating the two.

Principle (Evidence Contract, the mentor's own naming): an Observation
Frame is the atomic unit of the Evidence Cube. Everything emitted during
one scoreboard evaluation belongs to the same frame. Downstream consumers
record frames; they never reconstruct or guess frame boundaries.

Market -> Observation Frame -> Evidence Cube -> Projections (Timeline, P1, Follow Strike, ...)

Reads nifty-dashboard's own SIGNAL_CANDIDATE journal rows (already written
by state.py's append_signal_candidate calls). Frame-based grouping requires
an `observation_frame_id` field on those rows and matching OBSERVATION_FRAME
events, neither of which nifty-dashboard produces yet — so this always
takes the mentor's own documented "legacy" fallback path today (one
reconstructed frame per candidate, flagged with a
LEGACY_CANDIDATE_WITHOUT_FRAME_ID integrity warning) rather than the
canonical multi-candidate-per-frame path. That fallback is a real,
non-error code path in the source, not a degradation this port added.

Not yet wired into the live pipeline.
Self-check: python -m nifty.analytics.evidence_cube
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

EVIDENCE_CUBE_VERSION = "0.2"
SOURCE_EVENT_CANDIDATE = "SIGNAL_CANDIDATE"
SOURCE_EVENT_FRAME = "OBSERVATION_FRAME"
SCORE_MIN = 0
SCORE_MAX = 128
# Desk ΔOI / volume window shown in Projection 1 ladder cells (matches confluence velocity_5m).
LADDER_OI_WINDOW = "5m"


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float_opt(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _oi_side_from_writer(writer_side: str) -> Optional[str]:
    w = str(writer_side or "").upper()
    if w == "CE":
        return "CE OI"
    if w == "PE":
        return "PE OI"
    return None


def _line_key(strike: int, oi_side: str) -> str:
    return f"{strike}|{oi_side}"


def nearest_strike(spot: float, strikes: List[int], step: int = 50) -> Optional[int]:
    """Snap live spot to nearest strike on the contract lattice."""
    if spot <= 0 or not strikes:
        return None
    return min(strikes, key=lambda s: abs(s - spot))


def _candidate_metrics(cand: Dict[str, Any]) -> Tuple[int, Optional[float], Optional[float]]:
    """Confluence score + ΔOI / volume for ladder cells (from journaled candidate)."""
    score = _as_int(cand.get("total_score"))
    source = cand.get("source_alert") if isinstance(cand.get("source_alert"), dict) else {}
    v5 = source.get("velocity_5m") if isinstance(source.get("velocity_5m"), dict) else {}
    oi_delta = _as_float_opt(v5.get("delta"))
    volume_delta = _as_float_opt(v5.get("volume_delta"))
    if oi_delta is None:
        ov = cand.get("oi_velocity") if isinstance(cand.get("oi_velocity"), dict) else {}
        windows = ov.get("windows_raw") if isinstance(ov.get("windows_raw"), dict) else {}
        oi_delta = _as_float_opt(windows.get(LADDER_OI_WINDOW))
    return score, oi_delta, volume_delta


def resolve_ladder_strikes(
    *,
    chain_strikes: Optional[List[int]],
    observed_strikes: Set[int],
    spot: float,
    strike_step: int = 50,
) -> Tuple[List[int], str]:
    """High->low strike ladder — prefer live option-chain continuum; blank cells OK."""
    step = max(1, int(strike_step or 50))
    if chain_strikes:
        uniq = sorted({_as_int(s) for s in chain_strikes if _as_int(s) > 0}, reverse=True)
        if uniq:
            return uniq, "options_chain"
    if observed_strikes:
        lo = min(observed_strikes)
        hi = max(observed_strikes)
        if spot > 0:
            lo = min(lo, int(round(spot / step) * step) - 4 * step)
            hi = max(hi, int(round(spot / step) * step) + 4 * step)
        continuum = list(range(hi, lo - 1, -step))
        return continuum, "observed_continuum"
    if spot > 0:
        atm = int(round(spot / step) * step)
        continuum = [atm + offset * step for offset in range(8, -9, -1)]
        return continuum, "spot_seed"
    return [], "empty"

def load_journal_frames_and_candidates(
    journal_path: Path,
    trading_day: str,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Load OBSERVATION_FRAME rows and SIGNAL_CANDIDATE rows from today's journal."""
    frames: List[Dict[str, Any]] = []
    integrity: List[str] = []
    if not journal_path.exists():
        return frames, integrity

    candidates_by_frame: Dict[str, List[Dict[str, Any]]] = {}
    legacy_candidates: List[Dict[str, Any]] = []

    try:
        lines = journal_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return frames, integrity

    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        event = row.get("event")
        if event == SOURCE_EVENT_FRAME:
            frames.append(row)
        elif event == SOURCE_EVENT_CANDIDATE:
            fid = str(row.get("observation_frame_id") or "")
            if fid:
                candidates_by_frame.setdefault(fid, []).append(row)
            else:
                legacy_candidates.append(row)

    # Canonical frame order: journal OBSERVATION_FRAME events chronologically.
    frames.sort(key=lambda r: str(r.get("frame_timestamp") or r.get("recorded_at") or ""))

    built: List[Dict[str, Any]] = []
    seen_frame_ids: Set[str] = set()

    for frame_row in frames:
        fid = str(frame_row.get("frame_id") or "")
        if not fid:
            continue
        seen_frame_ids.add(fid)
        cands = candidates_by_frame.pop(fid, [])
        built.append(_assemble_frame(fid, frame_row, cands, trading_day, integrity))

    # Frames referenced only on candidates (should not happen if contract is correct).
    for fid, cands in sorted(candidates_by_frame.items()):
        built.append(
            _assemble_frame(
                fid,
                {"frame_id": fid, "frame_timestamp": cands[0].get("evaluated_at")},
                cands,
                trading_day,
                integrity,
            )
        )
        integrity.append(f"ORPHAN_FRAME_CANDIDATES:{fid}")

    # Legacy: one observation per frame — no batch guessing.
    for cand in sorted(
        legacy_candidates,
        key=lambda r: str(r.get("evaluated_at") or r.get("recorded_at") or ""),
    ):
        ts = str(cand.get("evaluated_at") or cand.get("recorded_at") or "")
        legacy_fid = f"LEGACY:{trading_day}:{ts}:{cand.get('signal_key', '')}"
        built.append(_assemble_frame(legacy_fid, {"frame_timestamp": ts, "legacy": True}, [cand], trading_day, integrity))
        integrity.append("LEGACY_CANDIDATE_WITHOUT_FRAME_ID")

    built.sort(key=lambda f: str(f.get("frame_timestamp") or ""))
    for idx, frame in enumerate(built, start=1):
        frame["frame_index"] = idx
    return built, integrity


def _assemble_frame(
    frame_id: str,
    frame_meta: Dict[str, Any],
    candidates: List[Dict[str, Any]],
    trading_day: str,
    integrity: List[str],
) -> Dict[str, Any]:
    observations: List[Dict[str, Any]] = []
    seen_keys: Set[str] = set()
    for cand in candidates:
        writer = str(cand.get("writer_side") or "")
        oi_side = _oi_side_from_writer(writer)
        if not oi_side:
            continue
        strike = _as_int(cand.get("strike"))
        if strike <= 0:
            continue
        lk = _line_key(strike, oi_side)
        if lk in seen_keys:
            integrity.append(f"DUPLICATE_OBSERVATION_IN_FRAME:{frame_id}:{lk}")
            continue
        seen_keys.add(lk)
        score, oi_delta, volume_delta = _candidate_metrics(cand)
        observations.append(
            {
                "strike": strike,
                "oi_side": oi_side,
                "score": score,
                "oi_delta": oi_delta,
                "volume_delta": volume_delta,
                "oi_window": LADDER_OI_WINDOW,
                "line_id": f"{trading_day}|{strike}|{oi_side}",
                "signal_key": cand.get("signal_key"),
            }
        )
    observations.sort(key=lambda o: (-o["strike"], o["oi_side"]))
    return {
        "frame_id": frame_id,
        "frame_timestamp": str(frame_meta.get("frame_timestamp") or ""),
        "legacy": bool(frame_meta.get("legacy")),
        "observations": observations,
    }


def build_evidence_cube_payload(
    *,
    journal_path: Path,
    trading_day: str,
    spot: float = 0.0,
    strike_step: int = 50,
    chain_strikes: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """Build Evidence Cube API payload from journaled observation frames.

    Projection 1 (v0.2) is an NSE-style ladder: CALLS | STRIKE | PUTS.
    X = Observation Frame (not clock buckets). Y = live option-chain strike continuum.
    Cell = confluence score + ΔOI (+ volume on hover). Blank = no candidate that frame.
    """
    frames, integrity = load_journal_frames_and_candidates(journal_path, trading_day)

    ce_strikes: Set[int] = set()
    pe_strikes: Set[int] = set()
    line_birth: Dict[str, int] = {}
    observed: Set[int] = set()

    for frame in frames:
        fidx = frame["frame_index"]
        for obs in frame.get("observations") or []:
            strike = _as_int(obs.get("strike"))
            oi_side = str(obs.get("oi_side") or "")
            lk = _line_key(strike, oi_side)
            if strike > 0:
                observed.add(strike)
            if oi_side == "CE OI":
                ce_strikes.add(strike)
            elif oi_side == "PE OI":
                pe_strikes.add(strike)
            if lk not in line_birth:
                line_birth[lk] = fidx

    ce_rows = sorted(ce_strikes, reverse=True)
    pe_rows = sorted(pe_strikes, reverse=True)
    all_strikes = sorted(ce_strikes | pe_strikes)
    spot_strike = nearest_strike(spot, all_strikes or list(observed), step=strike_step) if spot > 0 else None

    ladder_strikes, ladder_source = resolve_ladder_strikes(
        chain_strikes=chain_strikes,
        observed_strikes=observed,
        spot=spot,
        strike_step=strike_step,
    )
    if ladder_strikes and spot > 0:
        spot_strike = nearest_strike(spot, ladder_strikes, step=strike_step) or spot_strike

    n_frames = len(frames)
    # Rich grid for ladder: score + ΔOI + volume
    rich_grid: Dict[str, List[Optional[Dict[str, Any]]]] = {}
    for lk, birth in line_birth.items():
        rich_grid[lk] = [None] * n_frames

    for frame in frames:
        col = frame["frame_index"] - 1
        for obs in frame.get("observations") or []:
            strike = _as_int(obs.get("strike"))
            oi_side = str(obs.get("oi_side") or "")
            lk = _line_key(strike, oi_side)
            if lk not in rich_grid:
                rich_grid[lk] = [None] * n_frames
            rich_grid[lk][col] = {
                "score": _as_int(obs.get("score")),
                "oi_delta": obs.get("oi_delta"),
                "volume_delta": obs.get("volume_delta"),
            }

    # Legacy band lines (3D / Follow / deferred split-P1) — score array only
    lines: List[Dict[str, Any]] = []
    for strike in ce_rows:
        lk = _line_key(strike, "CE OI")
        cells = rich_grid.get(lk, [None] * n_frames)
        lines.append(
            {
                "strike": strike,
                "oi_side": "CE OI",
                "band": "CE",
                "line_id": f"{trading_day}|{strike}|CE OI",
                "birth_frame_index": line_birth.get(lk, 1),
                "cells": [c.get("score") if isinstance(c, dict) else c for c in cells],
            }
        )
    for strike in pe_rows:
        lk = _line_key(strike, "PE OI")
        cells = rich_grid.get(lk, [None] * n_frames)
        lines.append(
            {
                "strike": strike,
                "oi_side": "PE OI",
                "band": "PE",
                "line_id": f"{trading_day}|{strike}|PE OI",
                "birth_frame_index": line_birth.get(lk, 1),
                "cells": [c.get("score") if isinstance(c, dict) else c for c in cells],
            }
        )

    ladder_rows: List[Dict[str, Any]] = []
    for strike in ladder_strikes:
        ce_lk = _line_key(strike, "CE OI")
        pe_lk = _line_key(strike, "PE OI")
        ladder_rows.append(
            {
                "strike": strike,
                "is_atm": bool(spot_strike is not None and strike == spot_strike),
                "ce_line_id": f"{trading_day}|{strike}|CE OI",
                "pe_line_id": f"{trading_day}|{strike}|PE OI",
                "ce_cells": rich_grid.get(ce_lk, [None] * n_frames),
                "pe_cells": rich_grid.get(pe_lk, [None] * n_frames),
            }
        )

    return {
        "version": EVIDENCE_CUBE_VERSION,
        "instrument": "Atlas Evidence Cube",
        "mode": "observation_only",
        "trading_day": trading_day,
        "principle": (
            "An Observation Frame is the atomic unit of the Evidence Cube. "
            "Everything emitted during one scoreboard evaluation belongs to the same frame."
        ),
        "dimensions": {
            "x": "observation_frame",
            "y": "strike_ladder",
            "z": "score",
            "cell": "score_and_oi_delta",
        },
        "projections": {
            "projection_1": {
                "id": "nse_ladder_time_strike_score",
                "label": "Evidence Cube — Projection 1 (NSE Ladder)",
                "version": "0.2",
                "layout": "calls_strike_puts",
                "axes": {"x": "observation_frame", "y": "strike_ladder", "cell": "score+ΔOI"},
                "note": "Option-chain ladder with intelligence — not a heatmap.",
            },
            "projection_1_split_band": {
                "id": "time_strike_score_split",
                "label": "Projection 1 split CE/PE band (v0.1)",
                "version": "0.1",
                "status": "deferred_old",
                "axes": {"x": "observation_frame", "y": "strike", "z": "score"},
            },
            "follow_strike": {
                "id": "time_score_follow",
                "label": "Follow Strike",
                "axes": {"x": "observation_frame", "y": "score"},
            },
        },
        "frame_count": n_frames,
        "frames": frames,
        "lines": lines,
        "ce_rows": ce_rows,
        "pe_rows": pe_rows,
        "ladder": {
            "layout": "nse_chain",
            "source": ladder_source,
            "oi_window": LADDER_OI_WINDOW,
            "oi_label": "ΔOI 5m",
            "volume_label": "ΔVol 5m",
            "strikes": ladder_strikes,
            "atm_strike": spot_strike,
            "rows": ladder_rows,
        },
        "spot_reference": {
            "spot": spot,
            "nearest_strike": spot_strike,
            "note": "ATM highlight on strike column — orientation only, not scored",
        },
        "score_min": SCORE_MIN,
        "score_max": SCORE_MAX,
        "journal_path": str(journal_path),
        "integrity_warnings": integrity[:200],
        "note": (
            "Experimental research instrument. Projection 1 = institutional option-chain "
            "ladder + confluence/ΔOI. Does not prove scorecard correctness."
        ),
    }


def build_timeline_from_cube(cube: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten Evidence Cube frames into v1 timeline points (downstream projection)."""
    trading_day = str(cube.get("trading_day") or "")
    points: List[Dict[str, Any]] = []
    line_ids: List[str] = []
    seen: Set[str] = set()
    for frame in cube.get("frames") or []:
        ts = frame.get("frame_timestamp")
        for obs in frame.get("observations") or []:
            line_id = str(obs.get("line_id") or "")
            points.append(
                {
                    "line_id": line_id,
                    "trading_day": trading_day,
                    "timestamp": ts,
                    "observation_frame_id": frame.get("frame_id"),
                    "frame_index": frame.get("frame_index"),
                    "strike": obs.get("strike"),
                    "oi_side": obs.get("oi_side"),
                    "score": obs.get("score"),
                }
            )
            if line_id and line_id not in seen:
                seen.add(line_id)
                line_ids.append(line_id)
    return {
        "version": "1.0",
        "instrument": "Signal Confluence Observatory",
        "mode": "observation_only",
        "projection": "timeline",
        "source": "evidence_cube",
        "trading_day": trading_day,
        "emission_count": len(points),
        "line_count": len(line_ids),
        "line_ids": line_ids,
        "points": points,
        "y_min": SCORE_MIN,
        "y_max": SCORE_MAX,
    }


def _selftest() -> None:
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="evidence-cube-selftest-")) / "nifty_signal_candidates_2026-07-23.jsonl"
    day = "2026-07-23"
    rows = [
        {"event": "SIGNAL_CANDIDATE", "evaluated_at": "2026-07-23 10:00:00", "signal_key": "k1",
         "writer_side": "PE", "strike": 23000, "total_score": 78,
         "source_alert": {"velocity_5m": {"delta": 45000.0, "volume_delta": 1200.0}}},
        {"event": "SIGNAL_CANDIDATE", "evaluated_at": "2026-07-23 10:01:00", "signal_key": "k2",
         "writer_side": "CE", "strike": 23100, "total_score": 55,
         "source_alert": {"velocity_5m": {"delta": -20000.0}}},
    ]
    with tmp.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")

    assert nearest_strike(23050.0, [22900, 23000, 23100]) == 23000
    assert nearest_strike(0.0, [23000]) is None
    assert nearest_strike(23000.0, []) is None

    ladder, source = resolve_ladder_strikes(chain_strikes=[23000, 23100, 22900], observed_strikes=set(), spot=23000.0)
    assert source == "options_chain"
    assert ladder == [23100, 23000, 22900]

    seeded, seed_source = resolve_ladder_strikes(chain_strikes=None, observed_strikes=set(), spot=23000.0, strike_step=100)
    assert seed_source == "spot_seed"
    assert 23000 in seeded

    empty_ladder, empty_source = resolve_ladder_strikes(chain_strikes=None, observed_strikes=set(), spot=0.0)
    assert empty_source == "empty" and empty_ladder == []

    frames, integrity = load_journal_frames_and_candidates(tmp, day)
    assert len(frames) == 2  # legacy path: one frame per candidate (no observation_frame_id present)
    assert all(f["legacy"] for f in frames)
    assert any("LEGACY_CANDIDATE_WITHOUT_FRAME_ID" in w for w in integrity)

    cube = build_evidence_cube_payload(journal_path=tmp, trading_day=day, spot=23020.0, strike_step=100)
    assert cube["version"] == EVIDENCE_CUBE_VERSION
    assert cube["frame_count"] == 2
    assert 23000 in cube["ce_rows"] or 23000 in cube["pe_rows"] or 23100 in cube["ce_rows"]
    assert cube["ladder"]["atm_strike"] is not None
    assert len(cube["lines"]) >= 1

    timeline = build_timeline_from_cube(cube)
    assert timeline["emission_count"] == 2
    assert timeline["line_count"] == 2

    # Nonexistent journal path -> empty cube, never raises.
    missing = build_evidence_cube_payload(journal_path=Path("/does/not/exist.jsonl"), trading_day=day)
    assert missing["frame_count"] == 0
    assert missing["frames"] == []

    print("[analytics.evidence_cube] selftest OK: ladder resolution, legacy frame reconstruction, cube+timeline build")


if __name__ == "__main__":
    _selftest()
