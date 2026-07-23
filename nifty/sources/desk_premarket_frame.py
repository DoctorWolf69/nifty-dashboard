#!/usr/bin/env python3
"""Pre-open report frame — honest anchor, clocks, layered context (RQ-REPORT-001 Phase A/B).

Ported faithfully from quant-desk-engine v4/ATLAS's desk_premarket_frame.py
(mentor-authored). No logic changed. Only adaptation: the one import
(`from nse_official_flows import official_participant_bias`) points at
nifty.sources.nse_official_flows (already ported this session, same
function name). Every other function here is pure — no BASE_DIR/JOURNAL_DIR
dependency in the source either.

Genuinely new capability: report-formatting layer for the pre-open ->
auction -> cash-open lifecycle (narrative anchor text, layered open-frame
summary, global-markets table rows, auction timeline table/markdown/HTML).
Nothing in nifty-dashboard builds this narrative today. Pure text/dict
builders - no I/O, no journal writes.

Not yet wired into the live pipeline (no report writer calls these yet).
Self-check: python -m nifty.sources.desk_premarket_frame
"""

from __future__ import annotations

import json
from datetime import datetime, time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from nifty.sources.nse_official_flows import official_participant_bias


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_pts(value: Optional[float], signed: bool = True) -> str:
    if value is None:
        return "—"
    prefix = "+" if value >= 0 else "−"
    if not signed:
        prefix = ""
    return f"{prefix}{abs(value):.0f}"


def _fmt_pct(value: Optional[float], signed: bool = True) -> str:
    if value is None:
        return "—"
    prefix = "+" if value >= 0 else "−"
    if not signed:
        prefix = ""
    return f"{prefix}{abs(value):.2f}%"


def report_clock_phase(at: datetime) -> str:
    """Lifecycle clock: pre_open (<09:00), auction (09:00-09:14), cash_open (>=09:15)."""
    t = at.time()
    if t < time(9, 0):
        return "pre_open"
    if t < time(9, 15):
        return "auction"
    return "cash_open"


def live_price_stat_label(phase: str) -> str:
    return {
        "pre_open": "GIFT S1 (live)",
        "auction": "Auction / indicative",
        "cash_open": "Cash open (Kite)",
    }.get(phase, "Live reference")


def report_product_name(phase: str) -> str:
    if phase == "cash_open":
        return "Cash Open Brief"
    if phase == "auction":
        return "Auction Note"
    return "Pre-Open Card"


def yesterday_change(prev_sess: Dict[str, Any], prev_close: float) -> Tuple[Optional[float], Optional[float]]:
    """Session change vs prior close (pts, %)."""
    ref = _as_float(prev_sess.get("prev_close"))
    if ref is None or ref <= 0:
        filing_prev = _as_float(prev_sess.get("filing_prev_close"))
        ref = filing_prev
    if ref is None or ref <= 0:
        return None, None
    pts = round(prev_close - ref, 2)
    pct = round(pts / ref * 100, 2)
    return pts, pct


def breakout_confirmed_yesterday(
    prev_close: float,
    psych_level: Optional[float],
    change_pts: Optional[float],
) -> bool:
    """Breakout language only when level held on a non-red close."""
    if psych_level is None or prev_close < psych_level - 5:
        return False
    if change_pts is not None and change_pts < -10:
        return False
    if change_pts is not None and change_pts >= 0:
        return True
    return prev_close >= psych_level and (change_pts is None or change_pts >= -10)


