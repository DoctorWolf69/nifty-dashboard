#!/usr/bin/env python3
"""
Active Trade Thesis — persist and monitor qualified signals until entered / invalidated / expired.

Ported faithfully from quant-desk-engine v4/ATLAS's nifty_active_thesis.py
(mentor-authored). No logic changed. Fully self-contained in the source
too (only stdlib `time`), so no import adaptation was needed.

Desk rule: do not discard a valid thesis when abnormal OI alerts stop
firing. This is a separate lifecycle namespace from the Participant Theory
Engine's theory lifecycle (nifty.pte.theory_engine) — the two must never be
conflated per the mentor's own PTE freeze doc.

Reads/writes the same "watch" dict shape produced by this session's
earlier port of entry_conviction.py's start_entry_watch/update_entry_watch
(entry_status, entry_confirmed, rejected, conviction_score,
conviction_label, message, evidence, last_tick_class) — verified field-name
compatible with that module.

Not yet wired into the live pipeline.
Self-check: python -m nifty.analytics.active_thesis
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

THESIS_STATUS_WATCHING = "WATCHING"
THESIS_STATUS_BUILDING = "BUILDING"
THESIS_STATUS_CONFIRMED = "CONFIRMED"
THESIS_STATUS_INVALIDATED = "INVALIDATED"
THESIS_STATUS_EXPIRED = "EXPIRED"
THESIS_STATUS_ENTERED = "ENTERED"

TERMINAL_STATUSES = frozenset(
    {THESIS_STATUS_INVALIDATED, THESIS_STATUS_EXPIRED, THESIS_STATUS_ENTERED}
)

# Max watch life without entry (desk session bound)
THESIS_MAX_LIFE_SEC = 7200
# Journal THESIS_UPDATED at most once per minute per thesis
THESIS_UPDATE_JOURNAL_SEC = 60


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def thesis_id_for(signal_key: str, created_at: str) -> str:
    return f"{signal_key}:{created_at[:19]}"


def is_thesis_terminal(thesis: Optional[Dict[str, Any]]) -> bool:
    if not thesis:
        return True
    return str(thesis.get("status") or "") in TERMINAL_STATUSES


def spot_excursion_pts(decision: str, spot_at_create: float, spot_now: float) -> Tuple[float, float]:
    """Return (favorable_pts, adverse_pts) relative to thesis decision."""
    if spot_at_create <= 0 or spot_now <= 0:
        return 0.0, 0.0
    delta = spot_now - spot_at_create
    if decision == "BUY_PE":
        return max(0.0, -delta), max(0.0, delta)
    if decision == "BUY_CE":
        return max(0.0, delta), max(0.0, -delta)
    return 0.0, 0.0


def create_active_thesis(
    *,
    candidate: Dict[str, Any],
    watch: Dict[str, Any],
    spot: float,
    now_ts: Optional[float] = None,
) -> Dict[str, Any]:
    """Promote a trade-eligible candidate into an Active Trade Thesis."""
    now_ts = now_ts or time.time()
    created_at = str(candidate.get("evaluated_at") or "")
    signal_key = str(candidate.get("signal_key") or "")
    intent = (candidate.get("intent_filter") or {}).get("intent")
    return {
        "thesis_id": thesis_id_for(signal_key, created_at or time.strftime("%Y-%m-%d %H:%M:%S")),
        "signal_key": signal_key,
        "decision": str(candidate.get("decision") or ""),
        "strike": _as_int(candidate.get("strike")),
        "writer_side": str(candidate.get("writer_side") or ""),
        "entry_side": str(candidate.get("entry_side") or ""),
        "writer_contract": candidate.get("writer_contract"),
        "entry_contract": candidate.get("entry_contract"),
        "status": THESIS_STATUS_BUILDING if watch.get("entry_status") == "CONVICTION_BUILDING" else THESIS_STATUS_WATCHING,
        "created_at": created_at,
        "created_ts": now_ts,
        "last_tick_at": created_at,
        "last_tick_ts": now_ts,
        "alert_last_seen_at": created_at,
        "alert_last_seen_ts": now_ts,
        "alert_quiet_sec": 0,
        "spot_at_create": spot,
        "spot_now": spot,
        "mfe_spot_pts": 0.0,
        "mae_spot_pts": 0.0,
        "trade_score": _as_int(candidate.get("total_score")),
        "trade_grade": candidate.get("grade"),
        "paper_eligible": bool(candidate.get("paper_eligible")),
        "intent": intent,
        "blockers": list(candidate.get("blockers") or []),
        "supporting_evidence": list((watch.get("evidence") or [])),
        "contradicting_evidence": [],
        "watch": dict(watch),
        "candidate_snapshot": {
            "total_score": candidate.get("total_score"),
            "grade": candidate.get("grade"),
            "dimensions": candidate.get("dimensions"),
            "entry_price": candidate.get("entry_price"),
            "target_price": candidate.get("target_price"),
            "intent_filter": candidate.get("intent_filter"),
        },
        "last_journal_ts": 0.0,
    }


def sync_thesis_from_watch(
    thesis: Dict[str, Any],
    *,
    watch: Dict[str, Any],
    spot: float,
    alert_present: bool,
    now_ts: Optional[float] = None,
    trade_score: Optional[int] = None,
    paper_eligible: Optional[bool] = None,
    intent: Optional[str] = None,
    blockers: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Advance thesis metadata from latest conviction tick and market context."""
    now_ts = now_ts or time.time()
    thesis["watch"] = dict(watch)
    thesis["last_tick_ts"] = now_ts
    thesis["spot_now"] = spot

    fav, adv = spot_excursion_pts(
        str(thesis.get("decision") or ""),
        _as_float(thesis.get("spot_at_create")),
        spot,
    )
    thesis["mfe_spot_pts"] = max(_as_float(thesis.get("mfe_spot_pts")), fav)
    thesis["mae_spot_pts"] = max(_as_float(thesis.get("mae_spot_pts")), adv)

    if alert_present:
        thesis["alert_last_seen_ts"] = now_ts
        thesis["alert_quiet_sec"] = 0
    else:
        last_alert = _as_float(thesis.get("alert_last_seen_ts"), now_ts)
        thesis["alert_quiet_sec"] = int(max(0.0, now_ts - last_alert))

    if trade_score is not None:
        thesis["trade_score"] = trade_score
    if paper_eligible is not None:
        thesis["paper_eligible"] = paper_eligible
    if intent is not None:
        thesis["intent"] = intent
    if blockers is not None:
        thesis["blockers"] = list(blockers)

    evidence = list(watch.get("evidence") or [])
    tick_class = str(watch.get("last_tick_class") or "")
    if tick_class == "BUILDING":
        thesis["supporting_evidence"] = evidence[-6:]
    elif tick_class in {"WEAKENING", "BROKEN"}:
        thesis["contradicting_evidence"] = evidence[-6:]

    entry_status = str(watch.get("entry_status") or "")
    if watch.get("rejected"):
        thesis["status"] = THESIS_STATUS_INVALIDATED
        thesis["invalidation_reason"] = watch.get("entry_status") or watch.get("message")
    elif watch.get("entry_confirmed"):
        thesis["status"] = THESIS_STATUS_CONFIRMED
    elif entry_status == "CONVICTION_BUILDING":
        thesis["status"] = THESIS_STATUS_BUILDING
    else:
        thesis["status"] = THESIS_STATUS_WATCHING

    age_sec = now_ts - _as_float(thesis.get("created_ts"), now_ts)
    if age_sec >= THESIS_MAX_LIFE_SEC and thesis["status"] not in TERMINAL_STATUSES:
        thesis["status"] = THESIS_STATUS_EXPIRED
        thesis["expiry_reason"] = f"max_life_{THESIS_MAX_LIFE_SEC}s"

    return thesis


