#!/usr/bin/env python3
"""Legacy scoreboard v2 (shadow) — re-score v1 dimensions with journal-learned weights.

Obeys desk Laws 1-6 (journal/desk_principles.md): OI is fact only; directional
meaning is a call-side/put-side positioning hypothesis; reject -> no trade.
Shadow only — does not open paper. Same observation ladder as Legacy v1;
weights are journal-learned, not a different identity claim.

Ported faithfully from quant-desk-engine v4/ATLAS's nifty_signal_confluence_v2.py
(mentor-authored). No logic changed. Adaptations:
- CONFIG_PATH resolves via nifty.paths.PROJECT_ROOT instead of bare
  __file__-relative (config/legacy_score_shadow.json - not present in
  nifty-dashboard yet, so reload_legacy_score_shadow() loads an empty dict
  and rescore_legacy_v2() always returns the documented
  {"status": "SHADOW_NOT_TRAINED"} placeholder until that config is
  trained/added - matches the source's own graceful-empty contract).
- `from nifty_signal_confluence import CONFLUENCE_WEIGHTS,
  TRADE_MIN_CONFLUENCE, _grade` -> nifty.analytics.confluence (already
  ported/updated this session, identical names).
- `from desk_reasoning_ladder import build_desk_reasoning_from_candidate`
  -> nifty.analytics.reasoning_ladder (already ported this session,
  identical name/signature).

This is the legacy_score_v2 shadow model nifty/analytics/engine_paper_book.py
already anticipates (its engine_gate_pass() degrades L2 to False specifically
because this field didn't exist upstream yet). Purely a re-weighting of
v1's ALREADY-computed dimension scores/blockers — reads candidate["dimensions"]/
["total_score"]/["blockers"] etc, writes only a new "legacy_score_v2" key.
Never mutates v1's own paper_eligible/total_score/grade fields, so attaching
it changes nothing about what v1 already decided.

Not yet wired into the live pipeline (no caller invokes attach_legacy_score_v2
yet, and no config/legacy_score_shadow.json exists so it would stay in
SHADOW_NOT_TRAINED status if it were).
Self-check: python -m nifty.analytics.confluence_v2
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from nifty.analytics.confluence import CONFLUENCE_WEIGHTS, TRADE_MIN_CONFLUENCE, _grade
from nifty.analytics.reasoning_ladder import build_desk_reasoning_from_candidate
from nifty.paths import PROJECT_ROOT

CONFIG_PATH = PROJECT_ROOT / "config" / "legacy_score_shadow.json"
_LEGACY_SCORE_SHADOW: Dict[str, Any] = {}


def load_legacy_score_shadow_config(path: Path = CONFIG_PATH) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def reload_legacy_score_shadow(path: Path = CONFIG_PATH) -> Dict[str, Any]:
    global _LEGACY_SCORE_SHADOW
    _LEGACY_SCORE_SHADOW = load_legacy_score_shadow_config(path)
    return _LEGACY_SCORE_SHADOW


def _dim_score_v2(dimension: str, v1_dim: Dict[str, Any], v2_max: int) -> int:
    """Scale each dimension's v1 achievement ratio onto the v2 weight."""
    v1_score = int(v1_dim.get("score") or 0)
    v1_max = int(v1_dim.get("max") or 0)
    if v1_max <= 0:
        return v2_max if bool(v1_dim.get("pass")) else 0
    ratio = max(0.0, min(1.0, v1_score / v1_max))
    return int(round(v2_max * ratio))