def build_premarket_anchor(
    *,
    at: datetime,
    gift_last: Optional[float],
    gift_premium: Optional[float],
    gift_bias: str,
    prev_close: float,
    prev_sess: Dict[str, Any],
    sector_scan: Dict[str, Any],
    psych_level: Optional[float],
    global_bias: str,
    global_factors: List[str],
) -> str:
    """Three-part pre-open anchor: GIFT now * yesterday truth * open question."""
    change_pts, change_pct = yesterday_change(prev_sess, prev_close)
    sess_date = str(prev_sess.get("session_date") or "")[5:].replace("-", " ")
    breadth = sector_scan.get("breadth") or {}
    green = breadth.get("green", 0)
    total = breadth.get("total", 0)

    sectors = list(sector_scan.get("sectors") or [])
    best = sector_scan.get("best_performer") or {}
    worst = sector_scan.get("worst_performer") or {}
    bank = next((s for s in sectors if "Bank" in str(s.get("sector", ""))), None)
    nifty = next((s for s in sectors if "Nifty 50" in str(s.get("sector", ""))), None)

    parts: List[str] = []

    if gift_last:
        parts.append(
            f"**GIFT S1 @ {at.strftime('%H:%M')} IST:** {gift_last:,.1f} "
            f"(premium {_fmt_pts(gift_premium)} vs official close {prev_close:,.2f}) · {gift_bias.replace('_', ' ')}."
        )
    else:
        parts.append(f"**Pre-open @ {at.strftime('%H:%M')} IST:** GIFT not loaded — check manually before 9:15.")

    global_clause = global_bias.replace("_", " ")
    if global_factors:
        global_clause += f" ({'; '.join(global_factors[:3])})"
    parts.append(f"**Global overnight:** {global_clause}.")

    yday_bits = [f"**Yesterday ({sess_date or 'T−1'}):** Nifty closed **{prev_close:,.2f}**"]
    if change_pts is not None:
        yday_bits.append(f" ({_fmt_pts(change_pts)} pts / {_fmt_pct(change_pct)})")
    if psych_level and prev_close >= psych_level:
        if breakout_confirmed_yesterday(prev_close, psych_level, change_pts):
            yday_bits.append(f"— held **{psych_level:,.0f}** breakout")
        else:
            yday_bits.append(f"— still **above {psych_level:,.0f}** but session was weak")
    elif psych_level:
        yday_bits.append(f"— **{_fmt_pts(psych_level - prev_close)}** below {psych_level:,.0f}")
    if best.get("sector") and _as_float(best.get("change_pct")) is not None:
        yday_bits.append(f"; {best.get('sector')} led ({best.get('change_pct'):+.2f}%)")
    if worst.get("sector") and worst.get("sector") != best.get("sector"):
        wc = _as_float(worst.get("change_pct"))
        if wc is not None:
            yday_bits.append(f", {worst.get('sector')} lagged ({wc:+.2f}%)")
    if green and total:
        yday_bits.append(f" · breadth **{green}/{total}** green")
    if bank and _as_float(bank.get("change_pct")) is not None:
        yday_bits.append(f" · Bank Nifty {bank.get('change_pct'):+.2f}%")
    parts.append("".join(yday_bits) + ".")

    open_q: List[str] = []
    if gift_bias.upper().find("DOWN") >= 0:
        open_q.append("Gap-down open")
    elif gift_bias.upper().find("UP") >= 0:
        open_q.append("Gap-up open")
    else:
        open_q.append("Flat-to-mild gap")
    if psych_level and prev_close >= psych_level:
        open_q.append(f"into **{psych_level:,.0f}** support zone")
    elif nifty and _as_float(nifty.get("change_pct")) is not None and (_as_float(nifty.get("change_pct")) or 0) < 0:
        open_q.append("after a red index session — fade vs hold is the trade")
    else:
        open_q.append("— confirm tape at 9:15, not headline bias")
    parts.append(f"**Open question:** {' '.join(open_q)}.")

    return " ".join(parts)