def expire_thesis(thesis: Dict[str, Any], reason: str) -> Dict[str, Any]:
    thesis["status"] = THESIS_STATUS_EXPIRED
    thesis["expiry_reason"] = reason
    return thesis


def mark_thesis_entered(thesis: Dict[str, Any], *, paper_signal_id: int) -> Dict[str, Any]:
    thesis["status"] = THESIS_STATUS_ENTERED
    thesis["paper_signal_id"] = paper_signal_id
    thesis["entry_reason"] = (
        f"Trade score {thesis.get('trade_score')} · "
        f"conviction {((thesis.get('watch') or {}).get('conviction_score'))} · "
        f"participant continuation confirmed"
    )
    return thesis


def thesis_to_api(thesis: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Flatten watch fields for dashboard API."""
    if not thesis:
        return None
    watch = thesis.get("watch") or {}
    return {
        **thesis,
        "conviction_score": watch.get("conviction_score"),
        "conviction_label": watch.get("conviction_label"),
        "entry_status": watch.get("entry_status"),
        "entry_confirmed": watch.get("entry_confirmed"),
        "entry_message": watch.get("message"),
        "evidence": watch.get("evidence"),
        "rejected": watch.get("rejected"),
        "alert_active": _as_int(thesis.get("alert_quiet_sec")) < 90,
    }


def should_journal_update(thesis: Dict[str, Any], now_ts: float) -> bool:
    last = _as_float(thesis.get("last_journal_ts"))
    return (now_ts - last) >= THESIS_UPDATE_JOURNAL_SEC


def writer_row_as_alert(writer_row: Dict[str, Any], *, writer_side: str) -> Dict[str, Any]:
    """Minimal alert payload for paper entry when abnormal alert queue is empty."""
    raw_direction = str(writer_row.get("direction") or "OI ADDING")
    if raw_direction == "WRITERS ADDING":
        direction = "OI ADDING"
    elif "OI ADDING" in raw_direction:
        direction = raw_direction
    else:
        direction = "OI ADDING"
    return {
        **writer_row,
        "option_type": writer_side,
        "direction": direction,
        "key_area_reasons": writer_row.get("key_area_reasons") or [],
        "reason": writer_row.get("reason") or "Active thesis — monitoring strike from live chain",
    }


def restore_thesis_from_journal_rows(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Rebuild open thesis from today's journal (last non-terminal create/update)."""
    open_thesis: Optional[Dict[str, Any]] = None
    for row in rows:
        event = str(row.get("event") or "")
        tid = str(row.get("thesis_id") or "")
        if event == "THESIS_CREATED":
            open_thesis = {
                "thesis_id": tid,
                "signal_key": row.get("signal_key"),
                "decision": row.get("decision"),
                "strike": row.get("strike"),
                "writer_side": row.get("writer_side"),
                "entry_side": row.get("entry_side"),
                "writer_contract": row.get("writer_contract"),
                "entry_contract": row.get("entry_contract"),
                "status": row.get("status") or THESIS_STATUS_WATCHING,
                "created_at": row.get("created_at") or row.get("recorded_at"),
                "created_ts": _as_float(row.get("created_ts")),
                "spot_at_create": _as_float(row.get("spot_at_create")),
                "watch": row.get("watch") or {},
                "candidate_snapshot": row.get("candidate_snapshot") or {},
                "mfe_spot_pts": _as_float(row.get("mfe_spot_pts")),
                "mae_spot_pts": _as_float(row.get("mae_spot_pts")),
                "alert_last_seen_ts": _as_float(row.get("alert_last_seen_ts")),
            }
        elif event == "THESIS_UPDATED" and open_thesis and open_thesis.get("thesis_id") == tid:
            open_thesis.update(
                {
                    k: row[k]
                    for k in (
                        "status",
                        "spot_now",
                        "mfe_spot_pts",
                        "mae_spot_pts",
                        "trade_score",
                        "paper_eligible",
                        "intent",
                        "blockers",
                        "alert_quiet_sec",
                        "watch",
                    )
                    if k in row
                }
            )
        elif event in {"THESIS_INVALIDATED", "THESIS_EXPIRED", "THESIS_ENTERED"}:
            if open_thesis and open_thesis.get("thesis_id") == tid:
                open_thesis = None
    if open_thesis and not is_thesis_terminal(open_thesis):
        return open_thesis
    return None


def _selftest() -> None:
    candidate = {
        "evaluated_at": "2026-07-23 10:00:00",
        "signal_key": "23000:PE:BUY_CE",
        "decision": "BUY_CE",
        "strike": 23000,
        "writer_side": "PE",
        "entry_side": "CE",
        "writer_contract": "NIFTY23000PE",
        "entry_contract": "NIFTY23000CE",
        "total_score": 78,
        "grade": "B",
        "paper_eligible": True,
        "intent_filter": {"intent": "CONTINUATION"},
        "blockers": [],
    }
    watch = {
        "entry_status": "WAITING_FOR_CONVICTION",
        "conviction_score": 50,
        "evidence": ["seed"],
    }
    thesis = create_active_thesis(candidate=candidate, watch=watch, spot=23050.0, now_ts=1000.0)
    assert thesis["status"] == THESIS_STATUS_WATCHING
    assert thesis["thesis_id"] == "23000:PE:BUY_CE:2026-07-23 10:00:00"
    assert thesis["spot_at_create"] == 23050.0
    assert is_thesis_terminal(thesis) is False

    fav, adv = spot_excursion_pts("BUY_CE", 23050.0, 23070.0)
    assert fav == 20.0 and adv == 0.0
    fav2, adv2 = spot_excursion_pts("BUY_PE", 23050.0, 23070.0)
    assert fav2 == 0.0 and adv2 == 20.0

    # Building tick — conviction score climbing, still no confirm/reject.
    watch_building = {
        "entry_status": "CONVICTION_BUILDING",
        "conviction_score": 65,
        "evidence": ["writer add"],
        "last_tick_class": "BUILDING",
        "entry_confirmed": False,
        "rejected": False,
    }
    sync_thesis_from_watch(thesis, watch=watch_building, spot=23080.0, alert_present=True, now_ts=1060.0)
    assert thesis["status"] == THESIS_STATUS_BUILDING
    assert thesis["mfe_spot_pts"] == 30.0  # 23080 - 23050
    assert thesis["supporting_evidence"] == ["writer add"]
    assert thesis["alert_quiet_sec"] == 0

    # Confirmed tick.
    watch_confirmed = {
        "entry_status": "ENTRY_CONFIRMED",
        "conviction_score": 90,
        "evidence": ["confirmed"],
        "entry_confirmed": True,
        "rejected": False,
    }
    sync_thesis_from_watch(thesis, watch=watch_confirmed, spot=23090.0, alert_present=False, now_ts=1120.0)
    assert thesis["status"] == THESIS_STATUS_CONFIRMED
    assert thesis["alert_quiet_sec"] == 60  # 1120 - 1060, alert went quiet

    entered = mark_thesis_entered(dict(thesis), paper_signal_id=42)
    assert entered["status"] == THESIS_STATUS_ENTERED
    assert entered["paper_signal_id"] == 42
    assert is_thesis_terminal(entered) is True

    # Rejection path.
    rejected_thesis = create_active_thesis(candidate=candidate, watch=watch, spot=23050.0, now_ts=1000.0)
    sync_thesis_from_watch(
        rejected_thesis,
        watch={"entry_status": "ENTRY_REJECTED", "rejected": True, "message": "Conviction collapsed"},
        spot=23000.0, alert_present=False, now_ts=1200.0,
    )
    assert rejected_thesis["status"] == THESIS_STATUS_INVALIDATED
    assert is_thesis_terminal(rejected_thesis) is True

    # Max-life expiry. Note: now_ts=0.0 would hit `now_ts or time.time()`'s
    # falsy-zero fallback in create_active_thesis (verbatim mentor behavior,
    # not fixed here) — use a non-zero start instead.
    aged_thesis = create_active_thesis(candidate=candidate, watch=watch, spot=23050.0, now_ts=1.0)
    sync_thesis_from_watch(
        aged_thesis, watch=watch, spot=23050.0, alert_present=True,
        now_ts=THESIS_MAX_LIFE_SEC + 1,
    )
    assert aged_thesis["status"] == THESIS_STATUS_EXPIRED

    api_row = thesis_to_api(thesis)
    assert api_row["conviction_score"] == 90
    assert api_row["entry_confirmed"] is True
    assert thesis_to_api(None) is None

    assert should_journal_update(thesis, now_ts=thesis["last_journal_ts"] + THESIS_UPDATE_JOURNAL_SEC + 1) is True
    assert should_journal_update(thesis, now_ts=thesis["last_journal_ts"] + 1) is False

    alert = writer_row_as_alert({"direction": "WRITERS ADDING", "strike": 23000}, writer_side="PE")
    assert alert["direction"] == "OI ADDING"
    assert alert["option_type"] == "PE"

    # Journal restore roundtrip.
    rows = [
        {"event": "THESIS_CREATED", "thesis_id": "t1", "signal_key": "k1", "decision": "BUY_CE",
         "created_at": "2026-07-23 09:00:00", "created_ts": 100.0, "spot_at_create": 23000.0},
        {"event": "THESIS_UPDATED", "thesis_id": "t1", "status": THESIS_STATUS_BUILDING, "spot_now": 23020.0},
    ]
    restored = restore_thesis_from_journal_rows(rows)
    assert restored is not None
    assert restored["status"] == THESIS_STATUS_BUILDING
    assert restored["spot_now"] == 23020.0

    rows_closed = rows + [{"event": "THESIS_ENTERED", "thesis_id": "t1"}]
    assert restore_thesis_from_journal_rows(rows_closed) is None  # terminal event clears the open thesis

    print("[analytics.active_thesis] selftest OK: create/sync/expire/entered lifecycle, journal restore")


if __name__ == "__main__":
    _selftest()
