#!/usr/bin/env python3
"""Normalize desk session/morning context and emit PTE evidence candidates.

Ported faithfully from quant-desk-engine v4/ATLAS's
pte_session_context_evidence.py (mentor-authored). No logic changed. Only
adaptation: imports load_eod_futures_context/previous_trading_day from
nifty.analytics.futures (both already exist there — this session's earlier
v1 port of nifty_futures_context.py) instead of the standalone
nifty_futures_context / nse_trading_calendar modules. nifty-dashboard
already has its own previous_trading_day equivalents in nifty/eod/filing.py
and nifty/sources/nse_eod.py too (pre-existing duplication, not introduced
by this port) — nifty.analytics.futures's own copy is used here since this
adapter's sibling function (load_eod_futures_context) lives in that same
module.

Bridges nifty-dashboard's morning-desk/session-journal/EOD-futures context
into nifty.pte.evidence_engine's EvidenceRecord candidate shape, same
pattern as chain_evidence.py and liquidity_evidence.py.

Not yet wired into the live pipeline.
Self-check: python -m nifty.pte.session_evidence
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from nifty.analytics.futures import load_eod_futures_context, previous_trading_day

IST = timezone(timedelta(hours=5, minutes=30))
RECORDED_AT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})")


def _f(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


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


def _vix_last(vix: Any) -> Optional[float]:
    if isinstance(vix, dict):
        return _f(vix.get("last"))
    return _f(vix)


def _vix_pct_change(vix: Any) -> Optional[float]:
    if isinstance(vix, dict):
        return _f(vix.get("percent_change"))
    return None


def _fii_dii_nets(morning: Mapping[str, Any]) -> tuple[Optional[float], Optional[float]]:
    fii = _f(morning.get("fii_net_crores"))
    dii = _f(morning.get("dii_net_crores"))
    if fii is not None or dii is not None:
        return fii, dii
    fii_net: Optional[float] = None
    dii_net: Optional[float] = None
    for row in morning.get("fii_dii") or []:
        if not isinstance(row, dict):
            continue
        cat = str(row.get("category") or "").upper()
        net = _f(row.get("netValue"))
        if "FII" in cat:
            fii_net = net
        elif cat == "DII":
            dii_net = net
    return fii_net, dii_net


def normalize_desk_context(raw: Mapping[str, Any], *, spot: float = 0.0) -> Dict[str, Any]:
    """Unify morning desk, session journal, EOD futures, and replay shapes."""
    ctx = dict(raw or {})
    vix = ctx.get("india_vix")
    if vix is None:
        vix = ctx.get("vix")

    ema20 = _f(ctx.get("ema_20"))
    ema50 = _f(ctx.get("ema_50"))
    if ema20 is None and spot > 0:
        ema20 = _f(ctx.get("ema_20_level"))
    if ema50 is None and spot > 0:
        ema50 = _f(ctx.get("ema_50_level"))

    def ema_dist(level: Optional[float]) -> Optional[float]:
        if level is None or spot <= 0:
            return _f(ctx.get(f"ema_{20 if level == ema20 else 50}_dist"))
        return round(spot - level, 2)

    gift_bias = str(ctx.get("gift_overnight_bias") or ctx.get("overnight_bias") or "").upper()
    gift_premium = _f(ctx.get("gift_premium_pct") or ctx.get("premium_pct"))

    fii_net, dii_net = _fii_dii_nets(ctx)
    if fii_net is None:
        fii_net = _f(ctx.get("fii_net_crores"))
    if dii_net is None:
        dii_net = _f(ctx.get("dii_net_crores"))

    return {
        "india_vix": _vix_last(vix),
        "vix_pct_change": _vix_pct_change(vix) or _f(ctx.get("vix_pct_change")),
        "ema_20": ema20,
        "ema_50": ema50,
        "ema_20_dist": ema_dist(ema20),
        "ema_50_dist": ema_dist(ema50),
        "gift_overnight_bias": gift_bias or None,
        "gift_premium_pct": gift_premium,
        "fii_net_crores": fii_net,
        "dii_net_crores": dii_net,
        "macro_bias_eod": str(ctx.get("macro_bias_eod") or ctx.get("macro_bias") or "").upper() or None,
        "fii_index_net": _f(ctx.get("fii_index_net")),
        "dii_index_net": _f(ctx.get("dii_index_net")),
        "india_bias_label": str(ctx.get("india_bias_label") or ctx.get("india_bias") or "").upper() or None,
        "prev_day_direction": str(ctx.get("prev_day_direction") or "").upper() or None,
        "futures_front_behavior": str(ctx.get("futures_front_behavior") or ctx.get("front_behavior") or "").upper() or None,
        "futures_inherited_read": str(ctx.get("futures_inherited_read") or ctx.get("eod_read") or "").strip() or None,
    }


def build_desk_context(
    *,
    morning: Optional[Mapping[str, Any]] = None,
    session_row: Optional[Mapping[str, Any]] = None,
    eod: Optional[Mapping[str, Any]] = None,
    spot: float = 0.0,
    futures_layer: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Merge morning, intraday session, and inherited EOD futures context."""
    merged: Dict[str, Any] = {}
    morning = morning or {}
    session_row = session_row or {}
    eod = eod or {}
    futures_layer = futures_layer or {}

    if morning:
        merged.update(morning)
        bias = morning.get("india_bias") or {}
        if isinstance(bias, dict):
            merged["india_bias_label"] = bias.get("label")
        gift = morning.get("gift_nifty") or {}
        if isinstance(gift, dict):
            merged["gift_overnight_bias"] = gift.get("overnight_bias")
            merged["gift_premium_pct"] = gift.get("premium_pct")
        prev = morning.get("prev_day_structure") or {}
        if isinstance(prev, dict):
            candle = prev.get("candle") or {}
            merged["prev_day_direction"] = candle.get("direction") or prev.get("close_vs_prev")
        vix = morning.get("india_vix")
        if vix is not None:
            merged["india_vix"] = vix

    sc = session_row.get("session_context") or {}
    if isinstance(sc, dict):
        tech = sc.get("technical_levels") or {}
        if isinstance(tech, dict):
            for key in ("india_vix", "ema_20", "ema_50", "ema_100", "ema_200"):
                if tech.get(key) is not None:
                    merged[key] = tech.get(key)
            for period in (20, 50, 100, 200):
                dist_key = f"ema_{period}_dist"
                if tech.get(dist_key) is not None:
                    merged[dist_key] = tech.get(dist_key)
        gift = sc.get("gift_nifty") or {}
        if isinstance(gift, dict):
            if gift.get("overnight_bias"):
                merged["gift_overnight_bias"] = gift.get("overnight_bias")
            if gift.get("premium_pct") is not None:
                merged["gift_premium_pct"] = gift.get("premium_pct")

    if eod:
        merged["macro_bias_eod"] = eod.get("macro_bias")
        merged["fii_index_net"] = eod.get("fii_index_net")
        merged["dii_index_net"] = eod.get("dii_index_net")
        merged["futures_inherited_read"] = eod.get("read")

    front_behavior = str(futures_layer.get("front_behavior") or "").upper()
    if front_behavior and front_behavior not in {"UNKNOWN", "FLAT"}:
        merged["futures_front_behavior"] = front_behavior

    if spot <= 0:
        spot = _f(session_row.get("spot")) or _f((morning.get("nifty") or {}).get("last")) or 0.0

    norm = normalize_desk_context(merged, spot=spot or 0.0)
    norm["futures_front_behavior"] = norm.get("futures_front_behavior") or front_behavior or None
    return norm


