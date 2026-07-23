#!/usr/bin/env python3
"""Official FII + participant context for intraday playbook (no PTE).

Ported faithfully from quant-desk-engine v4/ATLAS's playbook_official_context.py
(mentor-authored). No logic changed. Adaptations:
- BASE_DIR/JOURNAL_DIR resolve via nifty.paths; NSE_EOD_DB/RAW_EOD_DIR
  follow the same convention as nifty/sources/nse_eod.py and
  nifty/sources/india_vix.py (NSE_EOD_DIR / "nse_eod.sqlite",
  NSE_EOD_DIR / "raw").
- `from desk_phase_capture import load_fii_dii_history` is NOT imported
  from nifty.morning.phases (which already has an OLDER, simpler
  load_fii_dii_history wired into the live morning report and EOD filing
  paths - see nifty/eod/filing.py). v4's version of this function is
  richer (adds fii_buy_crores/sell_crores/positioning/read via
  fii_dii_entry_from_rows) but resolves each SQLite row's trade_date via
  fii_rows_trade_date(date-label parsing) instead of the raw SQL
  trade_date column, which is a different (theoretically fine, but
  unverified-equivalent) date-key derivation than nifty.morning.phases's
  existing version. Rather than touch a live-wired report function on an
  unverified equivalence, this richer load_fii_dii_history + its
  _prefer_fii_entry helper are duplicated locally, scoped to this new,
  not-yet-wired module only. nifty.morning.phases.load_fii_dii_history is
  untouched and behaves exactly as before.
- `from nse_official_flows import (...)` -> nifty.sources.nse_official_flows
  (already ported this session; build_multiframe_flows_payload,
  official_participant_bias, participant_brief_from_filing,
  positioning_label_short, previous_trading_day, fii_dii_entry_from_rows
  all already exist there with identical names/signatures).

Genuinely new capability: layers the official (T-1 filed) FII cash flow +
F&O participant OI positioning onto the intraday playbook - check-panel
rows, extension-gate adjustments, and a bias-verdict overlay that downgrades
CONFIRMED reads when official positioning is two-way/churn rather than
clean directional flow. nifty-dashboard's playbook today has no official-
flows overlay.

Not yet wired into the live playbook (no caller invokes enrich_playbook_payload
or backfill_playbook_file yet).
Self-check: python -m nifty.sources.playbook_official_context
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from nifty.paths import JOURNAL_DIR, NSE_EOD_DIR
from nifty.sources.fii_dii import fii_rows_trade_date, parse_fii_date
from nifty.sources.nse_official_flows import (
    build_multiframe_flows_payload,
    fii_dii_entry_from_rows,
    official_participant_bias,
    participant_brief_from_filing,
    positioning_label_short,
    previous_trading_day,
)

NSE_EOD_DB = NSE_EOD_DIR / "nse_eod.sqlite"
RAW_EOD_DIR = NSE_EOD_DIR / "raw"

FII_TWO_WAY = frozenset(
    {
        "POSITIONING_CHURN",
        "NET_SELL_HEAVY_TWO_WAY",
        "NET_BUY_HEAVY_TWO_WAY",
    }
)
OFFICIAL_BEARISH = frozenset({"BEARISH_FUT", "HEDGED_BEARISH", "PUT_HEAVY"})
OFFICIAL_BULLISH = frozenset({"BULLISH_FUT"})


def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _prefer_fii_entry(
    existing: Optional[Dict[str, Any]],
    candidate: Dict[str, Any],
) -> Dict[str, Any]:
    if not existing:
        return candidate
    if existing.get("fii_buy_crores") is None and candidate.get("fii_buy_crores") is not None:
        return candidate
    if candidate.get("source") == "nse_eod_raw" and existing.get("source") != "nse_eod_raw":
        return candidate
    return existing


def load_fii_dii_history(days: int = 5) -> List[Dict[str, Any]]:
    """Collect FII/DII buy/sell/net/positioning from SQLite, raw JSON, and prior morning desk files."""
    by_date: Dict[str, Dict[str, Any]] = {}

    if NSE_EOD_DB.exists():
        with sqlite3.connect(NSE_EOD_DB) as conn:
            rows = conn.execute(
                """
                SELECT trade_date, category, buyvalue, sellvalue, netvalue, date
                FROM fii_dii
                ORDER BY trade_date DESC
                """
            ).fetchall()
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for _trade_date, category, buyvalue, sellvalue, netvalue, label in rows:
            group_rows = [
                {
                    "category": category,
                    "buyValue": buyvalue,
                    "sellValue": sellvalue,
                    "netValue": netvalue,
                    "date": label,
                }
            ]
            payload_day = fii_rows_trade_date(group_rows)
            if not payload_day:
                continue
            key = payload_day.isoformat()
            grouped.setdefault(key, []).append(group_rows[0])
        for trade_date, group in grouped.items():
            entry = fii_dii_entry_from_rows(group, trade_date=trade_date, source="nse_eod_sqlite")
            by_date[trade_date] = _prefer_fii_entry(by_date.get(trade_date), entry)

    if RAW_EOD_DIR.exists():
        for folder in sorted(RAW_EOD_DIR.iterdir(), reverse=True):
            if not folder.is_dir():
                continue
            for path in folder.glob("fii_dii_*.json"):
                try:
                    rows = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(rows, list):
                    continue
                parsed = fii_rows_trade_date(rows)
                if not parsed:
                    continue
                key = parsed.isoformat()
                entry = fii_dii_entry_from_rows(rows, trade_date=key, source="nse_eod_raw")
                by_date[key] = _prefer_fii_entry(by_date.get(key), entry)

    for path in sorted(JOURNAL_DIR.glob("morning_desk_*.json"), reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        key = str(payload.get("trade_date") or path.stem.replace("morning_desk_", ""))
        if key in by_date and by_date[key].get("fii_buy_crores") is not None:
            continue
        raw_rows = payload.get("fii_dii") or []
        if isinstance(raw_rows, list) and raw_rows:
            parsed = fii_rows_trade_date(raw_rows)
            if parsed:
                key = parsed.isoformat()
            entry = fii_dii_entry_from_rows(raw_rows, trade_date=key, source="morning_desk_journal")
        else:
            fii_net = _as_float(payload.get("fii_net_crores"))
            dii_net = _as_float(payload.get("dii_net_crores"))
            if fii_net is None and dii_net is None:
                continue
            parsed = parse_fii_date(str(payload.get("fii_dii_date") or ""))
            if parsed:
                key = parsed.isoformat()
            entry = {
                "trade_date": key,
                "fii_net_crores": fii_net,
                "dii_net_crores": dii_net,
                "fii_dii_date": payload.get("fii_dii_date"),
                "source": "morning_desk_journal",
            }
        by_date[key] = _prefer_fii_entry(by_date.get(key), entry)

    history = sorted(by_date.values(), key=lambda row: row["trade_date"], reverse=True)[:days]
    history.reverse()
    return history


def _dedupe_fii_history(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: set = set()
    out: List[Dict[str, Any]] = []
    for row in rows:
        d = str(row.get("trade_date") or "")
        if not d or d in seen:
            continue
        seen.add(d)
        out.append(row)
    return out


def trim_fii_history(fii_history: List[Dict[str, Any]], trade_date: date) -> List[Dict[str, Any]]:
    day_str = trade_date.isoformat()
    hist = _dedupe_fii_history(fii_history)
    return [r for r in hist if str(r.get("trade_date") or "") < day_str]


def _slim_participant_brief(brief: Dict[str, Any]) -> Dict[str, Any]:
    if not brief.get("participants"):
        return {}
    slim: Dict[str, Any] = {"trade_date": brief.get("trade_date"), "participants": {}, "reads": brief.get("reads") or []}
    for name in ("FII", "DII", "Pro", "Client"):
        row = (brief.get("participants") or {}).get(name) or {}
        if not row:
            continue
        slim["participants"][name] = {
            "index_fut_net_contracts": row.get("index_fut_net_contracts"),
            "index_call_oi_net": row.get("index_call_oi_net"),
            "index_put_oi_net": row.get("index_put_oi_net"),
            "index_fut_vol_net": row.get("index_fut_vol_net"),
        }
    return slim


def load_official_flows_context(trade_date: date) -> Dict[str, Any]:
    """Morning anchor: latest filed FII positioning + T-1 official participant OI."""
    prev = previous_trading_day(trade_date)
    fii_daily = trim_fii_history(load_fii_dii_history(days=60), trade_date)
    latest_fii = fii_daily[-1] if fii_daily else {}
    participant_brief = participant_brief_from_filing(prev.isoformat())
    multiframe = build_multiframe_flows_payload(trade_date, fii_daily, participant_end=prev)
    official = official_participant_bias(participant_brief)
    fii_w = (multiframe.get("fii") or {}).get("weekly") or []
    weekly_labels = [positioning_label_short(w.get("fii_positioning")) for w in fii_w[-3:]]
    positioning = str(latest_fii.get("fii_positioning") or "")
    return {
        "trade_date": trade_date.isoformat(),
        "participant_session": prev.isoformat(),
        "loaded": bool(latest_fii or participant_brief.get("participants")),
        "fii_latest": {
            "trade_date": latest_fii.get("trade_date"),
            "buy_crores": latest_fii.get("fii_buy_crores"),
            "sell_crores": latest_fii.get("fii_sell_crores"),
            "net_crores": latest_fii.get("fii_net_crores"),
            "positioning": positioning,
            "positioning_label": positioning_label_short(positioning),
            "read": latest_fii.get("fii_read") or "",
        },
        "official_participant": official,
        "participant_brief": _slim_participant_brief(participant_brief),
        "multiframe_summary": {
            "transition_narrative": (multiframe.get("fii") or {}).get("transition_narrative") or "",
            "weekly_positioning": weekly_labels,
        },
    }


def official_flows_checks(official_ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Single check-row payload for playbook checks panel."""
    fii = official_ctx.get("fii_latest") or {}
    official = official_ctx.get("official_participant") or {}
    pos = str(fii.get("positioning") or "")
    if pos in FII_TWO_WAY:
        status = "warn"
        detail = fii.get("read") or f"{fii.get('positioning_label')} — net-only is insufficient"
    elif pos.startswith("DISTRIBUTION") or pos == "MILD_NET_SELL":
        status = "bad"
        detail = fii.get("read") or f"FII {fii.get('positioning_label')}"
    elif pos.startswith("ACCUMULATION") or pos == "MILD_NET_BUY":
        status = "ok"
        detail = fii.get("read") or f"FII {fii.get('positioning_label')}"
    elif fii.get("net_crores") is not None:
        status = "info"
        detail = f"FII net ₹{fii.get('net_crores'):+,.0f} Cr"
    else:
        status = "info"
        detail = "FII flow not filed yet"
    ob = str(official.get("bias") or "NEUTRAL")
    if ob in OFFICIAL_BEARISH:
        part_status = "bad"
    elif ob in OFFICIAL_BULLISH:
        part_status = "ok"
    else:
        part_status = "info"
    return {
        "fii_positioning": {
            "id": "official_fii_positioning",
            "label": "Official FII positioning (T-1 filed)",
            "status": status,
            "detail": detail,
        },
        "participant_oi": {
            "id": "official_participant_oi",
            "label": f"Official participant OI (EOD {official_ctx.get('participant_session', '')})",
            "status": part_status,
            "detail": official.get("read") or "No participant filing",
        },
        "weekly_transition": {
            "id": "official_weekly_transition",
            "label": "FII weekly transition",
            "status": "info",
            "detail": (official_ctx.get("multiframe_summary") or {}).get("transition_narrative")
            or "Insufficient weekly FII history",
        },
    }


