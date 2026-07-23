#!/usr/bin/env python3
"""Replay historical desk journals through Evidence -> PTE -> EvaluationInput.

Ported faithfully from quant-desk-engine v4/ATLAS's pte_backfill.py
(mentor-authored). No core algorithm changed. Two documented adaptations
were required, both structural (accommodating dependencies deliberately
not ported), not changes to the backfill logic itself:

1. PTE v2 support (pte_version="v2"/"both") is NOT available. This session
   deliberately ported only PTE v1 (see nifty.pte.theory_engine's
   docstring) — the mentor's own v4 tree shows v2 isn't live-wired
   anywhere, and its extra machinery (plugin architecture, its own
   runtime invariant checker) is out of scope. The v2 import is lazy and
   optional here; requesting pte_version="v2"/"both" raises a clear
   NotImplementedError rather than silently degrading or crashing on
   import. pte_version="v1" (the default, matching the source) works
   exactly as ported.

2. `nse_day_status`/`is_nse_trading_day` originally read a full NSE
   trading-holiday calendar module (nse_trading_calendar.py, 344 lines —
   out of scope for this file; it's a separate, larger pending port with
   its own holiday-master data and caching). Adapted to wrap
   nifty.jobs.is_trading_day (already-existing weekday + live NSE
   holiday-master check) into an equivalent status dict instead. Weekday
   and holiday semantics are the same; only the richer status/reason
   labeling (WEEKEND vs HOLIDAY vs CLOSED) is collapsed to a single
   "not a trading day" reason, since nifty.jobs doesn't distinguish them.

Everything else — evidence-candidate construction from desk state, replay
frame iteration (event_replay + timeline modes), backfill file output —
is unchanged.

Not yet wired into the live pipeline (no scheduled job runs this).
Self-check: python -m nifty.pte.backfill
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Literal, Optional, Tuple

from nifty.pte.artifact_ids import PREFIX_INT, PREFIX_OBS, ArtifactIdGenerator
from nifty.pte.evaluation_adapter import EvaluationInputAdapter
from nifty.pte.evidence_engine import EvidenceEngine
from nifty.pte.theory_engine import ParticipantTheoryEngine, slim_participant_theory_set_for_journal
from nifty.pte.chain_evidence import (
    append_chain_analytics_evidence,
    options_analytics_from_replay_snapshot,
)
from nifty.pte.liquidity_evidence import (
    append_market_profile_liquidity_evidence,
    liquidity_read_for_evidence,
    normalize_liquidity_engine,
    normalize_market_profile,
)
from nifty.pte.session_evidence import (
    DayContextBundle,
    append_session_context_evidence,
    desk_context_from_replay_snapshot,
)

PteVersion = Literal["v1", "v2", "both"]

IST = timezone(timedelta(hours=5, minutes=30))
RECORDED_AT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})")


def nse_day_status(session_day: date) -> Dict[str, Any]:
    """NSE trading-day check via nifty.jobs.is_trading_day (see module docstring §2)."""
    from nifty.jobs import is_trading_day

    trading = is_trading_day(session_day)
    return {
        "date": session_day.isoformat(),
        "is_trading_day": trading,
        "status": "TRADING DAY" if trading else "CLOSED",
        "reason": "NSE session scheduled" if trading else "Weekend or NSE trading holiday",
    }


def is_nse_trading_day(session_day: date) -> bool:
    return bool(nse_day_status(session_day).get("is_trading_day"))


def filter_nse_trading_dates(dates: Iterable[str]) -> List[str]:
    return sorted(d for d in dates if is_nse_trading_day(date.fromisoformat(d)))


def discover_backfill_jobs(
    journal_dir: Path,
    *,
    month_prefix: str = "",
    trading_days_only: bool = True,
) -> List[Tuple[str, str]]:
    """Discover replay/timeline backfill jobs; optionally skip NSE closed days."""
    replay = set(discover_backfill_dates(journal_dir, "replay"))
    timeline = set(discover_backfill_dates(journal_dir, "timeline"))
    days = sorted(d for d in (replay | timeline) if not month_prefix or d.startswith(month_prefix))
    if trading_days_only:
        days = filter_nse_trading_dates(days)
    jobs: List[Tuple[str, str]] = []
    for day in days:
        mode = "replay" if day in replay else "timeline"
        jobs.append((day, mode))
    return jobs


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_recorded_at(value: str, session_day: date) -> datetime:
    text = str(value or "").strip()
    match = RECORDED_AT_RE.match(text)
    if match:
        day_s, time_s = match.groups()
        return datetime.fromisoformat(f"{day_s}T{time_s}").replace(tzinfo=IST)
    if "T" in text:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        return dt.astimezone(IST)
    return datetime.combine(session_day, datetime.min.time(), tzinfo=IST)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(IST).isoformat(timespec="seconds")


@dataclass(frozen=True)
class DeskInputs:
    timestamp: datetime
    chain_bias: Dict[str, Any]
    futures_layer: Dict[str, Any]
    market_profile: Dict[str, Any]
    liquidity_engine: Dict[str, Any]
    playbook: Dict[str, Any]
    options_analytics: Dict[str, Any]
    spot: float
    spot_5m_delta: float
    abnormal_alerts: List[Dict[str, Any]]
    strongest_ce_add: List[Dict[str, Any]]
    strongest_pe_add: List[Dict[str, Any]]
    source: str
    desk_context: Dict[str, Any] = field(default_factory=dict)


def chain_bias_from_replay(chain_bias: Dict[str, Any]) -> Dict[str, Any]:
    label = str(chain_bias.get("label") or chain_bias.get("dominant_side") or "")
    if chain_bias.get("dominant_side"):
        return dict(chain_bias)
    dominant = "BALANCED"
    upper = label.upper()
    if "PE" in upper and "CE" not in upper.replace("PE_DOMINANT", ""):
        dominant = "PE"
    elif "CE" in upper:
        dominant = "CE"
    ce = as_float(chain_bias.get("ce_sum_5m"))
    pe = as_float(chain_bias.get("pe_sum_5m"))
    total = ce + pe
    confidence = round(max(ce, pe) / total * 100.0, 1) if total > 0 else 50.0
    return {
        "dominant_side": dominant,
        "confidence_pct": confidence,
        "label": label,
        "ce_sum_5m": ce,
        "pe_sum_5m": pe,
    }


def chain_bias_from_analytics(row: Dict[str, Any]) -> Dict[str, Any]:
    pcr = as_float(row.get("pcr_oi_chain"), default=1.0)
    if pcr > 1.05:
        dominant = "PE"
    elif pcr < 0.95:
        dominant = "CE"
    else:
        dominant = "BALANCED"
    confidence = round(min(100.0, abs(pcr - 1.0) * 200.0), 1)
    return {"dominant_side": dominant, "confidence_pct": confidence, "pcr_oi_chain": pcr}


def desk_inputs_from_replay_snapshot(snapshot: Dict[str, Any], session_day: date) -> DeskInputs:
    ts = parse_recorded_at(str(snapshot.get("recorded_at") or ""), session_day)
    futures = snapshot.get("futures") or {}
    mp = snapshot.get("market_profile") or {}
    liq = snapshot.get("liquidity") or {}
    chain = chain_bias_from_replay(snapshot.get("chain_bias") or {})
    option = snapshot.get("option") or {}
    strongest_pe: List[Dict[str, Any]] = []
    strongest_ce: List[Dict[str, Any]] = []
    strike = as_int(option.get("strike") or option.get("entry_strike"))
    writer_side = str(option.get("writer_side") or option.get("entry_side") or "")
    writer_pct = as_float(option.get("writer_oi_velocity_5m_pct"))
    entry_pct = as_float(option.get("oi_velocity_5m_pct"))
    if strike and writer_side == "PE" and writer_pct > 0:
        strongest_pe = [{"strike": strike, "velocity_5m": {"delta": 0, "pct": writer_pct}}]
    if strike and writer_side == "CE" and writer_pct > 0:
        strongest_ce = [{"strike": strike, "velocity_5m": {"delta": 0, "pct": writer_pct}}]
    spot = as_float(snapshot.get("spot"))
    mp_norm = normalize_market_profile(mp, spot=spot)
    liq_norm = normalize_liquidity_engine(liq)
    oa_norm = options_analytics_from_replay_snapshot(snapshot)
    ctx_norm = desk_context_from_replay_snapshot(snapshot)
    return DeskInputs(
        timestamp=ts,
        chain_bias=chain,
        futures_layer={"front_behavior": str(futures.get("front_behavior") or "UNKNOWN")},
        market_profile=mp_norm,
        liquidity_engine=liq_norm,
        playbook={"phase": str(snapshot.get("market_state") or "UNKNOWN")},
        options_analytics=oa_norm,
        spot=as_float(snapshot.get("spot")),
        spot_5m_delta=as_float(snapshot.get("spot_5m_delta")),
        abnormal_alerts=[],
        strongest_ce_add=strongest_ce,
        strongest_pe_add=strongest_pe,
        source="event_replay",
        desk_context=ctx_norm,
    )


def desk_inputs_from_timeline(
    analytics: Dict[str, Any],
    playbook: Dict[str, Any],
    session_day: date,
) -> DeskInputs:
    ts = parse_recorded_at(str(analytics.get("recorded_at") or ""), session_day)
    return DeskInputs(
        timestamp=ts,
        chain_bias=chain_bias_from_analytics(analytics),
        futures_layer={"front_behavior": "UNKNOWN"},
        market_profile={"balance_state": "UNKNOWN"},
        liquidity_engine={"read": "UNKNOWN"},
        playbook={"phase": str(playbook.get("phase") or analytics.get("phase") or "UNKNOWN")},
        options_analytics=analytics,
        spot=as_float(analytics.get("spot")),
        spot_5m_delta=0.0,
        abnormal_alerts=[],
        strongest_ce_add=[],
        strongest_pe_add=[],
        source="options_analytics",
        desk_context={},
    )


def pte_lane_ids(id_gen: ArtifactIdGenerator, session_day: date) -> Tuple[Dict[str, str], Dict[str, str]]:
    lanes = ["chain", "context", "market_profile", "liquidity", "discovery", "flow"]
    obs = {lane: id_gen.new(PREFIX_OBS, session_day=session_day) for lane in lanes}
    ints = {lane: id_gen.new(PREFIX_INT, session_day=session_day) for lane in lanes}
    return obs, ints


def theory_signature(participant_theory_set: Dict[str, Any]) -> tuple:
    rows = participant_theory_set.get("theories") or []
    return tuple(
        (str(row.get("catalog_id") or ""), int(row.get("strength") or 0), int(row.get("share") or 0))
        for row in rows
    )


def narrative_signature(market_narrative: Dict[str, Any]) -> tuple:
    exp = market_narrative.get("current_explanation") or {}
    parts: List[tuple] = []
    for key in ("market_regime", "price_control", "primary_mechanism"):
        row = exp.get(key) or {}
        parts.append((key, str(row.get("answer_id") or ""), row.get("confidence")))
    for row in (exp.get("liquidity_behaviour") or [])[:2]:
        parts.append(("liq", str(row.get("answer_id") or ""), row.get("confidence")))
    return tuple(parts)


def evidence_signature(candidates: List[Dict[str, Any]]) -> tuple:
    return tuple(str(c.get("observation") or "") for c in candidates)


def build_evidence_candidates(
    *,
    desk: DeskInputs,
    observatory_state_ids: Dict[str, str],
    interpretation_state_ids: Dict[str, str],
) -> List[Dict[str, Any]]:
    now_iso = to_iso(desk.timestamp)
    oa = desk.options_analytics
    expected_move = as_float(oa.get("expected_move_pts"))
    net_gex = as_float(oa.get("net_gex"))
    net_dealer_delta = as_float(oa.get("net_dealer_delta"))
    chain_side = str(desk.chain_bias.get("dominant_side") or "BALANCED")
    chain_conf = as_float(desk.chain_bias.get("confidence_pct"))
    ce_sum_5m = as_float(desk.chain_bias.get("ce_sum_5m"))
    pe_sum_5m = as_float(desk.chain_bias.get("pe_sum_5m"))
    fut_behavior = str(desk.futures_layer.get("front_behavior") or "UNKNOWN")
    mp_balance = str(desk.market_profile.get("balance_state") or "UNKNOWN")
    liq_read = liquidity_read_for_evidence(desk.liquidity_engine)
    phase = str(desk.playbook.get("phase") or "UNKNOWN")
    spot = desk.spot
    spot_v5 = desk.spot_5m_delta

    cands: List[Dict[str, Any]] = [
        {
            "label": "Chain dominant side",
            "source": {
                "observatory_id": "chain",
                "observatory_state_id": observatory_state_ids["chain"],
                "field_path": "chain_bias.dominant_side",
            },
            "observation": f"Dominant side={chain_side} ({chain_conf:.1f}%)",
            "interpretation": "Participant flow side dominance",
            "interpretation_state_id": interpretation_state_ids["chain"],
            "quality": "observed",
            "weight_hint": "strong",
            "theory_hints": {
                "supports": ["premium_writing", "dealer_hedging"],
                "contradicts": ["inventory_transfer"] if chain_side in {"CE", "PE"} else [],
            },
            "effective_at": now_iso,
            "observed_at": now_iso,
        },
        {
            "label": "Net GEX",
            "source": {
                "observatory_id": "chain",
                "observatory_state_id": observatory_state_ids["chain"],
                "field_path": "options_analytics.net_gex",
            },
            "observation": f"Net GEX={net_gex:.4e}",
            "interpretation": "Gamma regime",
            "interpretation_state_id": interpretation_state_ids["chain"],
            "quality": "observed",
            "weight_hint": "strong",
            "theory_hints": {
                "supports": ["breakout_expansion", "dealer_hedging"] if net_gex < 0 else ["range_compression"],
                "contradicts": ["range_compression"] if net_gex < 0 else ["breakout_expansion"],
            },
            "effective_at": now_iso,
            "observed_at": now_iso,
        },
        {
            "label": "Dealer delta posture",
            "source": {
                "observatory_id": "chain",
                "observatory_state_id": observatory_state_ids["chain"],
                "field_path": "options_analytics.net_dealer_delta",
            },
            "observation": f"Net dealer delta={net_dealer_delta:.4e}",
            "interpretation": "Dealer hedge pressure",
            "interpretation_state_id": interpretation_state_ids["chain"],
            "quality": "observed",
            "weight_hint": "moderate",
            "theory_hints": {"supports": ["dealer_hedging"], "contradicts": []},
            "effective_at": now_iso,
            "observed_at": now_iso,
        },
        {
            "label": "Expected move context",
            "source": {
                "observatory_id": "context",
                "observatory_state_id": observatory_state_ids["context"],
                "field_path": "options_analytics.expected_move_pts",
            },
            "observation": f"Expected move={expected_move:.1f} pts",
            "interpretation": "Volatility envelope",
            "interpretation_state_id": interpretation_state_ids["context"],
            "quality": "inferred",
            "weight_hint": "weak",
            "theory_hints": {
                "supports": ["breakout_expansion"] if expected_move >= 250 else ["range_compression"],
                "contradicts": ["range_compression"] if expected_move >= 250 else ["breakout_expansion"],
            },
            "effective_at": now_iso,
            "observed_at": now_iso,
        },
        {
            "label": "Futures behaviour",
            "source": {
                "observatory_id": "context",
                "observatory_state_id": observatory_state_ids["context"],
                "field_path": "futures_layer.front_behavior",
            },
            "observation": f"Front futures behavior={fut_behavior}",
            "interpretation": "Live vs inherited futures relationship",
            "interpretation_state_id": interpretation_state_ids["context"],
            "quality": "observed",
            "weight_hint": "moderate",
            "theory_hints": {"supports": ["dealer_hedging", "inventory_transfer"], "contradicts": []},
            "effective_at": now_iso,
            "observed_at": now_iso,
        },
        {
            "label": "Market profile balance",
            "source": {
                "observatory_id": "market_profile",
                "observatory_state_id": observatory_state_ids["market_profile"],
                "field_path": "market_profile.balance_state",
            },
            "observation": f"Balance state={mp_balance}",
            "interpretation": "Auction acceptance state",
            "interpretation_state_id": interpretation_state_ids["market_profile"],
            "quality": "inferred",
            "weight_hint": "moderate",
            "theory_hints": {
                "supports": ["range_compression"] if "BALANCE" in mp_balance.upper() else ["breakout_expansion"],
                "contradicts": ["breakout_expansion"] if "BALANCE" in mp_balance.upper() else ["range_compression"],
            },
            "effective_at": now_iso,
            "observed_at": now_iso,
        },
        {
            "label": "Liquidity read",
            "source": {
                "observatory_id": "liquidity",
                "observatory_state_id": observatory_state_ids["liquidity"],
                "field_path": "liquidity_engine.read",
            },
            "observation": f"Liquidity read={liq_read}",
            "interpretation": "Sweep/structure context",
            "interpretation_state_id": interpretation_state_ids["liquidity"],
            "quality": "inferred",
            "weight_hint": "moderate",
            "theory_hints": {"supports": ["inventory_transfer", "dealer_hedging"], "contradicts": []},
            "effective_at": now_iso,
            "observed_at": now_iso,
        },
        {
            "label": "Intraday playbook phase",
            "source": {
                "observatory_id": "discovery",
                "observatory_state_id": observatory_state_ids["discovery"],
                "field_path": "intraday_playbook.phase",
            },
            "observation": f"Playbook phase={phase}",
            "interpretation": "Session progression state",
            "interpretation_state_id": interpretation_state_ids["discovery"],
            "quality": "inferred",
            "weight_hint": "weak",
            "theory_hints": {"supports": ["inventory_transfer"], "contradicts": []},
            "effective_at": now_iso,
            "observed_at": now_iso,
        },
        {
            "label": "Spot context",
            "source": {
                "observatory_id": "context",
                "observatory_state_id": observatory_state_ids["context"],
                "field_path": "spot",
            },
            "observation": f"Spot={spot:.2f} · 5m Δ={spot_v5:+.2f}",
            "interpretation": "Live spot displacement",
            "interpretation_state_id": interpretation_state_ids["context"],
            "quality": "observed",
            "weight_hint": "moderate",
            "theory_hints": {
                "supports": ["breakout_expansion"] if abs(spot_v5) >= 15 else ["range_compression"],
                "contradicts": ["range_compression"] if abs(spot_v5) >= 15 else ["breakout_expansion"],
            },
            "effective_at": now_iso,
            "observed_at": now_iso,
        },
    ]

    append_market_profile_liquidity_evidence(
        cands,
        market_profile=desk.market_profile,
        liquidity_engine=desk.liquidity_engine,
        spot=spot,
        observatory_state_ids=observatory_state_ids,
        interpretation_state_ids=interpretation_state_ids,
        now_iso=now_iso,
    )
    append_chain_analytics_evidence(
        cands,
        options_analytics=desk.options_analytics,
        spot=spot,
        observatory_state_ids=observatory_state_ids,
        interpretation_state_ids=interpretation_state_ids,
        now_iso=now_iso,
    )
    append_session_context_evidence(
        cands,
        desk_context=desk.desk_context,
        spot=spot,
        observatory_state_ids=observatory_state_ids,
        interpretation_state_ids=interpretation_state_ids,
        now_iso=now_iso,
    )

    if ce_sum_5m > 0 or pe_sum_5m > 0:
        cands.append(
            {
                "label": "Chain OI velocity sums",
                "source": {
                    "observatory_id": "chain",
                    "observatory_state_id": observatory_state_ids["chain"],
                    "field_path": "chain_bias.oi_velocity_sums",
                },
                "observation": f"CE 5m OI norm sum={ce_sum_5m:.2f} · PE sum={pe_sum_5m:.2f}",
                "interpretation": "Normalized 5m OI velocity by side",
                "interpretation_state_id": interpretation_state_ids["chain"],
                "quality": "observed",
                "weight_hint": "strong" if max(ce_sum_5m, pe_sum_5m) >= 500 else "moderate",
                "theory_hints": {
                    "supports": (
                        ["premium_writing", "dealer_hedging"]
                        if ce_sum_5m > pe_sum_5m
                        else ["premium_writing", "premium_buying", "dealer_hedging"]
                    ),
                    "contradicts": [],
                },
                "effective_at": now_iso,
                "observed_at": now_iso,
            }
        )

    alerts = list(desk.abnormal_alerts or [])[:5]
    alert_count = len(alerts)
    top = alerts[0] if alerts else {}
    top_dir = str(top.get("direction") or "NONE")
    top_strike = as_int(top.get("strike"))
    top_type = str(top.get("option_type") or "")
    writer_hint = "WRITER" in top_dir.upper()
    cands.append(
        {
            "label": "Abnormal OI activity",
            "source": {
                "observatory_id": "flow",
                "observatory_state_id": observatory_state_ids["flow"],
                "field_path": "abnormal_alerts",
            },
            "observation": (
                f"Alerts={alert_count}"
                + (f" · top={top_strike}{top_type} {top_dir}" if alert_count else " · none active")
            ),
            "interpretation": "Strike-level OI velocity outliers (identity not assumed)",
            "interpretation_state_id": interpretation_state_ids["flow"],
            "quality": "observed",
            "weight_hint": "strong" if alert_count else "weak",
            "theory_hints": {
                "supports": (
                    ["premium_writing", "inventory_transfer"]
                    if writer_hint
                    else (["premium_buying", "inventory_transfer", "dealer_hedging"] if alert_count else [])
                ),
                "contradicts": [],
            },
            "effective_at": now_iso,
            "observed_at": now_iso,
        }
    )

    if desk.strongest_pe_add:
        row = desk.strongest_pe_add[0]
        v5 = row.get("velocity_5m") or {}
        cands.append(
            {
                "label": "Strongest PE OI add",
                "source": {
                    "observatory_id": "flow",
                    "observatory_state_id": observatory_state_ids["flow"],
                    "field_path": "strongest_pe_add",
                },
                "observation": (
                    f"PE {row.get('strike')} 5m Δ={as_int(v5.get('delta'))} ({as_float(v5.get('pct')):+.2f}%)"
                ),
                "interpretation": "Leading PE-side open-interest build",
                "interpretation_state_id": interpretation_state_ids["flow"],
                "quality": "observed",
                "weight_hint": "moderate",
                "theory_hints": {
                    "supports": ["premium_writing", "dealer_hedging", "inventory_transfer"],
                    "contradicts": [],
                },
                "effective_at": now_iso,
                "observed_at": now_iso,
            }
        )
    if desk.strongest_ce_add:
        row = desk.strongest_ce_add[0]
        v5 = row.get("velocity_5m") or {}
        cands.append(
            {
                "label": "Strongest CE OI add",
                "source": {
                    "observatory_id": "flow",
                    "observatory_state_id": observatory_state_ids["flow"],
                    "field_path": "strongest_ce_add",
                },
                "observation": (
                    f"CE {row.get('strike')} 5m Δ={as_int(v5.get('delta'))} ({as_float(v5.get('pct')):+.2f}%)"
                ),
                "interpretation": "Leading CE-side open-interest build",
                "interpretation_state_id": interpretation_state_ids["flow"],
                "quality": "observed",
                "weight_hint": "moderate",
                "theory_hints": {
                    "supports": ["premium_writing", "dealer_hedging", "inventory_transfer"],
                    "contradicts": [],
                },
                "effective_at": now_iso,
                "observed_at": now_iso,
            }
        )
    return cands


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def discover_backfill_dates(journal_dir: Path, mode: str) -> List[str]:
    if mode == "replay":
        pattern = "event_replay_*.jsonl"
    else:
        pattern = "nifty_options_analytics_*.jsonl"
    dates: set[str] = set()
    for path in journal_dir.glob(pattern):
        match = re.search(r"(\d{4}-\d{2}-\d{2})", path.name)
        if match:
            dates.add(match.group(1))
    return sorted(dates)


def iter_replay_frames(path: Path, session_day: date, *, t0_only: bool = True) -> Iterator[DeskInputs]:
    seen: set[str] = set()
    for row in load_jsonl(path):
        if row.get("event") != "EVENT_REPLAY":
            continue
        if t0_only and str(row.get("label") or "") != "T0":
            continue
        snapshot = row.get("snapshot") or {}
        key = str(snapshot.get("recorded_at") or row.get("recorded_at") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        yield desk_inputs_from_replay_snapshot(snapshot, session_day)


def iter_timeline_frames(
    analytics_path: Path,
    playbook_path: Path,
    session_day: date,
    *,
    session_only: bool = True,
) -> Iterator[DeskInputs]:
    analytics_rows = load_jsonl(analytics_path)
    playbook_rows = [
        row for row in load_jsonl(playbook_path) if str(row.get("event") or "") == "PLAYBOOK_PHASE"
    ]
    playbook_rows.sort(key=lambda r: parse_recorded_at(str(r.get("recorded_at") or ""), session_day))

    def phase_at(ts: datetime) -> Dict[str, Any]:
        chosen: Dict[str, Any] = {"phase": "UNKNOWN"}
        for row in playbook_rows:
            row_ts = parse_recorded_at(str(row.get("recorded_at") or ""), session_day)
            if row_ts <= ts:
                chosen = row
            else:
                break
        return chosen

    for row in analytics_rows:
        if row.get("event") != "OPTIONS_ANALYTICS":
            continue
        ts = parse_recorded_at(str(row.get("recorded_at") or ""), session_day)
        if session_only:
            start = ts.replace(hour=9, minute=15, second=0, microsecond=0)
            end = ts.replace(hour=15, minute=30, second=0, microsecond=0)
            if ts < start or ts > end:
                continue
        yield desk_inputs_from_timeline(row, phase_at(ts), session_day)


@dataclass
class BackfillResult:
    day: str
    mode: str
    frames: int
    evidence_sets: int
    evidence_records: int
    theory_sets: int
    evaluation_inputs: int
    narrative_sets: int = 0
    first_timestamp: str = ""
    last_timestamp: str = ""
    output_files: Dict[str, str] = field(default_factory=dict)
    skipped: bool = False
    skip_reason: str = ""


def run_backfill_day(
    *,
    journal_dir: Path,
    session_day: date,
    mode: str = "replay",
    suffix: str = "backfill",
    overwrite: bool = True,
    min_interval_sec: float = 0.0,
    pte_version: PteVersion = "v1",
    morning_context_policy: str = "baseline",
    morning_context_expiry: str = "09:45",
) -> BackfillResult:
    day_s = session_day.isoformat()
    cal = nse_day_status(session_day)
    if not cal.get("is_trading_day"):
        reason = str(cal.get("reason") or cal.get("status") or "NSE closed")
        return BackfillResult(
            day=day_s,
            mode=mode,
            frames=0,
            evidence_sets=0,
            evidence_records=0,
            theory_sets=0,
            evaluation_inputs=0,
            narrative_sets=0,
            skipped=True,
            skip_reason=reason,
        )
    if mode == "replay":
        source_path = journal_dir / f"event_replay_{day_s}.jsonl"
        if not source_path.exists():
            raise FileNotFoundError(f"Missing replay journal: {source_path}")
        frames = list(iter_replay_frames(source_path, session_day, t0_only=True))
    elif mode == "timeline":
        analytics_path = journal_dir / f"nifty_options_analytics_{day_s}.jsonl"
        playbook_path = journal_dir / f"nifty_playbook_{day_s}.jsonl"
        if not analytics_path.exists():
            raise FileNotFoundError(f"Missing analytics journal: {analytics_path}")
        frames = list(
            iter_timeline_frames(
                analytics_path,
                playbook_path,
                session_day,
                session_only=True,
            )
        )
    else:
        raise ValueError(f"Unsupported mode: {mode}")

    frames.sort(key=lambda f: f.timestamp)
    context_bundle = DayContextBundle.load(journal_dir, session_day)
    if context_bundle.morning or context_bundle.session_rows or context_bundle.eod.get("loaded"):
        frames = [
            replace(
                frame,
                desk_context=context_bundle.desk_context_at(
                    frame.timestamp,
                    session_day,
                    spot=frame.spot,
                    futures_layer=frame.futures_layer,
                ),
            )
            for frame in frames
        ]
    if min_interval_sec > 0 and frames:
        throttled: List[DeskInputs] = []
        last_ts: Optional[datetime] = None
        for frame in frames:
            if last_ts is None or (frame.timestamp - last_ts).total_seconds() >= min_interval_sec:
                throttled.append(frame)
                last_ts = frame.timestamp
        frames = throttled

    id_gen = ArtifactIdGenerator()
    evidence_engine = EvidenceEngine(id_generator=id_gen)
    run_v1 = pte_version in ("v1", "both")
    run_v2 = pte_version in ("v2", "both")
    if run_v2:
        raise NotImplementedError(
            "PTE v2 was deliberately not ported this session (not live-wired anywhere "
            "in the mentor's own v4 source, and its plugin/invariant machinery is out "
            "of scope). Only pte_version='v1' is supported — see nifty.pte.backfill's "
            "module docstring."
        )
    pte_engine = ParticipantTheoryEngine(id_generator=id_gen) if run_v1 else None
    pte_v2_engine = None
    evi_adapter = EvaluationInputAdapter(id_generator=id_gen)
    obs_ids, int_ids = pte_lane_ids(id_gen, session_day)

    tag = suffix.strip() or "backfill"
    paths = {
        "evidence_sets": journal_dir / f"evidence_sets_{day_s}_{tag}.jsonl",
        "evidence_records": journal_dir / f"evidence_records_{day_s}_{tag}.jsonl",
        "participant_theory_set": journal_dir / f"participant_theory_set_{day_s}_{tag}.jsonl",
        "evaluation_input": journal_dir / f"evaluation_input_{day_s}_{tag}.jsonl",
    }
    if run_v2:
        paths["market_narrative"] = journal_dir / f"market_narrative_{day_s}_{tag}.jsonl"
    if overwrite:
        for path in paths.values():
            if path.exists():
                path.unlink()

    scope = {
        "session_date": day_s,
        "instrument": "NIFTY",
        "observatory_scope": "weekly_current",
        "backfill_mode": mode,
        "backfill_source": frames[0].source if frames else mode,
    }

    previous_theory: Optional[Dict[str, Any]] = None
    previous_narrative: Optional[Dict[str, Any]] = None
    last_evidence_sig: Optional[tuple] = None
    last_theory_sig: Optional[tuple] = None
    last_narrative_sig: Optional[tuple] = None
    counts = {
        "evidence_sets": 0,
        "evidence_records": 0,
        "theory_sets": 0,
        "evaluation_inputs": 0,
        "narrative_sets": 0,
    }

    def append_row(path: Path, row: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, default=str, separators=(",", ":")) + "\n")

    for desk in frames:
        ts_iso = to_iso(desk.timestamp)
        candidates = build_evidence_candidates(
            desk=desk,
            observatory_state_ids=obs_ids,
            interpretation_state_ids=int_ids,
        )
        ev_sig = evidence_signature(candidates)
        if ev_sig == last_evidence_sig:
            continue
        last_evidence_sig = ev_sig

        evidence_set, new_records = evidence_engine.publish(
            scope=scope,
            observatory_state_ids=list(obs_ids.values()),
            interpretation_state_ids=list(int_ids.values()),
            evidence_candidates=candidates,
            publish_reason="backfill",
            session_day=session_day,
            timestamp=ts_iso,
        )
        records = evidence_engine.records_for_ids(evidence_set.evidence_record_ids)

        pth_dict: Optional[Dict[str, Any]] = None
        if run_v1 and pte_engine is not None:
            pth = pte_engine.infer(
                evidence_set_id=evidence_set.id,
                evidence_engine_version=evidence_set.evidence_engine_version,
                evidence_records=records,
                scope=scope,
                previous_set=previous_theory,
                active_theory_count=5,
                timestamp=ts_iso,
            )
            pth_dict = pth.to_dict()
            pth_dict.setdefault("meta", {})
            pth_dict["meta"]["backfill_mode"] = mode

        mna_dict: Optional[Dict[str, Any]] = None
        if run_v2 and pte_v2_engine is not None:
            mna_dict = pte_v2_engine.infer(
                evidence_set_id=evidence_set.id,
                evidence_engine_version=evidence_set.evidence_engine_version,
                evidence_records=records,
                scope=scope,
                previous_narrative=previous_narrative,
                timestamp=ts_iso,
                publish_reason="backfill",
            )
            mna_dict.setdefault("meta", {})
            mna_dict["meta"]["backfill_mode"] = mode

        theory_sig = theory_signature(pth_dict) if pth_dict else None
        evi = None
        evi_dict: Optional[Dict[str, Any]] = None
        if pth_dict is not None:
            evi = evi_adapter.build(
                participant_theory_set=pth_dict,
                evidence_set_id=evidence_set.id,
                top_n=3,
                adapter_policy="backfill_v1",
                interpretation_state_ids=list(int_ids.values()),
                timestamp=ts_iso,
            )
            evi_dict = evi.to_dict()
            evi_dict.setdefault("meta", {})
            evi_dict["meta"]["backfill_mode"] = mode

        append_row(paths["evidence_sets"], evidence_set.to_dict())
        counts["evidence_sets"] += 1
        for rec in new_records:
            append_row(paths["evidence_records"], rec.to_dict())
            counts["evidence_records"] += 1

        if pth_dict is not None and theory_sig != last_theory_sig:
            append_row(
                paths["participant_theory_set"],
                slim_participant_theory_set_for_journal(pth_dict),
            )
            if evi_dict is not None:
                append_row(paths["evaluation_input"], evi_dict)
            counts["theory_sets"] += 1
            counts["evaluation_inputs"] += 1
            last_theory_sig = theory_sig

        if mna_dict is not None:
            narrative_sig = narrative_signature(mna_dict)
            if narrative_sig != last_narrative_sig:
                append_row(paths["market_narrative"], mna_dict)
                counts["narrative_sets"] += 1
                last_narrative_sig = narrative_sig

        if pth_dict is not None:
            previous_theory = pth_dict
        if mna_dict is not None:
            previous_narrative = mna_dict

    first_ts = to_iso(frames[0].timestamp) if frames else ""
    last_ts = to_iso(frames[-1].timestamp) if frames else ""
    return BackfillResult(
        day=day_s,
        mode=mode,
        frames=len(frames),
        evidence_sets=counts["evidence_sets"],
        evidence_records=counts["evidence_records"],
        theory_sets=counts["theory_sets"],
        evaluation_inputs=counts["evaluation_inputs"],
        narrative_sets=counts["narrative_sets"],
        first_timestamp=first_ts,
        last_timestamp=last_ts,
        output_files={k: str(v) for k, v in paths.items()},
    )


def _selftest() -> None:
    import tempfile

    day = date(2026, 7, 21)  # a Tuesday
    weekend = date(2026, 7, 25)  # a Saturday

    assert is_nse_trading_day(weekend) is False
    status = nse_day_status(weekend)
    assert status["is_trading_day"] is False

    assert chain_bias_from_replay({"dominant_side": "CE", "confidence_pct": 70.0}) == {
        "dominant_side": "CE", "confidence_pct": 70.0,
    }
    inferred = chain_bias_from_replay({"label": "PE_DOMINANT_CHAIN", "ce_sum_5m": 10.0, "pe_sum_5m": 40.0})
    assert inferred["dominant_side"] == "PE"
    assert inferred["confidence_pct"] == 80.0

    assert chain_bias_from_analytics({"pcr_oi_chain": 1.3})["dominant_side"] == "PE"
    assert chain_bias_from_analytics({"pcr_oi_chain": 0.7})["dominant_side"] == "CE"
    assert chain_bias_from_analytics({"pcr_oi_chain": 1.0})["dominant_side"] == "BALANCED"

    ts = parse_recorded_at("2026-07-21 10:00:00", day)
    assert ts.hour == 10
    assert to_iso(ts).startswith("2026-07-21T10:00:00")

    tmp = Path(tempfile.mkdtemp(prefix="pte-backfill-selftest-"))
    replay_path = tmp / f"event_replay_{day.isoformat()}.jsonl"
    snapshot_rows = [
        {
            "event": "EVENT_REPLAY", "label": "T0",
            "snapshot": {
                "recorded_at": "2026-07-21 10:00:00", "spot": 23050.0, "spot_5m_delta": 12.0,
                "chain_bias": {"dominant_side": "PE", "confidence_pct": 65.0},
                "options_analytics": {"net_gex_cr": -8.0, "gex_regime": "NEGATIVE_GAMMA", "expected_move_pts": 260.0},
                "market_profile": {"poc": 23000.0, "balance_state": "BALANCED_ROTATION"},
                "liquidity": {"liquidity_grab": "NONE"},
                "futures": {"front_behavior": "CONFIRMING"},
            },
        },
        {
            "event": "EVENT_REPLAY", "label": "T0",
            "snapshot": {
                "recorded_at": "2026-07-21 10:05:00", "spot": 23070.0, "spot_5m_delta": 20.0,
                "chain_bias": {"dominant_side": "PE", "confidence_pct": 70.0},
                "options_analytics": {"net_gex_cr": -9.0, "gex_regime": "NEGATIVE_GAMMA", "expected_move_pts": 270.0},
                "market_profile": {"poc": 23010.0, "balance_state": "BALANCED_ROTATION"},
                "liquidity": {"liquidity_grab": "NONE"},
                "futures": {"front_behavior": "CONFIRMING"},
            },
        },
    ]
    with replay_path.open("w", encoding="utf-8") as fh:
        for row in snapshot_rows:
            fh.write(json.dumps(row) + "\n")

    dates = discover_backfill_dates(tmp, "replay")
    assert dates == [day.isoformat()]

    frames = list(iter_replay_frames(replay_path, day, t0_only=True))
    assert len(frames) == 2
    assert frames[0].spot == 23050.0

    result = run_backfill_day(journal_dir=tmp, session_day=day, mode="replay", suffix="selftest")
    assert result.skipped is False
    assert result.frames == 2
    assert result.evidence_sets >= 1
    assert result.theory_sets >= 1
    assert Path(result.output_files["evidence_sets"]).exists()
    assert Path(result.output_files["participant_theory_set"]).exists()

    # A closed day (weekend) is skipped cleanly, never raises, no output files written.
    skip_result = run_backfill_day(journal_dir=tmp, session_day=weekend, mode="replay", suffix="selftest")
    assert skip_result.skipped is True
    assert skip_result.frames == 0

    # Requesting PTE v2 raises a clear, documented error rather than crashing on import.
    try:
        run_backfill_day(journal_dir=tmp, session_day=day, mode="replay", pte_version="v2")
        raise AssertionError("expected NotImplementedError for pte_version='v2'")
    except NotImplementedError as exc:
        assert "not ported" in str(exc)

    # A day with no journal at all raises FileNotFoundError, not a silent empty result.
    empty_dir = Path(tempfile.mkdtemp(prefix="pte-backfill-empty-"))
    try:
        run_backfill_day(journal_dir=empty_dir, session_day=day, mode="replay")
        raise AssertionError("expected FileNotFoundError for missing replay journal")
    except FileNotFoundError:
        pass

    print("[pte.backfill] selftest OK: trading-day gate, replay iteration, full backfill run, v2 guard")


if __name__ == "__main__":
    _selftest()