def desk_context_from_replay_snapshot(snapshot: Mapping[str, Any]) -> Dict[str, Any]:
    """Extract desk context from replay snapshot blocks."""
    raw = dict(snapshot.get("desk_context") or {})
    if not raw:
        morning = snapshot.get("morning_context") or {}
        volatility = snapshot.get("volatility") or {}
        breadth = snapshot.get("breadth") or {}
        sc = snapshot.get("session_context") or {}
        iv = snapshot.get("iv") or {}
        futures = snapshot.get("futures") or {}
        if morning:
            raw.update(morning if isinstance(morning, dict) else {})
        if isinstance(sc, dict):
            raw.update(sc.get("technical_levels") or {})
            gift = sc.get("gift_nifty") or {}
            if isinstance(gift, dict):
                raw["gift_overnight_bias"] = gift.get("overnight_bias")
                raw["gift_premium_pct"] = gift.get("premium_pct")
        if volatility.get("regime"):
            raw["volatility_regime"] = volatility.get("regime")
        if breadth:
            raw["breadth"] = breadth
        if iv.get("iv_rank") is not None and raw.get("india_vix") is None:
            pass
        raw["futures_front_behavior"] = futures.get("front_behavior")
    spot = _f(snapshot.get("spot")) or 0.0
    return normalize_desk_context(raw, spot=spot)