def apply_official_flows_gates(
    *,
    official_ctx: Dict[str, Any],
    can_extend: bool,
    extend_reasons: List[str],
    phase: str,
    phase_note: str,
    pe_confirmed: List[Dict[str, Any]],
    pe_divergence: List[Dict[str, Any]],
    ce_push: List[Dict[str, Any]],
    gap_type: str,
) -> Tuple[bool, List[str], str, List[str]]:
    """Adjust extension gate and phase note from official flows. Returns notes list."""
    notes: List[str] = []
    if not official_ctx.get("loaded"):
        return can_extend, extend_reasons, phase_note, notes

    fii = official_ctx.get("fii_latest") or {}
    positioning = str(fii.get("positioning") or "")
    official = official_ctx.get("official_participant") or {}
    ob = str(official.get("bias") or "NEUTRAL")

    if positioning in FII_TWO_WAY:
        notes.append(f"FII {fii.get('positioning_label')} — do not lean on net FII alone")

    if ob in OFFICIAL_BEARISH:
        notes.append(f"Official T-1: {official.get('read') or ob}")
        if can_extend and not pe_confirmed:
            can_extend = False
            extend_reasons = list(extend_reasons) + [
                "Official hedged/bearish participant OI — need PE+spot confirmation for extension"
            ]
        if phase == "EXTENSION" and not pe_confirmed:
            phase_note = (
                f"{phase_note} — official bearish/hedged positioning; extension needs live PE+spot confirm"
            )
        if ce_push and phase in {"EXTENSION", "ORB_RECLAIMED", "PE_BUILD_930"}:
            phase_note = f"{phase_note} — respect official bearish structure; CE push is fade watch"

    if ob in OFFICIAL_BULLISH and gap_type == "GAP_DOWN" and pe_confirmed:
        notes.append("Official T-1 bullish fut — gap-down reclaim has structural support if PE+spot confirms")

    if positioning == "POSITIONING_CHURN" and gap_type == "GAP_DOWN":
        phase_note = f"{phase_note} — FII churn/roll (two-way flow); gap-down ≠ clean distribution"

    trans = (official_ctx.get("multiframe_summary") or {}).get("transition_narrative") or ""
    if trans and phase in {"FLAT_OPEN", "ORB_WATCH", "ORB_HOLD", "ORB_RECLAIMED"}:
        notes.append(trans[:180] + ("…" if len(trans) > 180 else ""))

    return can_extend, extend_reasons, phase_note, notes


