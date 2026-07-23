#!/usr/bin/env python3
"""Normalize desk market-profile / liquidity state and emit PTE evidence candidates.

Ported faithfully from quant-desk-engine v4/ATLAS's
pte_profile_liquidity_evidence.py (mentor-authored). No logic changed. Only
adaptation: imports GRAB_DOWN/GRAB_NONE/GRAB_UP from
nifty.analytics.liquidity_engine (this session's port) instead of the
standalone nifty_liquidity_engine module.

Bridges nifty-dashboard's market_profile block and the newly-ported
liquidity_engine.py output into nifty.pte.evidence_engine's EvidenceRecord
candidate shape, same pattern as chain_evidence.py.

Not yet wired into the live pipeline.
Self-check: python -m nifty.pte.liquidity_evidence
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from nifty.analytics.liquidity_engine import GRAB_DOWN, GRAB_NONE, GRAB_UP


def _f(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_market_profile(raw: Mapping[str, Any], *, spot: float = 0.0) -> Dict[str, Any]:
    """Unify live dashboard and event_replay market_profile shapes."""
    mp = dict(raw or {})
    vah = _f(mp.get("vah") if mp.get("vah") is not None else mp.get("value_area_high"))
    val = _f(mp.get("val") if mp.get("val") is not None else mp.get("value_area_low"))
    poc = _f(mp.get("poc"))
    acceptance = str(mp.get("acceptance_rejection") or "").strip()
    if not acceptance and poc is not None and spot > 0:
        acceptance = "ACCEPTED_ABOVE_POC" if spot >= poc else "ACCEPTED_BELOW_POC"
    return {
        "status": str(mp.get("status") or "READY"),
        "balance_state": str(mp.get("balance_state") or "UNKNOWN"),
        "poc": poc,
        "vah": vah,
        "val": val,
        "acceptance_rejection": acceptance or None,
        "poor_high": bool(mp.get("poor_high")),
        "poor_low": bool(mp.get("poor_low")),
    }


def normalize_liquidity_engine(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """Unify live liquidity_engine and replay liquidity blocks."""
    liq = dict(raw or {})
    grab = str(liq.get("liquidity_grab") or GRAB_NONE).upper()
    read = str(liq.get("read") or "").strip()
    if not read or read in {"UNKNOWN", "NONE"}:
        if grab == GRAB_DOWN:
            read = "Downside liquidity grab"
        elif grab == GRAB_UP:
            read = "Upside liquidity grab"
        elif grab not in {"", GRAB_NONE, "NONE"}:
            read = grab.replace("_", " ").title()
        else:
            read = "Range / no active grab"
    eq_high = liq.get("equal_highs") or []
    eq_low = liq.get("equal_lows") or []
    if not isinstance(eq_high, list):
        eq_high = []
    if not isinstance(eq_low, list):
        eq_low = []
    untapped_above = liq.get("untapped_above") or []
    untapped_below = liq.get("untapped_below") or []
    conf = liq.get("confirmation") or {}
    return {
        "status": str(liq.get("status") or "READY"),
        "read": read,
        "liquidity_grab": grab,
        "liquidity_grab_confirmed": bool(liq.get("liquidity_grab_confirmed")),
        "equal_high_count": len(eq_high),
        "equal_low_count": len(eq_low),
        "untapped_above_count": len(untapped_above) if isinstance(untapped_above, list) else 0,
        "untapped_below_count": len(untapped_below) if isinstance(untapped_below, list) else 0,
        "confirmation_stage": str(conf.get("stage") or liq.get("confirmation_stage") or "NONE"),
        "structure_bias": str(liq.get("structure_bias") or liq.get("market_structure") or ""),
    }


def liquidity_read_for_evidence(liq: Mapping[str, Any]) -> str:
    norm = normalize_liquidity_engine(liq)
    return str(norm.get("read") or "UNKNOWN")


def append_market_profile_liquidity_evidence(
    cands: List[Dict[str, Any]],
    *,
    market_profile: Mapping[str, Any],
    liquidity_engine: Mapping[str, Any],
    spot: float,
    observatory_state_ids: Mapping[str, str],
    interpretation_state_ids: Mapping[str, str],
    now_iso: str,
) -> None:
    """Append profile/liquidity EVD rows beyond balance_state + read."""
    mp = normalize_market_profile(market_profile, spot=spot)
    liq = normalize_liquidity_engine(liquidity_engine)

    def add(
        label: str,
        observatory_id: str,
        field_path: str,
        observation: str,
        interpretation: str,
        *,
        weight: str = "moderate",
        supports: Optional[List[str]] = None,
    ) -> None:
        cands.append(
            {
                "label": label,
                "source": {
                    "observatory_id": observatory_id,
                    "observatory_state_id": observatory_state_ids[observatory_id],
                    "field_path": field_path,
                },
                "observation": observation,
                "interpretation": interpretation,
                "interpretation_state_id": interpretation_state_ids[observatory_id],
                "quality": "inferred",
                "weight_hint": weight,
                "theory_hints": {"supports": supports or [], "contradicts": []},
                "effective_at": now_iso,
                "observed_at": now_iso,
            }
        )

    if mp.get("poc") is not None:
        add(
            "Market profile POC",
            "market_profile",
            "market_profile.poc",
            f"POC={mp['poc']:.2f} · spot={spot:.2f}",
            "Point of control vs spot",
            weight="moderate",
            supports=["range_compression"],
        )

    if mp.get("vah") is not None and mp.get("val") is not None:
        add(
            "Market profile value area",
            "market_profile",
            "market_profile.value_area",
            f"VAH={mp['vah']:.2f} · VAL={mp['val']:.2f} · spot={spot:.2f}",
            "Value area envelope vs spot",
            weight="moderate",
            supports=["range_compression"],
        )

    if mp.get("acceptance_rejection"):
        acc = str(mp["acceptance_rejection"])
        add(
            "Market profile acceptance",
            "market_profile",
            "market_profile.acceptance_rejection",
            f"Acceptance={acc}",
            "POC acceptance vs rejection",
            weight="strong" if "ACCEPTED" in acc else "moderate",
            supports=(
                ["breakout_expansion"]
                if "ABOVE" in acc
                else (["range_compression"] if "BELOW" in acc else ["range_compression"])
            ),
        )

    grab = str(liq.get("liquidity_grab") or GRAB_NONE)
    if grab != GRAB_NONE:
        add(
            "Liquidity grab",
            "liquidity",
            "liquidity_engine.liquidity_grab",
            f"Liquidity grab={grab}",
            "Active liquidity sweep/grab",
            weight="strong",
            supports=["breakout_expansion", "inventory_transfer"],
        )

    if liq.get("equal_high_count") or liq.get("equal_low_count"):
        add(
            "Liquidity pool map",
            "liquidity",
            "liquidity_engine.pools",
            (
                f"Equal highs={liq.get('equal_high_count', 0)} · "
                f"equal lows={liq.get('equal_low_count', 0)} · "
                f"untapped above={liq.get('untapped_above_count', 0)} · "
                f"below={liq.get('untapped_below_count', 0)}"
            ),
            "Resting liquidity pool inventory",
            weight="moderate",
            supports=["inventory_transfer"],
        )

    stage = str(liq.get("confirmation_stage") or "NONE")
    if stage not in {"", "NONE"}:
        add(
            "Liquidity grab confirmation",
            "liquidity",
            "liquidity_engine.confirmation",
            f"Confirmation stage={stage} · confirmed={liq.get('liquidity_grab_confirmed')}",
            "Post-grab confirmation state",
            weight="strong" if stage in {"RECLAIM", "CONTINUATION", "HOLD"} else "moderate",
            supports=["breakout_expansion"],
        )


def _selftest() -> None:
    mp_raw = {"poc": 23000.0, "vah": 23100.0, "val": 22900.0, "balance_state": "BALANCED_ROTATION"}
    mp = normalize_market_profile(mp_raw, spot=23150.0)
    assert mp["acceptance_rejection"] == "ACCEPTED_ABOVE_POC"

    liq_raw = {
        "liquidity_grab": GRAB_UP, "equal_highs": [{"level": 23200}], "equal_lows": [],
        "confirmation": {"stage": "RECLAIM"}, "liquidity_grab_confirmed": True,
    }
    liq = normalize_liquidity_engine(liq_raw)
    assert liq["read"] == "Upside liquidity grab"
    assert liq["equal_high_count"] == 1
    assert liq["confirmation_stage"] == "RECLAIM"

    assert liquidity_read_for_evidence({}) == "Range / no active grab"
    assert liquidity_read_for_evidence({"liquidity_grab": GRAB_DOWN}) == "Downside liquidity grab"

    cands: List[Dict[str, Any]] = []
    obs_ids = {"market_profile": "OBS-1", "liquidity": "OBS-2"}
    int_ids = {"market_profile": "INT-1", "liquidity": "INT-2"}
    append_market_profile_liquidity_evidence(
        cands, market_profile=mp_raw, liquidity_engine=liq_raw, spot=23150.0,
        observatory_state_ids=obs_ids, interpretation_state_ids=int_ids, now_iso="2026-07-23T10:00:00+05:30",
    )
    assert len(cands) >= 4  # poc, value area, acceptance, liquidity grab, confirmation
    labels = {c["label"] for c in cands}
    assert "Liquidity grab" in labels
    assert "Market profile POC" in labels

    # No grab, no pools, no confirmation -> only market-profile candidates emitted.
    quiet: List[Dict[str, Any]] = []
    append_market_profile_liquidity_evidence(
        quiet, market_profile={}, liquidity_engine={}, spot=0.0,
        observatory_state_ids=obs_ids, interpretation_state_ids=int_ids, now_iso="2026-07-23T10:00:00+05:30",
    )
    assert quiet == []

    print("[pte.liquidity_evidence] selftest OK: profile/liquidity normalization, evidence candidate emission")


if __name__ == "__main__":
    _selftest()