@dataclass
class DayContextBundle:
    morning: Dict[str, Any]
    eod: Dict[str, Any]
    session_rows: List[Dict[str, Any]]

    @classmethod
    def load(cls, journal_dir: Path, session_day: date) -> "DayContextBundle":
        day_s = session_day.isoformat()
        morning = _read_json(journal_dir / f"morning_desk_{day_s}.json")
        session_rows = []
        session_path = journal_dir / f"nifty_session_{day_s}.jsonl"
        if session_path.exists():
            for line in session_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    session_rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            session_rows.sort(
                key=lambda row: parse_recorded_at(str(row.get("recorded_at") or ""), session_day)
            )
        prior = previous_trading_day(session_day)
        eod = load_eod_futures_context(prior)
        if not eod.get("loaded"):
            eod = load_eod_futures_context()
        return cls(morning=morning, eod=eod, session_rows=session_rows)

    def session_row_at(self, ts: datetime, session_day: date) -> Dict[str, Any]:
        chosen: Dict[str, Any] = {}
        for row in self.session_rows:
            row_ts = parse_recorded_at(str(row.get("recorded_at") or ""), session_day)
            if row_ts <= ts:
                chosen = row
            else:
                break
        return chosen

    def desk_context_at(
        self,
        ts: datetime,
        session_day: date,
        *,
        spot: float = 0.0,
        futures_layer: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        return build_desk_context(
            morning=self.morning,
            session_row=self.session_row_at(ts, session_day),
            eod=self.eod,
            spot=spot,
            futures_layer=futures_layer or {},
        )


def append_session_context_evidence(
    cands: List[Dict[str, Any]],
    *,
    desk_context: Mapping[str, Any],
    spot: float,
    observatory_state_ids: Mapping[str, str],
    interpretation_state_ids: Mapping[str, str],
    now_iso: str,
) -> None:
    """Append context-tab EVD rows beyond spot and expected move."""
    ctx = normalize_desk_context(desk_context, spot=spot)

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
                    "observatory_id": "context",
                    "observatory_state_id": observatory_state_ids["context"],
                    "field_path": field_path,
                },
                "observation": observation,
                "interpretation": interpretation,
                "interpretation_state_id": interpretation_state_ids["context"],
                "quality": "inferred",
                "weight_hint": weight,
                "theory_hints": {"supports": supports or [], "contradicts": []},
                "effective_at": now_iso,
                "observed_at": now_iso,
            }
        )

    vix = ctx.get("india_vix")
    if vix is not None:
        pct = ctx.get("vix_pct_change")
        pct_s = f" · pct_change={pct:+.2f}%" if pct is not None else ""
        add(
            "India VIX",
            "context.india_vix",
            f"India VIX={vix:.2f}{pct_s}",
            "Fear gauge / vol overlay",
            weight="moderate",
            supports=["breakout_expansion"] if vix >= 15 else ["range_compression"],
        )

    if ctx.get("ema_20") is not None and spot > 0:
        add(
            "EMA distance",
            "context.ema_distances",
            (
                f"EMA20={ctx['ema_20']:.2f} · dist={ctx.get('ema_20_dist'):+.2f} · "
                f"EMA50={ctx.get('ema_50')} · dist50={ctx.get('ema_50_dist')}"
            ),
            "Spot vs daily EMA structure",
            weight="moderate",
            supports=["breakout_expansion"],
        )

    gift = str(ctx.get("gift_overnight_bias") or "")
    if gift:
        prem = ctx.get("gift_premium_pct")
        prem_s = f" · premium={prem:+.3f}%" if prem is not None else ""
        add(
            "GIFT overnight bias",
            "context.gift_overnight",
            f"GIFT overnight={gift}{prem_s}",
            "Pre-open / overnight gap context",
            weight="moderate",
            supports=["breakout_expansion"] if "DOWN" in gift else ["range_compression"],
        )

    if ctx.get("fii_net_crores") is not None or ctx.get("dii_net_crores") is not None:
        add(
            "FII/DII cash flow",
            "context.fii_dii_flow",
            (
                f"FII net={ctx.get('fii_net_crores')} Cr · "
                f"DII net={ctx.get('dii_net_crores')} Cr"
            ),
            "Prior-session institutional cash flow",
            weight="moderate",
            supports=["inventory_transfer", "dealer_hedging"],
        )

    macro = str(ctx.get("macro_bias_eod") or "")
    if macro and macro != "NEUTRAL":
        add(
            "Inherited macro bias",
            "context.macro_eod_bias",
            (
                f"EOD macro={macro} · FII fut net={ctx.get('fii_index_net')} · "
                f"DII fut net={ctx.get('dii_index_net')}"
            ),
            "T-1 participant futures positioning overlay",
            weight="strong",
            supports=["dealer_hedging", "inventory_transfer"],
        )

    bias = str(ctx.get("india_bias_label") or "")
    if bias and bias != "UNKNOWN":
        add(
            "Morning desk bias",
            "context.india_bias",
            f"Morning bias={bias}",
            "Pre-session composite bias read",
            weight="weak",
            supports=["breakout_expansion"] if "BEAR" in bias else ["range_compression"],
        )

    inherited = str(ctx.get("futures_inherited_read") or "")
    front = str(ctx.get("futures_front_behavior") or "")
    if inherited or (front and front not in {"UNKNOWN", "FLAT"}):
        add(
            "Futures inherited relationship",
            "context.futures_inherited",
            f"Inherited={inherited[:80]} · live front={front or 'UNKNOWN'}",
            "Live futures vs inherited structure",
            weight="strong" if front and front not in {"UNKNOWN", "FLAT"} else "moderate",
            supports=["inventory_transfer", "dealer_hedging"],
        )