def evaluate_official_bias_overlay(
    *,
    verdict: str,
    detail: List[str],
    expected_bias: str,
    gap_type: str,
    cash_gap_type: str,
    phase: str,
    official_ctx: Dict[str, Any],
) -> Tuple[str, List[str]]:
    """Layer official FII/participant reads onto gap-vs-bias verdict."""
    if not official_ctx.get("loaded"):
        return verdict, detail

    fii = official_ctx.get("fii_latest") or {}
    positioning = str(fii.get("positioning") or "")
    official = official_ctx.get("official_participant") or {}
    ob = str(official.get("bias") or "NEUTRAL")
    expected = str(expected_bias or "UNKNOWN").upper()
    bearish_expected = expected.startswith("BEAR") or cash_gap_type in {"GAP_DOWN", "MILD_DOWN"}
    bullish_expected = expected.startswith("BULL") or cash_gap_type in {"GAP_UP", "MILD_UP"}

    overlay: List[str] = []
    if positioning in FII_TWO_WAY and verdict == "CONFIRMED":
        if bearish_expected and (positioning == "POSITIONING_CHURN" or gap_type == "GAP_DOWN"):
            verdict = "PARTIAL"
            overlay.append(
                f"FII {fii.get('positioning_label')} — downgrade: net sell/buy is two-way, not clean directional confirm"
            )
        elif bullish_expected and positioning == "NET_BUY_HEAVY_TWO_WAY":
            verdict = "PARTIAL"
            overlay.append("FII net buy with heavy two-way turnover — accumulation vs roll unclear")

    if ob in OFFICIAL_BEARISH:
        overlay.append(f"Official participant: {official.get('read') or ob}")
        if verdict == "CONFIRMED" and bullish_expected:
            verdict = "PARTIAL"
            overlay.append("Morning bullish lean vs official bearish/hedged participant positioning")
        if phase in {"EXTENSION", "PE_BUILD_930"} and bullish_expected:
            verdict = "PARTIAL" if verdict == "CONFIRMED" else verdict
            overlay.append("Extension phases need live confirm against official bearish book")

    if ob in OFFICIAL_BULLISH and bearish_expected and gap_type == "GAP_DOWN":
        if verdict == "CONFIRMED":
            verdict = "PARTIAL"
            overlay.append("Gap-down vs official bullish fut positioning — possible shakeout, not auto bearish")

    if overlay:
        detail = list(detail) + overlay
    return verdict, detail


