#!/usr/bin/env python3
"""Desk bias lifecycle — Opening Frame until ORB (09:30), then live bias.

RQ-BIAS-ORB-001 / ATLAS_SESSION_KNOWLEDGE_LIFECYCLE Opening Frame class.
Display + Ph17 context thesis. Does not retune confluence paper weights.

Ported faithfully from quant-desk-engine v4/ATLAS's desk_bias_lifecycle.py
(mentor-authored). No logic changed, no adaptations needed — fully
self-contained (stdlib only), no BASE_DIR/JOURNAL_DIR dependency in the
source either.

Genuinely new capability: resolves a single desk "Bias" label for the UI
that behaves differently before vs after the 09:30 opening-range-breakout
seal — morning combined_bias before ORB (Opening Frame), then a live
chain-bias/playbook-phase recipe after. nifty-dashboard has no equivalent
bias-lifecycle resolver today; nothing currently computes a post-ORB live
bias chip. Explicitly display-only per the source's own docstring — does
not touch confluence scoring/paper weights.

Not yet wired into the live pipeline (no caller invokes resolve_desk_bias
yet).
Self-check: python -m nifty.analytics.bias_lifecycle
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Any, Dict, Optional

# Same seal as PTE EXP-MC expiry@09:30 (ORB complete)
FRAME_SEAL_HHMM = time(9, 30)

_CHAIN_TO_BIAS = {
    "PE_DOMINANT_CHAIN": "BULLISH",
    "CE_DOMINANT_CHAIN": "BEARISH",
    "CHOP": "CHOP",
    "QUIET": "NEUTRAL",
    "NEUTRAL": "NEUTRAL",
}

_PLAYBOOK_TO_BIAS = {
    "EXTENSION": "BULLISH",
    "PE_BUILD_930": "BULLISH",
    "ORB_RECLAIMED": "BULLISH",
    "PE_UNWIND": "BEARISH",
    "CE_PUSH": "BEARISH",
    "RECLAIM_FAILED": "BEARISH",
    "PE_DIVERGENCE": "NEUTRAL",
    "GAP_DOWN": "BEARISH",
    "GAP_UP": "BULLISH",
}


def frame_still_active(now: Optional[datetime] = None) -> bool:
    """True while Opening Frame may still label the desk Bias chip."""
    now = now or datetime.now()
    return now.time() < FRAME_SEAL_HHMM


def _norm_bias(raw: Any) -> str:
    text = str(raw or "UNKNOWN").upper().strip()
    if not text:
        return "UNKNOWN"
    if text.startswith("BULL"):
        return "BULLISH"
    if text.startswith("BEAR"):
        return "BEARISH"
    if text in {"CHOP", "NEUTRAL", "UNKNOWN", "FLAT"}:
        return text if text != "FLAT" else "NEUTRAL"
    return text


def live_bias_from_tape(
    *,
    chain_bias: Optional[Dict[str, Any]] = None,
    playbook: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Map live participant/playbook evidence -> desk bias label (post-ORB)."""
    chain = chain_bias or {}
    chain_label = str(chain.get("label") or "")
    if chain_label in _CHAIN_TO_BIAS:
        return {
            "label": _CHAIN_TO_BIAS[chain_label],
            "recipe": "chain_bias",
            "detail": str(chain.get("detail") or chain_label),
        }

    phase = str((playbook or {}).get("phase") or "")
    if phase in _PLAYBOOK_TO_BIAS:
        return {
            "label": _PLAYBOOK_TO_BIAS[phase],
            "recipe": "playbook_phase",
            "detail": str((playbook or {}).get("phase_note") or phase),
        }

    return {
        "label": "NEUTRAL",
        "recipe": "default",
        "detail": "No clear chain/playbook lean after ORB",
    }


def resolve_desk_bias(
    *,
    morning_context: Optional[Dict[str, Any]] = None,
    chain_bias: Optional[Dict[str, Any]] = None,
    playbook: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Return sealed desk bias for UI / Ph17.

    Before 09:30: Opening Frame = morning combined_bias.
    At/after 09:30: Frame archived; Bias chip = live tape recipe.
    """
    now = now or datetime.now()
    mc = morning_context or {}
    brief = mc.get("desk_brief") if isinstance(mc.get("desk_brief"), dict) else {}
    frame_bias = _norm_bias(
        brief.get("combined_bias") or mc.get("combined_bias") or mc.get("morning_bias")
    )
    frame_active = frame_still_active(now)

    if frame_active:
        return {
            "label": frame_bias,
            "source": "OPENING_FRAME",
            "frame_active": True,
            "frame_bias": frame_bias,
            "frame_sealed": False,
            "seal_hhmm": "09:30",
            "recipe": "morning_combined_bias",
            "detail": "Opening Frame — morning combined_bias until ORB 09:30",
        }

    live = live_bias_from_tape(chain_bias=chain_bias, playbook=playbook)
    return {
        "label": live["label"],
        "source": "LIVE",
        "frame_active": False,
        "frame_bias": frame_bias,
        "frame_sealed": True,
        "seal_hhmm": "09:30",
        "recipe": live["recipe"],
        "detail": live["detail"],
    }


def _selftest() -> None:
    assert frame_still_active(datetime(2026, 7, 21, 9, 0)) is True
    assert frame_still_active(datetime(2026, 7, 21, 9, 30)) is False
    assert frame_still_active(datetime(2026, 7, 21, 10, 0)) is False

    assert _norm_bias("bullish") == "BULLISH"
    assert _norm_bias("BEARISH_HEDGED") == "BEARISH"
    assert _norm_bias("FLAT") == "NEUTRAL"
    assert _norm_bias(None) == "UNKNOWN"
    assert _norm_bias("CHOP") == "CHOP"

    live1 = live_bias_from_tape(chain_bias={"label": "PE_DOMINANT_CHAIN", "detail": "PE OI dominant"})
    assert live1["label"] == "BULLISH" and live1["recipe"] == "chain_bias"

    live2 = live_bias_from_tape(playbook={"phase": "PE_UNWIND", "phase_note": "unwinding"})
    assert live2["label"] == "BEARISH" and live2["recipe"] == "playbook_phase"

    live3 = live_bias_from_tape()
    assert live3["label"] == "NEUTRAL" and live3["recipe"] == "default"

    # Before ORB seal -> Opening Frame from morning combined_bias.
    frame = resolve_desk_bias(
        morning_context={"desk_brief": {"combined_bias": "BULLISH"}},
        now=datetime(2026, 7, 21, 9, 15),
    )
    assert frame["source"] == "OPENING_FRAME"
    assert frame["frame_active"] is True
    assert frame["label"] == "BULLISH"

    # After ORB seal -> live tape recipe, frame_bias still carried for reference.
    live = resolve_desk_bias(
        morning_context={"combined_bias": "BEARISH"},
        chain_bias={"label": "CE_DOMINANT_CHAIN"},
        now=datetime(2026, 7, 21, 10, 0),
    )
    assert live["source"] == "LIVE"
    assert live["frame_sealed"] is True
    assert live["label"] == "BEARISH"
    assert live["frame_bias"] == "BEARISH"

    print("[analytics.bias_lifecycle] selftest OK: opening-frame seal, live-tape recipe mapping")


if __name__ == "__main__":
    _selftest()