def _selftest() -> None:
    import tempfile

    raw = {
        "india_vix": 14.5, "vix_pct_change": -2.1, "gift_overnight_bias": "GAP_DOWN",
        "gift_premium_pct": -0.25, "fii_net_crores": -850.0, "dii_net_crores": 620.0,
        "macro_bias_eod": "BEARISH", "india_bias_label": "BEARISH",
        "ema_20": 22950.0,
    }
    ctx = normalize_desk_context(raw, spot=23000.0)
    assert ctx["india_vix"] == 14.5
    assert ctx["ema_20_dist"] == 50.0
    assert ctx["gift_overnight_bias"] == "GAP_DOWN"
    assert ctx["fii_net_crores"] == -850.0

    parsed = parse_recorded_at("2026-07-23 10:15:00", date(2026, 7, 23))
    assert parsed.hour == 10 and parsed.minute == 15

    built = build_desk_context(
        morning={"india_bias": {"label": "BULLISH"}, "gift_nifty": {"overnight_bias": "GAP_UP", "premium_pct": 0.3}},
        session_row={"session_context": {"technical_levels": {"ema_20": 23000.0}}},
        eod={"macro_bias": "BULLISH", "fii_index_net": 500.0, "dii_index_net": -100.0, "read": "FII net long"},
        spot=23050.0,
    )
    assert built["india_bias_label"] == "BULLISH"
    assert built["gift_overnight_bias"] == "GAP_UP"
    assert built["macro_bias_eod"] == "BULLISH"
    assert built["futures_inherited_read"] == "FII net long"

    replay_ctx = desk_context_from_replay_snapshot({
        "spot": 23000.0, "morning_context": {"india_vix": 15.0},
        "session_context": {"technical_levels": {"ema_50": 22900.0}},
        "futures": {"front_behavior": "CONFIRMING"},
    })
    assert replay_ctx["india_vix"] == 15.0
    assert replay_ctx["futures_front_behavior"] == "CONFIRMING"

    tmp = Path(tempfile.mkdtemp(prefix="session-evidence-selftest-"))
    day = date(2026, 7, 23)
    (tmp / f"morning_desk_{day.isoformat()}.json").write_text(json.dumps({"india_vix": 13.5}), encoding="utf-8")
    session_path = tmp / f"nifty_session_{day.isoformat()}.jsonl"
    session_path.write_text(
        json.dumps({"recorded_at": "2026-07-23 09:20:00", "spot": 23000.0}) + "\n"
        + json.dumps({"recorded_at": "2026-07-23 09:30:00", "spot": 23020.0}) + "\n",
        encoding="utf-8",
    )
    bundle = DayContextBundle.load(tmp, day)
    assert bundle.morning.get("india_vix") == 13.5
    assert len(bundle.session_rows) == 2

    row_at = bundle.session_row_at(datetime(2026, 7, 23, 9, 25, tzinfo=IST), day)
    assert row_at.get("spot") == 23000.0  # picks the row at-or-before ts, not the later one

    desk_ctx = bundle.desk_context_at(datetime(2026, 7, 23, 9, 35, tzinfo=IST), day, spot=23020.0)
    assert desk_ctx["india_vix"] == 13.5

    cands: List[Dict[str, Any]] = []
    obs_ids = {"context": "OBS-1"}
    int_ids = {"context": "INT-1"}
    append_session_context_evidence(
        cands, desk_context=raw, spot=23000.0,
        observatory_state_ids=obs_ids, interpretation_state_ids=int_ids, now_iso="2026-07-23T10:00:00+05:30",
    )
    assert len(cands) >= 4
    labels = {c["label"] for c in cands}
    assert "India VIX" in labels
    assert "FII/DII cash flow" in labels
    assert "Inherited macro bias" in labels

    empty: List[Dict[str, Any]] = []
    append_session_context_evidence(
        empty, desk_context={}, spot=0.0,
        observatory_state_ids=obs_ids, interpretation_state_ids=int_ids, now_iso="2026-07-23T10:00:00+05:30",
    )
    assert empty == []

    print("[pte.session_evidence] selftest OK: context normalization, day-bundle load, evidence emission")


if __name__ == "__main__":
    _selftest()