def enrich_playbook_payload(
    payload: Dict[str, Any],
    official_ctx: Dict[str, Any],
) -> Dict[str, Any]:
    """Apply official flows to an existing playbook phase payload (live or backfill)."""
    if not official_ctx.get("loaded"):
        payload["official_flows"] = official_ctx
        return payload

    can_extend, extend_reasons, phase_note, gate_notes = apply_official_flows_gates(
        official_ctx=official_ctx,
        can_extend=bool(payload.get("can_extend")),
        extend_reasons=list(payload.get("extend_reasons") or []),
        phase=str(payload.get("phase") or ""),
        phase_note=str(payload.get("phase_note") or ""),
        pe_confirmed=list(payload.get("pe_confirmed") or []),
        pe_divergence=list(payload.get("pe_divergence") or []),
        ce_push=list(payload.get("ce_push") or []),
        gap_type=str(payload.get("gap_type") or payload.get("cash_open_gap_type") or ""),
    )
    payload["can_extend"] = can_extend
    payload["extend_reasons"] = extend_reasons
    payload["phase_note"] = phase_note
    payload["official_flows"] = official_ctx
    payload["official_gate_notes"] = gate_notes

    checks = list(payload.get("checks") or [])
    flow_checks = official_flows_checks(official_ctx)
    existing_ids = {str(c.get("id") or "") for c in checks}
    for key in ("fii_positioning", "participant_oi", "weekly_transition"):
        row = flow_checks[key]
        if row["id"] not in existing_ids:
            checks.append(row)
    payload["checks"] = checks

    bias = dict(payload.get("bias_verdict") or {})
    if bias:
        verdict, detail = evaluate_official_bias_overlay(
            verdict=str(bias.get("verdict") or "PENDING"),
            detail=[s for s in str(bias.get("detail") or "").split("; ") if s],
            expected_bias=str(bias.get("expected_bias") or ""),
            gap_type=str(payload.get("gap_type") or ""),
            cash_gap_type=str((bias.get("live_gap") or payload.get("cash_open_gap_type") or "")),
            phase=str(payload.get("phase") or ""),
            official_ctx=official_ctx,
        )
        bias["verdict"] = verdict
        bias["detail"] = "; ".join(detail)
        bias["official_fii_positioning"] = (official_ctx.get("fii_latest") or {}).get("positioning_label")
        bias["official_participant_bias"] = (official_ctx.get("official_participant") or {}).get("bias")
        payload["bias_verdict"] = bias

    return payload


