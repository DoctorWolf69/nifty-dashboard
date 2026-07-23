#!/usr/bin/env python3
"""Normalize options-analytics / chain state and emit PTE evidence candidates.

Ported faithfully from quant-desk-engine v4/ATLAS's
pte_chain_analytics_evidence.py (mentor-authored). No logic changed. Fully
self-contained in the source too (stdlib only), so no import adaptation
was needed.

Bridges nifty-dashboard's real analyze_option_chain output (nifty.analytics
.options) into nifty.pte.evidence_engine's EvidenceRecord candidate shape —
each `append_*` call appends dicts ready to pass as
`EvidenceEngine.publish(evidence_candidates=...)`. Reads fields the mentor's
evolved nifty_options_analytics.py added (gamma_structure, dealer_hedge_flow,
price_delta_divergence with a `read` sub-field) — nifty-dashboard's own
analyze_option_chain doesn't emit all of these dict shapes yet, so several
`normalize_options_analytics` lookups will simply come back None/empty
until that upstream data exists; every `append_*` call already guards with
`if oa.get(...) is not None` before adding a candidate, so this degrades
silently rather than emitting garbage evidence.

Not yet wired into the live pipeline.
Self-check: python -m nifty.pte.chain_evidence
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional


def _f(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_options_analytics(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """Unify live analyze_option_chain payload and replay snapshot shapes."""
    oa = dict(raw or {})
    gamma = oa.get("gamma_structure") or {}
    if not isinstance(gamma, dict):
        gamma = {}
    divergence = oa.get("price_delta_divergence") or {}
    if not isinstance(divergence, dict):
        divergence = {}
    hedge = oa.get("dealer_hedge_flow") or {}
    if not isinstance(hedge, dict):
        hedge = {}

    net_gex = _f(oa.get("net_gex"))
    if net_gex is None and oa.get("net_gex_cr") is not None:
        net_gex = _f(oa.get("net_gex_cr")) * 1e7

    return {
        "net_gex": net_gex,
        "net_gex_cr": _f(oa.get("net_gex_cr")),
        "net_dealer_delta": _f(oa.get("net_dealer_delta")),
        "net_dealer_delta_change": _f(oa.get("net_dealer_delta_change")),
        "gex_regime": str(oa.get("gex_regime") or "UNKNOWN").upper(),
        "expected_move_pts": _f(oa.get("expected_move_pts")),
        "pcr_oi_chain": _f(oa.get("pcr_oi_chain")),
        "atm_iv": _f(oa.get("atm_iv")),
        "iv_rank": _f(oa.get("iv_rank")),
        "iv_percentile": _f(oa.get("iv_percentile")),
        "iv_velocity_5m": _f(oa.get("iv_velocity_5m")),
        "premium_vs_vix": str(oa.get("premium_vs_vix") or "").upper(),
        "dealer_positioning": str(oa.get("dealer_positioning") or ""),
        "gamma_flip_strike": _f(gamma.get("gamma_flip_strike") or oa.get("gamma_flip_strike")),
        "gamma_flip_distance_pts": _f(gamma.get("gamma_flip_distance_pts") or oa.get("gamma_flip_distance_pts")),
        "gamma_call_wall_strike": _f((gamma.get("gamma_call_wall") or {}).get("strike") or oa.get("gamma_call_wall_strike")),
        "gamma_put_wall_strike": _f((gamma.get("gamma_put_wall") or {}).get("strike") or oa.get("gamma_put_wall_strike")),
        "divergence_label": str(divergence.get("label") or oa.get("divergence_label") or "").upper(),
        "divergence_read": str(divergence.get("read") or oa.get("divergence_read") or ""),
        "hedge_flow_label": str(hedge.get("label") or oa.get("hedge_flow_label") or "").upper(),
        "hedge_flow_score": _f(hedge.get("score") if hedge.get("score") is not None else oa.get("hedge_flow_score")),
    }


def options_analytics_from_replay_snapshot(snapshot: Mapping[str, Any]) -> Dict[str, Any]:
    """Merge replay options_analytics block with legacy greeks_dealer / iv fields."""
    merged = dict(snapshot.get("options_analytics") or {})
    greeks = snapshot.get("greeks_dealer") or {}
    iv = snapshot.get("iv") or {}

    for key, val in (
        ("net_gex_cr", greeks.get("net_gex_cr")),
        ("net_dealer_delta", greeks.get("net_dealer_delta")),
        ("gex_regime", greeks.get("gex_regime")),
        ("gamma_structure", greeks.get("gamma_structure")),
        ("dealer_positioning", greeks.get("dealer_positioning")),
        ("atm_iv", iv.get("atm_iv")),
        ("iv_rank", iv.get("iv_rank")),
        ("iv_percentile", iv.get("iv_percentile")),
    ):
        if merged.get(key) in (None, "", 0) and val not in (None, ""):
            merged[key] = val

    if merged.get("expected_move_pts") in (None, 0) and iv.get("expected_move_pts"):
        merged["expected_move_pts"] = iv.get("expected_move_pts")

    return normalize_options_analytics(merged)


def append_chain_analytics_evidence(
    cands: List[Dict[str, Any]],
    *,
    options_analytics: Mapping[str, Any],
    spot: float,
    observatory_state_ids: Mapping[str, str],
    interpretation_state_ids: Mapping[str, str],
    now_iso: str,
) -> None:
    """Append chain-tab EVD rows beyond net_gex / dealer_delta / expected_move."""
    oa = normalize_options_analytics(options_analytics)

    def add(
        label: str,
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
                    "observatory_id": "chain",
                    "observatory_state_id": observatory_state_ids["chain"],
                    "field_path": field_path,
                },
                "observation": observation,
                "interpretation": interpretation,
                "interpretation_state_id": interpretation_state_ids["chain"],
                "quality": "inferred",
                "weight_hint": weight,
                "theory_hints": {"supports": supports or [], "contradicts": []},
                "effective_at": now_iso,
                "observed_at": now_iso,
            }
        )

    regime = str(oa.get("gex_regime") or "")
    if regime and regime != "UNKNOWN":
        add(
            "GEX regime",
            "options_analytics.gex_regime",
            f"GEX regime={regime}",
            "Signed gamma environment",
            weight="strong",
            supports=["dealer_hedging", "range_compression"] if "POSITIVE" in regime else ["breakout_expansion"],
        )

    pcr = oa.get("pcr_oi_chain")
    if pcr is not None:
        add(
            "PCR OI chain",
            "options_analytics.pcr_oi_chain",
            f"PCR OI={pcr:.3f}",
            "Put/call OI positioning",
            weight="moderate",
            supports=["premium_writing", "dealer_hedging"],
        )

    if any(oa.get(k) is not None for k in ("iv_rank", "iv_percentile", "iv_velocity_5m")):
        add(
            "IV context",
            "options_analytics.iv_context",
            (
                f"IV rank={oa.get('iv_rank')} · percentile={oa.get('iv_percentile')} · "
                f"velocity_5m={oa.get('iv_velocity_5m')}"
            ),
            "Volatility level and change",
            weight="moderate",
            supports=["breakout_expansion"],
        )

    flip = oa.get("gamma_flip_strike")
    dist = oa.get("gamma_flip_distance_pts")
    if flip is not None and dist is not None and spot > 0:
        add(
            "Gamma flip structure",
            "options_analytics.gamma_structure",
            f"Gamma flip={flip:.2f} · distance={dist:+.2f} pts · spot={spot:.2f}",
            "Spot vs dealer gamma flip",
            weight="strong",
            supports=["dealer_hedging", "breakout_expansion"],
        )

    div_label = str(oa.get("divergence_label") or "")
    if div_label:
        add(
            "Price-delta divergence",
            "options_analytics.price_delta_divergence",
            f"Divergence={div_label} · {oa.get('divergence_read', '')[:80]}",
            "Price vs net-delta coupling",
            weight="strong",
            supports=["inventory_transfer", "premium_buying"],
        )

    hedge_label = str(oa.get("hedge_flow_label") or "")
    if hedge_label:
        add(
            "Dealer hedge flow",
            "options_analytics.dealer_hedge_flow",
            f"Hedge flow={hedge_label} · score={oa.get('hedge_flow_score')}",
            "Dealer hedge pressure proxy",
            weight="strong",
            supports=["dealer_hedging"],
        )

    premium = str(oa.get("premium_vs_vix") or "")
    if premium in {"CHEAP", "FAIR", "EXPENSIVE"}:
        add(
            "Premium vs VIX",
            "options_analytics.premium_vs_vix",
            f"Premium vs VIX={premium}",
            "Options richness vs India VIX",
            weight="weak",
            supports=["premium_writing"] if premium == "EXPENSIVE" else ["premium_buying"],
        )

    if oa.get("dealer_positioning"):
        add(
            "Dealer positioning read",
            "options_analytics.dealer_positioning",
            f"Dealer positioning={oa.get('dealer_positioning')[:120]}",
            "Human-readable dealer gamma posture",
            weight="moderate",
            supports=["dealer_hedging"],
        )


def _selftest() -> None:
    raw = {
        "net_gex_cr": -12.5,
        "gex_regime": "NEGATIVE_GAMMA",
        "pcr_oi_chain": 1.234,
        "iv_rank": 60.0,
        "premium_vs_vix": "EXPENSIVE",
        "dealer_positioning": "Dealers short gamma — expansion risk",
        "gamma_structure": {"gamma_flip_strike": 23100.0, "gamma_flip_distance_pts": 40.0},
        "price_delta_divergence": {"label": "AGGRESSIVE_BUYERS", "read": "Buyers absorbing offers"},
        "dealer_hedge_flow": {"label": "SELLING_INTO_RALLY", "score": 0.7},
    }
    norm = normalize_options_analytics(raw)
    assert norm["net_gex"] == -12.5 * 1e7
    assert norm["gex_regime"] == "NEGATIVE_GAMMA"
    assert norm["gamma_flip_strike"] == 23100.0
    assert norm["divergence_label"] == "AGGRESSIVE_BUYERS"
    assert norm["hedge_flow_label"] == "SELLING_INTO_RALLY"

    # Missing/malformed nested dicts degrade to empty, never raise.
    degraded = normalize_options_analytics({"gamma_structure": "not-a-dict", "net_gex_cr": None})
    assert degraded["gamma_flip_strike"] is None
    assert degraded["gex_regime"] == "UNKNOWN"

    replay_snapshot = {
        "options_analytics": {},
        "greeks_dealer": {"net_gex_cr": 5.0, "gex_regime": "POSITIVE_GAMMA"},
        "iv": {"atm_iv": 0.14, "iv_rank": 40.0, "expected_move_pts": 180.0},
    }
    merged = options_analytics_from_replay_snapshot(replay_snapshot)
    assert merged["net_gex_cr"] == 5.0
    assert merged["atm_iv"] == 0.14
    assert merged["expected_move_pts"] == 180.0

    cands: List[Dict[str, Any]] = []
    obs_ids = {"chain": "OBS-1"}
    int_ids = {"chain": "INT-1"}
    append_chain_analytics_evidence(
        cands, options_analytics=raw, spot=23050.0,
        observatory_state_ids=obs_ids, interpretation_state_ids=int_ids, now_iso="2026-07-23T10:00:00+05:30",
    )
    assert len(cands) >= 6  # regime, pcr, iv, gamma flip, divergence, hedge flow, premium, positioning
    labels = {c["label"] for c in cands}
    assert "GEX regime" in labels
    assert "Gamma flip structure" in labels
    assert all(c["source"]["observatory_state_id"] == "OBS-1" for c in cands)
    assert all(c["quality"] == "inferred" for c in cands)

    # Sparse input (nothing but a regime) -> exactly one candidate, no crash on missing fields.
    sparse: List[Dict[str, Any]] = []
    append_chain_analytics_evidence(
        sparse, options_analytics={"gex_regime": "POSITIVE_GAMMA"}, spot=0.0,
        observatory_state_ids=obs_ids, interpretation_state_ids=int_ids, now_iso="2026-07-23T10:00:00+05:30",
    )
    assert len(sparse) == 1
    assert sparse[0]["label"] == "GEX regime"

    print("[pte.chain_evidence] selftest OK: normalization, replay merge, evidence candidate emission")


if __name__ == "__main__":
    _selftest()