def rescore_legacy_v2(
    candidate: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Recompute score from existing v1 dimension pass/fail flags using v2 weights."""
    cfg = config if config is not None else _LEGACY_SCORE_SHADOW
    if not cfg:
        return {"status": "SHADOW_NOT_TRAINED", "model": "legacy_score_v2_shadow"}

    weights_v2 = cfg.get("weights_v2") or dict(CONFLUENCE_WEIGHTS)
    v1_dims = candidate.get("dimensions") or {}
    v2_dims: Dict[str, Dict[str, Any]] = {}
    dimension_deltas: List[Dict[str, Any]] = []

    for dim, v1_dim in v1_dims.items():
        v1_max = int(v1_dim.get("max") or weights_v2.get(dim, 0))
        v2_max = int(weights_v2.get(dim, v1_max))
        v1_score = int(v1_dim.get("score") or 0)
        v2_score = _dim_score_v2(dim, v1_dim, v2_max)
        v2_dims[dim] = {
            **v1_dim,
            "max": v2_max,
            "score": v2_score,
        }
        if v2_score != v1_score or v2_max != v1_max:
            dimension_deltas.append(
                {
                    "dimension": dim,
                    "score_v1": v1_score,
                    "score_v2": v2_score,
                    "max_v1": v1_max,
                    "max_v2": v2_max,
                }
            )

    total_v1 = int(candidate.get("total_score") or 0)
    max_v1 = int(candidate.get("max_score") or sum((cfg.get("weights_v1") or CONFLUENCE_WEIGHTS).values()))
    total_v2 = sum(int(dim.get("score") or 0) for dim in v2_dims.values())
    max_v2 = sum(int(v) for v in weights_v2.values())

    regime = str(candidate.get("playbook_phase") or "GLOBAL")
    thresholds = cfg.get("thresholds_v2") or {}
    min_score_v2 = int(thresholds.get(regime) or thresholds.get("GLOBAL") or cfg.get("min_score_v2") or TRADE_MIN_CONFLUENCE)

    blockers = list(candidate.get("blockers") or [])
    hard_blockers = set(cfg.get("hard_blockers_v2") or [])
    watch_only = set(cfg.get("watch_only_blockers_v2") or [])
    hard_hits = sorted(b for b in blockers if b in hard_blockers)
    watch_hits = sorted(b for b in blockers if b in watch_only)

    confluence_ready_v2 = total_v2 >= min_score_v2
    paper_eligible_v2 = confluence_ready_v2 and not hard_hits

    # Observability only — same dimensions (pass flags preserved); does not change eligibility
    desk_reasoning = build_desk_reasoning_from_candidate(
        {**candidate, "dimensions": v2_dims},
        engine="legacy_v2_shadow",
    )

    return {
        "model": "legacy_score_v2_shadow",
        "status": "SHADOW_READY",
        "weights_version": cfg.get("updated_at"),
        "total_score": total_v2,
        "max_score": max_v2,
        "score_pct": round((total_v2 / max_v2) * 100, 1) if max_v2 else 0.0,
        "grade": _grade(total_v2, max_v2),
        "dimensions": v2_dims,
        "min_score": min_score_v2,
        "confluence_ready": confluence_ready_v2,
        "paper_eligible": paper_eligible_v2,
        "hard_blockers": hard_hits,
        "watch_blockers": watch_hits,
        "desk_reasoning": desk_reasoning,
        "delta_vs_v1": {
            "total_score": total_v2 - total_v1,
            "score_pct": round(
                (total_v2 / max_v2 * 100 if max_v2 else 0.0)
                - (total_v1 / max_v1 * 100 if max_v1 else 0.0),
                1,
            ),
            "total_score_on_v1_scale": int(round(total_v2 * max_v1 / max_v2)) if max_v2 else total_v2,
            "paper_eligible": bool(paper_eligible_v2) != bool(candidate.get("paper_eligible")),
            "grade_changed": _grade(total_v2, max_v2) != str(candidate.get("grade") or ""),
        },
        "dimension_deltas": dimension_deltas,
        "comparison": {
            "total_score_v1": total_v1,
            "total_score_v2": total_v2,
            "max_score_v1": max_v1,
            "max_score_v2": max_v2,
            "paper_eligible_v1": bool(candidate.get("paper_eligible")),
            "paper_eligible_v2": paper_eligible_v2,
            "grade_v1": candidate.get("grade"),
            "grade_v2": _grade(total_v2, max_v2),
            "min_score_v1": int(candidate.get("paper_min_score") or TRADE_MIN_CONFLUENCE),
            "min_score_v2": min_score_v2,
        },
    }


def attach_legacy_score_v2(candidate: Dict[str, Any]) -> Dict[str, Any]:
    candidate["legacy_score_v2"] = rescore_legacy_v2(candidate)
    return candidate


reload_legacy_score_shadow()


def _selftest() -> None:
    global _LEGACY_SCORE_SHADOW
    original = dict(_LEGACY_SCORE_SHADOW)
    try:
        # No config loaded (config/legacy_score_shadow.json doesn't exist yet) ->
        # graceful SHADOW_NOT_TRAINED, never raises.
        _LEGACY_SCORE_SHADOW = {}
        candidate = {
            "dimensions": {
                "atm_proximity": {"score": 20, "max": 25, "pass": True},
                "oi_sustained": {"score": 0, "max": 20, "pass": False},
            },
            "total_score": 20,
            "max_score": 45,
            "blockers": ["STRIKE_TOO_FAR"],
            "grade": "C",
            "paper_eligible": False,
        }
        untrained = rescore_legacy_v2(candidate)
        assert untrained["status"] == "SHADOW_NOT_TRAINED"

        # Explicit config passed directly -> full re-score.
        cfg = {
            "weights_v2": {"atm_proximity": 30, "oi_sustained": 25},
            "min_score_v2": 20,
            "hard_blockers_v2": ["STRIKE_TOO_FAR"],
            "updated_at": "2026-07-01",
        }
        result = rescore_legacy_v2(candidate, config=cfg)
        assert result["status"] == "SHADOW_READY"
        assert result["model"] == "legacy_score_v2_shadow"
        # atm_proximity ratio 20/25=0.8 * 30 = 24; oi_sustained 0/20=0 * 25 = 0
        assert result["dimensions"]["atm_proximity"]["score"] == 24
        assert result["dimensions"]["oi_sustained"]["score"] == 0
        assert result["total_score"] == 24
        assert result["max_score"] == 55
        assert result["hard_blockers"] == ["STRIKE_TOO_FAR"]
        assert result["paper_eligible"] is False  # hard blocker present
        assert result["dimension_deltas"]  # both dims changed vs v1

        attached = attach_legacy_score_v2(dict(candidate))
        assert attached["legacy_score_v2"]["status"] == "SHADOW_NOT_TRAINED"  # global config still empty

        # Passing config via the global cache path.
        _LEGACY_SCORE_SHADOW = cfg
        attached2 = attach_legacy_score_v2(dict(candidate))
        assert attached2["legacy_score_v2"]["status"] == "SHADOW_READY"
        assert attached2["legacy_score_v2"]["comparison"]["paper_eligible_v1"] is False

        # v1_max==0 fallback path: dimension with no max but pass=True gets full v2_max.
        pass_only_candidate = {
            "dimensions": {"volatility_align": {"score": 0, "max": 0, "pass": True}},
            "total_score": 0,
            "max_score": 0,
            "blockers": [],
        }
        cfg2 = {"weights_v2": {"volatility_align": 10}}
        result2 = rescore_legacy_v2(pass_only_candidate, config=cfg2)
        assert result2["dimensions"]["volatility_align"]["score"] == 10
    finally:
        _LEGACY_SCORE_SHADOW = original

    print("[analytics.confluence_v2] selftest OK: shadow re-scoring, untrained fallback, attach helper")


if __name__ == "__main__":
    _selftest()