def build_official_flows_anchor(trade_date: date) -> Dict[str, Any]:
    ctx = load_official_flows_context(trade_date)
    return {"event": "OFFICIAL_FLOWS_ANCHOR", **ctx}


def backfill_playbook_file(
    path: Path,
    trade_date: date,
    *,
    overwrite: bool = True,
) -> Dict[str, Any]:
    """Enrich existing nifty_playbook jsonl with official flows anchor + phase patches."""
    if not path.exists():
        return {"path": str(path), "skipped": True, "reason": "missing"}

    official_ctx = load_official_flows_context(trade_date)
    lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    rows: List[Dict[str, Any]] = []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    has_anchor = any(r.get("event") == "OFFICIAL_FLOWS_ANCHOR" for r in rows)
    out: List[Dict[str, Any]] = []
    if not has_anchor and official_ctx.get("loaded"):
        out.append(build_official_flows_anchor(trade_date))

    phases = 0
    for row in rows:
        if row.get("event") == "OFFICIAL_FLOWS_ANCHOR":
            if overwrite and official_ctx.get("loaded"):
                out.append(build_official_flows_anchor(trade_date))
            else:
                out.append(row)
            continue
        if row.get("event") == "PLAYBOOK_PHASE":
            phases += 1
            out.append(enrich_playbook_payload(dict(row), official_ctx))
        else:
            out.append(row)

    backup = path.with_suffix(path.suffix + ".pre_official")
    if not backup.exists() and path.exists():
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in out) + "\n", encoding="utf-8")
    return {
        "path": str(path),
        "skipped": False,
        "phases_enriched": phases,
        "anchor_added": not has_anchor and official_ctx.get("loaded"),
        "official_loaded": official_ctx.get("loaded"),
    }