def build_open_frame_layers(
    *,
    global_bias: str,
    global_factors: List[str],
    participant_brief: Dict[str, Any],
    fii_latest_row: Optional[Dict[str, Any]],
    gift_bias: str,
    gift_premium: Optional[float],
    sector_scan: Dict[str, Any],
    psych_level: Optional[float],
    prev_close: float,
    change_pts: Optional[float],
) -> Dict[str, Any]:
    """Layered open frame — replaces single BEARISH/BULLISH headline."""
    ob = official_participant_bias(participant_brief)
    fii_pos = (fii_latest_row or {}).get("fii_positioning")
    fii_label = str(fii_pos or "—").replace("POSITIONING_", "").replace("_", " ").title()
    if fii_latest_row and fii_latest_row.get("fii_read"):
        fii_read = str(fii_latest_row["fii_read"]).split(";")[0].strip()
    else:
        fii_read = fii_label or "—"

    global_line = global_bias.replace("_", " ")
    if global_factors:
        global_line += " — " + ", ".join(global_factors[:4])

    positioning_line = ob.get("bias", "NEUTRAL")
    if ob.get("read"):
        positioning_line += f" ({ob['read']})"
    positioning_line += f" · FII cash: {fii_read}"

    gap_line = f"{gift_bias.replace('_', ' ')} {_fmt_pts(gift_premium)} vs prev close"

    breadth = sector_scan.get("breadth") or {}
    green = breadth.get("green", 0)
    total = breadth.get("total", 0)
    alignment = sector_scan.get("alignment_label") or "MIXED"

    if gift_bias.upper().find("DOWN") >= 0 and psych_level and prev_close >= psych_level:
        trade_frame = (
            f"Gap down toward {psych_level:,.0f} support — not a clean trend-bearish day; "
            f"watch hold vs breakdown with participant confirmation."
        )
    elif change_pts is not None and change_pts < 0 and green >= max(1, (total or 1) // 2):
        trade_frame = (
            f"Index red yesterday but **{green}/{total}** sectors green — rotation, not broad risk-off."
        )
    elif alignment in ("BROAD_WEAKNESS", "NIFTY_BANK_ALIGNED_DOWN"):
        trade_frame = "Broad / bank weakness yesterday — respect rallies into resistance; dips need OI confirm."
    else:
        trade_frame = "Mixed tape — let 9:15–9:30 participant behaviour set direction; bias is context only."

    return {
        "global_overnight": global_line,
        "yesterday_positioning": positioning_line,
        "pre_open_gap": gap_line,
        "open_trade_frame": trade_frame,
        "official_participant": ob,
    }


def participant_open_inference(participant_brief: Dict[str, Any]) -> str:
    """Plain-language read of official EOD participant book for today's open."""
    if not participant_brief.get("participants"):
        return "No official participant OI filed for prior session yet."
    ob = official_participant_bias(participant_brief)
    bias = str(ob.get("bias") or "")
    fii = (participant_brief.get("participants") or {}).get("FII") or {}
    fut = _as_float(fii.get("index_fut_net_contracts"))
    call = _as_float(fii.get("index_call_oi_net"))
    put = _as_float(fii.get("index_put_oi_net"))
    vol_fut = _as_float(fii.get("index_fut_vol_net"))

    lines: List[str] = []
    if bias == "HEDGED_BEARISH":
        lines.append(
            "FII carry a **hedged bearish** book — short index futures with heavy put OI. "
            "Rallies may meet sell/hedge flow; sharp dips can see put-cover squeezes."
        )
    elif bias == "BEARISH_FUT":
        lines.append("FII net short index futures — directional bearish positioning into the open.")
    elif bias == "PUT_HEAVY":
        lines.append("FII put-heavy in index options — downside hedged; watch for vol crush if gap holds.")
    elif bias == "BULLISH_FUT":
        lines.append("FII net long index futures — supports gap-up follow-through if global allows.")
    else:
        lines.append(f"Official participant bias **{bias.replace('_', ' ')}** — read numbers with caution.")

    if fut is not None and vol_fut is not None:
        if vol_fut < 0 and fut < -100000:
            lines.append(f"Day volume net **added** shorts (fut vol {int(vol_fut):+,}) — bearish reinforcement.")
        elif vol_fut > 0 and fut < 0:
            lines.append(f"Futures vol net **+{vol_fut:+,}** vs short OI — possible short covering / roll.")
    if call is not None and put is not None and put > abs(call) + 50000:
        lines.append(f"Put OI dominates call ({put:+,} vs {call:+,}) — downside tail hedged.")

    return " ".join(lines)


def build_global_phase1_rows(global_desk: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    """(label, status, read) rows for Phase 1 global table."""
    movers = {m.get("id"): m for m in (global_desk.get("global_movers") or [])}
    macro = global_desk.get("macro") or {}
    gb = global_desk.get("global_bias") or {}
    factors = list(gb.get("factors") or [])

    def mover_row(key: str, label: str) -> Tuple[str, str, str]:
        row = movers.get(key) or {}
        chg = _as_float(row.get("change_pct"))
        read = "—"
        if chg is not None:
            if chg > 0.5:
                read = "Risk-on / supportive"
            elif chg < -0.5:
                read = "Risk-off / pressure"
            else:
                read = "Flat / mixed"
        return label, _fmt_pct(chg), read

    rows: List[Tuple[str, str, str]] = [
        mover_row("sp500", "S&P 500"),
        mover_row("nasdaq", "Nasdaq"),
        mover_row("dow", "Dow"),
        mover_row("nikkei", "Nikkei"),
        mover_row("hang_seng", "Hang Seng"),
    ]

    crude = macro.get("crude_wti") or movers.get("crude_wti") or {}
    dxy = macro.get("dxy") or movers.get("dxy") or {}
    usd_inr = macro.get("usd_inr") or {}
    us10y = macro.get("us_10y_yield") or movers.get("us_10y") or {}

    rows.append(("Crude WTI", _fmt_pct(_as_float(crude.get("change_pct"))), "Inflation / energy read"))
    rows.append(("DXY", _fmt_pct(_as_float(dxy.get("change_pct"))), "Dollar strength vs EM"))
    inr_chg = _as_float(usd_inr.get("change_pct"))
    inr_rate = _as_float(usd_inr.get("rate"))
    rows.append(
        (
            "USD/INR",
            f"{inr_rate:.3f}" if inr_rate else "—",
            _fmt_pct(inr_chg) if inr_chg is not None else "FX context",
        )
    )
    rows.append(("US 10Y", _fmt_pct(_as_float(us10y.get("change_pct"))), "Global rates / liquidity"))

    gb_label = str(gb.get("label") or "NEUTRAL").replace("_", " ")
    rows.append(("Global bias", gb_label, "; ".join(factors[:4]) if factors else "—"))
    return rows


def pte_research_footnote(trade_date_label: str) -> str:
    return (
        f"*PTE / desk research:* compare participant theories in "
        f"`desk_research_{trade_date_label}.jsonl` after 09:15 — not part of this pre-open card.*"
    )


def _gap_vs_close(price: Optional[float], prev_close: Optional[float]) -> Tuple[Optional[float], Optional[float]]:
    if price is None or prev_close is None or prev_close <= 0:
        return None, None
    pts = round(price - prev_close, 2)
    pct = round(pts / prev_close * 100, 2)
    return pts, pct


def _load_json_path(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def build_auction_timeline(
    *,
    prev_close: float,
    gift_last: Optional[float],
    gift_premium: Optional[float],
    auction_scan: Dict[str, Any],
    cash_open_scan: Dict[str, Any],
    premarket_scan: Dict[str, Any],
) -> Dict[str, Any]:
    """9:00 auction -> ~9:08 equilibrium -> 9:15 cash open ladder vs yesterday close."""
    checkpoints = auction_scan.get("checkpoints") or {}
    cp900 = checkpoints.get("auction_start_900") or {}
    cp901 = checkpoints.get("premarket_901") or {}
    cp908 = checkpoints.get("auction_eq_908") or {}
    probe = auction_scan.get("probe") or {}
    auction_range = auction_scan.get("auction_range") or {}
    sweep = auction_scan.get("sweep_summary") or {}

    eq = _as_float(cp908.get("equilibrium")) or _as_float(probe.get("equilibrium"))
    eq_at = cp908.get("captured_at") or probe.get("observed_at")

    cash_gap = cash_open_scan.get("cash_open_gap") or {}
    cash_open = _as_float(cash_gap.get("reference_open")) or _as_float(
        cash_open_scan.get("premarket_compare", {}).get("kite_cash_open")
    )
    cash_at = cash_open_scan.get("captured_at")

    rows: List[Dict[str, Any]] = []

    prev_pts, prev_pct = None, None
    rows.append(
        {
            "checkpoint": "Yesterday close",
            "time": "T−1 EOD",
            "price": prev_close,
            "vs_close_pts": prev_pts,
            "vs_close_pct": prev_pct,
            "note": "Official NSE cash close",
        }
    )

    if gift_last is not None:
        g_pts, g_pct = _gap_vs_close(gift_last, prev_close)
        rows.append(
            {
                "checkpoint": "GIFT S1 (pre-auction)",
                "time": "≤08:59",
                "price": gift_last,
                "vs_close_pts": g_pts,
                "vs_close_pct": g_pct,
                "note": f"Premium {_fmt_pts(gift_premium)} vs close",
            }
        )

    upper = (
        _as_float(sweep.get("nse_indicative_high"))
        or _as_float(auction_range.get("upper_indicative"))
        or _as_float(probe.get("indicative_high"))
    )
    lower = (
        _as_float(sweep.get("nse_indicative_low"))
        or _as_float(auction_range.get("lower_indicative"))
        or _as_float(probe.get("indicative_low"))
    )
    spread = _as_float(sweep.get("nse_indicative_spread_pts")) or _as_float(auction_range.get("spread_pts"))
    if spread is None and upper is not None and lower is not None:
        spread = round(upper - lower, 2)

    ind900 = _as_float((checkpoints.get("auction_start_900") or {}).get("indicative"))
    if ind900 is None and sweep.get("first_open"):
        ind900 = _as_float((sweep.get("first_open") or {}).get("indicative"))
    if ind900 is not None:
        pts, pct = _gap_vs_close(ind900, prev_close)
        rows.append(
            {
                "checkpoint": "Auction opens (NSE indicative)",
                "time": "09:00",
                "price": ind900,
                "vs_close_pts": pts,
                "vs_close_pct": pct,
                "note": f"First poll · status {(sweep.get('first_open') or {}).get('status', cp900.get('status', '—'))}",
            }
        )

    if upper is not None and lower is not None:
        range_note = auction_range.get("note") or f"Spread {spread:.0f} pts · NSE indicative sweep"
        if sweep.get("open_poll_count"):
            range_note = (
                f"{sweep.get('open_poll_count')} live polls · spread {spread:.0f} pts · "
                f"settled {eq or upper}"
            )
        elif spread == 0:
            range_note = "No live sweep — only equilibrium captured after 9:08 (run --poll-sweep at 9:00 tomorrow)"
        rows.append(
            {
                "checkpoint": "Auction range (upper / lower bid)",
                "time": "09:00–09:08",
                "price": f"{upper:,.2f} / {lower:,.2f}",
                "vs_close_pts": None,
                "vs_close_pct": None,
                "note": range_note,
            }
        )
    kite_high = _as_float(sweep.get("kite_high"))
    kite_low = _as_float(sweep.get("kite_low"))
    if kite_high is not None and kite_low is not None and abs(kite_high - kite_low) >= 0.5:
        rows.append(
            {
                "checkpoint": "Kite indicative path (chart)",
                "time": "09:00–09:08",
                "price": f"{kite_low:,.2f} / {kite_high:,.2f}",
                "vs_close_pts": None,
                "vs_close_pct": None,
                "note": f"Kite last sweep · spread {kite_high - kite_low:.0f} pts (what you see on live chart)",
            }
        )

    ind901 = _as_float(cp901.get("indicative")) or _as_float(premarket_scan.get("kite_preopen"))
    if ind901 is not None and (cp901 or premarket_scan):
        pts, pct = _gap_vs_close(ind901, prev_close)
        if cp901.get("indicative"):
            note = f"NSE indicative · status {cp901.get('status', '—')}"
        else:
            gap_type = premarket_scan.get("cash_open_gap", {}).get("gap_type", "—")
            note = f"Kite pre-open · gap {gap_type}"
        rows.append(
            {
                "checkpoint": "Pre-market scan",
                "time": "09:01",
                "price": ind901,
                "vs_close_pts": pts,
                "vs_close_pct": pct,
                "note": note,
            }
        )

    if eq is not None:
        pts, pct = _gap_vs_close(eq, prev_close)
        vs_gift = round(eq - gift_last, 2) if gift_last is not None else None
        note = "Opening equilibrium (~9:08 match)"
        if vs_gift is not None:
            note += f" · vs GIFT {_fmt_pts(vs_gift)}"
        rows.append(
            {
                "checkpoint": "Equilibrium open",
                "time": "~09:08",
                "price": eq,
                "vs_close_pts": pts,
                "vs_close_pct": pct,
                "note": note,
                "observed_at": eq_at,
            }
        )

    if cash_open is not None:
        pts, pct = _gap_vs_close(cash_open, prev_close)
        vs_eq = round(cash_open - eq, 2) if eq is not None else None
        note = "Kite cash open @ 9:15"
        if vs_eq is not None:
            note += f" · vs equilibrium {_fmt_pts(vs_eq)}"
        rows.append(
            {
                "checkpoint": "Cash market open",
                "time": "09:15",
                "price": cash_open,
                "vs_close_pts": pts,
                "vs_close_pct": pct,
                "note": note,
                "observed_at": cash_at,
            }
        )

    summary = ""
    if cash_open is not None:
        c_pts, c_pct = _gap_vs_close(cash_open, prev_close)
        summary = (
            f"Cash opened at {cash_open:,.2f} — "
            f"{_fmt_pts(c_pts)} pts ({_fmt_pct(c_pct)}) vs yesterday close {prev_close:,.2f}."
        )
        if eq is not None and abs(cash_open - eq) >= 0.5:
            summary += f" vs equilibrium {_fmt_pts(cash_open - eq)}."
    elif eq is not None:
        e_pts, e_pct = _gap_vs_close(eq, prev_close)
        summary = (
            f"Equilibrium marked {eq:,.2f} at ~9:08 — "
            f"{_fmt_pts(e_pts)} pts ({_fmt_pct(e_pct)}) vs yesterday close {prev_close:,.2f}."
        )
        if spread and spread > 0 and upper is not None and lower is not None:
            summary += (
                f" Auction path {lower:,.0f}–{upper:,.0f} ({spread:.0f} pts) before settle."
            )
        else:
            summary += " Confirm cash open at 9:15."
    elif ind900 is not None:
        i_pts, _ = _gap_vs_close(ind900, prev_close)
        summary = (
            f"Auction live — indicative {ind900:,.2f} at 9:00 "
            f"({_fmt_pts(i_pts)} vs close). "
            f"Watch upper/lower range until ~9:08 equilibrium, then 9:15 cash open."
        )
    elif gift_last is not None:
        summary = (
            f"GIFT implies {_fmt_pts(gift_premium)} vs close {prev_close:,.2f}. "
            f"Auction scan at 9:00 not captured yet — run auction_scan.py."
        )

    return {
        "rows": rows,
        "summary": summary,
        "equilibrium": eq,
        "cash_open": cash_open,
        "auction_range": auction_range,
        "has_auction_data": bool(cp900 or eq or upper),
        "has_cash_open": cash_open is not None,
    }


def format_auction_timeline_markdown(timeline: Dict[str, Any]) -> List[str]:
    if not timeline.get("rows"):
        return []
    lines = [
        "## ⏱ Auction timeline (9:00 → 9:08 → 9:15)",
        "",
        "| Checkpoint | Time | Price | vs yesterday | Note |",
        "|------------|------|-------|--------------|------|",
    ]
    for row in timeline["rows"]:
        price = row.get("price")
        if isinstance(price, float):
            price_txt = f"{price:,.2f}"
        else:
            price_txt = str(price or "—")
        pts = row.get("vs_close_pts")
        if pts is not None:
            vs = f"{_fmt_pts(pts)} ({_fmt_pct(row.get('vs_close_pct'))})"
        else:
            vs = "—"
        lines.append(
            f"| {row.get('checkpoint', '—')} | {row.get('time', '—')} | {price_txt} | {vs} | {row.get('note', '—')} |"
        )
    lines.append("")
    if timeline.get("summary"):
        lines.append(f"**{timeline['summary']}**")
        lines.append("")
    return lines


def format_auction_timeline_html(timeline: Dict[str, Any]) -> str:
    if not timeline.get("rows"):
        return ""
    body_rows = []
    for row in timeline["rows"]:
        price = row.get("price")
        if isinstance(price, float):
            price_txt = f"{price:,.2f}"
        else:
            price_txt = str(price or "—")
        pts = row.get("vs_close_pts")
        if pts is not None:
            vs = f"{_fmt_pts(pts)} ({_fmt_pct(row.get('vs_close_pct'))})"
        else:
            vs = "—"
        body_rows.append(
            f"<tr><td>{row.get('checkpoint', '—')}</td><td>{row.get('time', '—')}</td>"
            f"<td>{price_txt}</td><td>{vs}</td><td class='muted'>{row.get('note', '—')}</td></tr>"
        )
    summary = timeline.get("summary") or ""
    return (
        "<div class='card' style='border-left:4px solid var(--warn)'>"
        "<h2>⏱ Auction timeline (9:00 → 9:08 → 9:15)</h2>"
        "<table><thead><tr><th>Checkpoint</th><th>Time</th><th>Price</th>"
        "<th>vs yesterday</th><th>Note</th></tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody></table>"
        f"<p>{summary}</p>"
        "<p class='muted'>Upper / lower = NSE index indicative sweep during pre-open (not stock order book).</p>"
        "</div>"
    )


def build_premarket_one_liner(
    *,
    open_frame: Dict[str, Any],
    gift_last: Optional[float],
    gift_premium: Optional[float],
    gift_bias: str,
    prev_close: float,
    psych_level: Optional[float],
    vix_last: Optional[float],
    expiry_label: str,
    sector_scan: Dict[str, Any],
) -> str:
    breadth = sector_scan.get("breadth") or {}
    green = breadth.get("green", 0)
    total = breadth.get("total", 0)
    best = sector_scan.get("best_performer") or {}

    bits: List[str] = []
    if expiry_label and expiry_label != "—":
        bits.append(f"Expiry **{expiry_label}**")
    if gift_last:
        bits.append(f"GIFT **{gift_last:,.0f}** ({gift_bias.replace('_', ' ')} {_fmt_pts(gift_premium)})")
    bits.append(f"yesterday **{prev_close:,.0f}**")
    if psych_level and prev_close >= psych_level:
        bits.append(f"above **{psych_level:,.0f}**")
    bits.append(open_frame.get("open_trade_frame", ""))
    if best.get("sector"):
        bits.append(f"rotation: {best.get('sector')} led")
    if green and total:
        bits.append(f"{green}/{total} sectors green")
    if vix_last is not None and vix_last < 13.5:
        bits.append(f"VIX **{vix_last:.2f}** — size down premium risk")
    return " · ".join(b for b in bits if b)


def _selftest() -> None:
    assert report_clock_phase(datetime(2026, 7, 21, 8, 30)) == "pre_open"
    assert report_clock_phase(datetime(2026, 7, 21, 9, 5)) == "auction"
    assert report_clock_phase(datetime(2026, 7, 21, 10, 0)) == "cash_open"

    assert live_price_stat_label("pre_open") == "GIFT S1 (live)"
    assert report_product_name("cash_open") == "Cash Open Brief"

    pts, pct = yesterday_change({"prev_close": 25000.0}, 25100.0)
    assert pts == 100.0 and pct == 0.4
    assert yesterday_change({}, 25100.0) == (None, None)

    assert breakout_confirmed_yesterday(25100.0, 25000.0, 50.0) is True
    assert breakout_confirmed_yesterday(25100.0, 25000.0, -50.0) is False
    assert breakout_confirmed_yesterday(24900.0, 25000.0, None) is False

    anchor = build_premarket_anchor(
        at=datetime(2026, 7, 21, 8, 45),
        gift_last=25150.0,
        gift_premium=50.0,
        gift_bias="MILD_UP",
        prev_close=25100.0,
        prev_sess={"prev_close": 25000.0, "session_date": "2026-07-18"},
        sector_scan={
            "breadth": {"green": 6, "total": 9},
            "sectors": [{"sector": "Nifty Bank", "change_pct": 0.8}],
            "best_performer": {"sector": "Nifty IT", "change_pct": 1.2},
            "worst_performer": {"sector": "Nifty Metal", "change_pct": -0.5},
        },
        psych_level=25000.0,
        global_bias="RISK_ON",
        global_factors=["S&P +0.5%"],
    )
    assert "GIFT S1 @ 08:45" in anchor
    assert "25,100.00" in anchor
    assert "Open question" in anchor

    open_frame = build_open_frame_layers(
        global_bias="RISK_ON",
        global_factors=["S&P +0.5%"],
        participant_brief={"participants": {"FII": {"index_fut_net_contracts": -5000}}},
        fii_latest_row={"fii_positioning": "POSITIONING_CHURN", "fii_read": "FII churn; net sell"},
        gift_bias="MILD_UP",
        gift_premium=50.0,
        sector_scan={"breadth": {"green": 6, "total": 9}, "alignment_label": "MIXED"},
        psych_level=25000.0,
        prev_close=25100.0,
        change_pts=100.0,
    )
    assert "FII cash: FII churn" in open_frame["yesterday_positioning"]
    assert open_frame["pre_open_gap"].startswith("MILD UP")

    inference = participant_open_inference({"participants": {}})
    assert "No official participant OI" in inference
    inference2 = participant_open_inference(
        {"participants": {"FII": {"index_fut_net_contracts": -50000, "index_put_oi_net": 200000, "index_call_oi_net": 50000}}}
    )
    assert "put" in inference2.lower() or "bearish" in inference2.lower() or "positioning" in inference2.lower()

    rows = build_global_phase1_rows(
        {
            "global_movers": [{"id": "sp500", "change_pct": 0.6}],
            "macro": {"crude_wti": {"change_pct": -1.2}, "usd_inr": {"rate": 83.2, "change_pct": 0.1}},
            "global_bias": {"label": "RISK_ON", "factors": ["S&P up"]},
        }
    )
    assert rows[0][0] == "S&P 500" and "Risk-on" in rows[0][2]

    footnote = pte_research_footnote("2026-07-21")
    assert "desk_research_2026-07-21.jsonl" in footnote

    timeline = build_auction_timeline(
        prev_close=25100.0,
        gift_last=25150.0,
        gift_premium=50.0,
        auction_scan={
            "checkpoints": {
                "auction_start_900": {"indicative": 25120.0, "status": "OPEN"},
                "auction_eq_908": {"equilibrium": 25125.0, "captured_at": "2026-07-21T09:08:00"},
            },
            "sweep_summary": {
                "nse_indicative_high": 25130.0,
                "nse_indicative_low": 25110.0,
                "equilibrium": 25125.0,
                "open_poll_count": 12,
            },
            "probe": {},
            "auction_range": {"note": "swept 12 polls"},
        },
        cash_open_scan={},
        premarket_scan={},
    )
    assert timeline["has_auction_data"] is True
    assert timeline["equilibrium"] == 25125.0
    md = format_auction_timeline_markdown(timeline)
    assert any("Auction timeline" in line for line in md)
    html = format_auction_timeline_html(timeline)
    assert "<table>" in html

    one_liner = build_premarket_one_liner(
        open_frame=open_frame,
        gift_last=25150.0,
        gift_premium=50.0,
        gift_bias="MILD_UP",
        prev_close=25100.0,
        psych_level=25000.0,
        vix_last=12.5,
        expiry_label="21-Jul-2026",
        sector_scan={"breadth": {"green": 6, "total": 9}, "best_performer": {"sector": "Nifty IT"}},
    )
    assert "VIX" in one_liner and "Expiry" in one_liner

    print("[sources.desk_premarket_frame] selftest OK: anchor/frame/timeline/one-liner builders")


if __name__ == "__main__":
    _selftest()