def _selftest() -> None:
    import tempfile

    global JOURNAL_DIR, NSE_EOD_DB, RAW_EOD_DIR
    tmp = Path(tempfile.mkdtemp(prefix="playbook-official-context-selftest-"))
    original_journal_dir = JOURNAL_DIR
    original_db = NSE_EOD_DB
    original_raw_dir = RAW_EOD_DIR
    try:
        JOURNAL_DIR = tmp
        NSE_EOD_DB = tmp / "nse_eod.sqlite"
        RAW_EOD_DIR = tmp / "raw"

        assert _prefer_fii_entry(None, {"source": "x"}) == {"source": "x"}
        richer = _prefer_fii_entry({"fii_buy_crores": None}, {"fii_buy_crores": 100.0})
        assert richer["fii_buy_crores"] == 100.0

        # No sources at all -> empty history, never raises.
        assert load_fii_dii_history(days=10) == []

        conn = sqlite3.connect(NSE_EOD_DB)
        conn.execute(
            "CREATE TABLE fii_dii (trade_date TEXT, category TEXT, buyvalue REAL, sellvalue REAL, netvalue REAL, date TEXT)"
        )
        conn.executemany(
            "INSERT INTO fii_dii VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("2026-07-20", "FII", 15000.0, 14500.0, 500.0, "20-Jul-2026"),
                ("2026-07-20", "DII", 12000.0, 11800.0, 200.0, "20-Jul-2026"),
            ],
        )
        conn.commit()
        conn.close()

        history = load_fii_dii_history(days=10)
        assert len(history) == 1
        row = history[0]
        assert row["trade_date"] == "2026-07-20"
        assert row["fii_net_crores"] == 500.0
        assert row["fii_buy_crores"] == 15000.0
        assert row["fii_positioning"]  # positioning label always populated

        trimmed = trim_fii_history(history, date(2026, 7, 21))
        assert len(trimmed) == 1
        trimmed_same_day = trim_fii_history(history, date(2026, 7, 20))
        assert trimmed_same_day == []  # strictly before trade_date

        assert _dedupe_fii_history(history + history) == history

        slim = _slim_participant_brief({"participants": {"FII": {"index_fut_net_contracts": -5000}}})
        assert slim["participants"]["FII"]["index_fut_net_contracts"] == -5000
        assert _slim_participant_brief({}) == {}

        # load_official_flows_context: no participant filing journal -> loaded True from FII history alone.
        ctx = load_official_flows_context(date(2026, 7, 21))
        assert ctx["trade_date"] == "2026-07-21"
        assert ctx["loaded"] is True
        assert ctx["fii_latest"]["net_crores"] == 500.0

        checks = official_flows_checks(ctx)
        assert "fii_positioning" in checks and "participant_oi" in checks

        can_extend, extend_reasons, phase_note, notes = apply_official_flows_gates(
            official_ctx={"loaded": False},
            can_extend=True,
            extend_reasons=[],
            phase="EXTENSION",
            phase_note="",
            pe_confirmed=[],
            pe_divergence=[],
            ce_push=[],
            gap_type="GAP_DOWN",
        )
        assert can_extend is True and notes == []  # not loaded -> no-op passthrough

        bearish_ctx = {
            "loaded": True,
            "fii_latest": {"positioning": "DISTRIBUTION_HEAVY", "positioning_label": "Distribution"},
            "official_participant": {"bias": "HEDGED_BEARISH", "read": "FII short + puts"},
            "multiframe_summary": {"transition_narrative": ""},
        }
        can_extend2, extend_reasons2, phase_note2, notes2 = apply_official_flows_gates(
            official_ctx=bearish_ctx,
            can_extend=True,
            extend_reasons=[],
            phase="EXTENSION",
            phase_note="Extending",
            pe_confirmed=[],
            pe_divergence=[],
            ce_push=[],
            gap_type="GAP_DOWN",
        )
        assert can_extend2 is False
        assert "PE+spot confirmation" in extend_reasons2[0]

        verdict, detail = evaluate_official_bias_overlay(
            verdict="CONFIRMED",
            detail=["morning bullish lean"],
            expected_bias="BULLISH",
            gap_type="GAP_DOWN",
            cash_gap_type="GAP_UP",
            phase="EXTENSION",
            official_ctx=bearish_ctx,
        )
        assert verdict == "PARTIAL"
        assert any("bearish" in d.lower() for d in detail)

        payload = {"phase": "EXTENSION", "can_extend": True, "checks": [], "bias_verdict": {"verdict": "CONFIRMED", "expected_bias": "BULLISH"}}
        enriched = enrich_playbook_payload(dict(payload), bearish_ctx)
        assert enriched["can_extend"] is False
        assert enriched["bias_verdict"]["verdict"] == "PARTIAL"
        assert len(enriched["checks"]) == 3

        not_loaded = enrich_playbook_payload(dict(payload), {"loaded": False})
        assert not_loaded["official_flows"] == {"loaded": False}
        assert not_loaded["can_extend"] is True  # untouched

        anchor = build_official_flows_anchor(date(2026, 7, 21))
        assert anchor["event"] == "OFFICIAL_FLOWS_ANCHOR"

        # backfill_playbook_file: missing path -> skipped, never raises.
        missing = backfill_playbook_file(tmp / "does_not_exist.jsonl", date(2026, 7, 21))
        assert missing["skipped"] is True

        pb_path = tmp / "nifty_playbook_2026-07-21.jsonl"
        pb_path.write_text(
            "\n".join(
                json.dumps(r)
                for r in [
                    {"event": "PLAYBOOK_PHASE", "phase": "EXTENSION", "can_extend": True, "checks": [], "bias_verdict": {"verdict": "CONFIRMED", "expected_bias": "BULLISH"}}
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        result = backfill_playbook_file(pb_path, date(2026, 7, 21))
        assert result["skipped"] is False
        assert result["phases_enriched"] == 1
        assert (pb_path.with_suffix(".jsonl.pre_official")).exists()
    finally:
        JOURNAL_DIR = original_journal_dir
        NSE_EOD_DB = original_db
        RAW_EOD_DIR = original_raw_dir

    print("[sources.playbook_official_context] selftest OK: FII history, context load, gates, bias overlay, backfill")


if __name__ == "__main__":
    _selftest()
