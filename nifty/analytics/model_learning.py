#!/usr/bin/env python3
"""
Model learning: calibration, counterfactual replay, and governance metrics.

Ported faithfully from quant-desk-engine's nifty_model_learning.py
(mentor-authored). No formula, threshold, or branch changed. Adaptations:
  - nifty_journal_store.JOURNAL_DIR -> nifty.paths.JOURNAL_DIR
  - nifty_probability_engine -> nifty.analytics.probability_engine (ported)
  - nifty_relationships_lab's 4 journal helpers -> nifty.analytics.journal_reader
    (extracted there specifically so this port didn't need to duplicate them
    or wait on the full relationships_lab/graphs port)
  - DATA_DIR (tick archive) -> nifty.paths.DATA_LIVE_OI; the raw SQL against
    option_ticks(strike, option_type, ts, ltp) matches this repo's legacy
    tick-archive schema unchanged (state.py's LiveDataStore).

run_daily_model_governance's local imports (feature_drift, model_promotion,
research_db) mirror the original's own style - it imports them INSIDE the
function, not at module top, so this file imports cleanly even before those
sibling modules exist. Only calling that one function requires all three to
be ported (feature_drift + research_db are still pending on the todo list;
model_promotion is already ported).

Not yet wired into the live pipeline.
Self-check: python -m nifty.analytics.model_learning
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from nifty.paths import DATA_LIVE_OI, JOURNAL_DIR
from nifty.analytics.journal_reader import (
    _load_jsonl,
    _recorded_ts,
    list_available_journal_days,
    load_paper_trades_from_journal,
)
from nifty.analytics.probability_engine import (
    DEFAULT_EVIDENCE_LOG_ODDS,
    build_direction_evidence,
    estimate_futures_context,
    evaluate_trade_opportunity,
)

CONFIG_DIR = JOURNAL_DIR.parent / "config"
WEIGHTS_PATH = CONFIG_DIR / "evidence_weights.json"

NIFTY_LOT_SIZE = 65
STOP_FRACTION = 0.70
TARGET_FRACTION = 1.50
MAX_HOLD_MINUTES = 90
MIN_FEATURE_SAMPLES = 8
LAPLACE_ALPHA = 1.0

CALIBRATED_FEATURES = tuple(DEFAULT_EVIDENCE_LOG_ODDS.keys())


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_day(value: str) -> date:
    return datetime.strptime(value[:10], "%Y-%m-%d").date()


def _candidate_id(row: Dict[str, Any]) -> str:
    return f"{row.get('signal_key', '')}:{str(row.get('evaluated_at') or row.get('recorded_at') or '')[:19]}"


def load_signal_candidates(
    journal_dir: Path = JOURNAL_DIR,
    *,
    days: Optional[List[str]] = None,
    limit_per_day: int = 2000,
) -> List[Dict[str, Any]]:
    """Load SIGNAL_CANDIDATE rows across session days."""
    day_list = days or list_available_journal_days(journal_dir)
    rows: List[Dict[str, Any]] = []
    for day_str in day_list:
        path = journal_dir / f"nifty_signal_candidates_{day_str}.jsonl"
        for row in _load_jsonl(path, limit=limit_per_day):
            if str(row.get("event") or "") == "SIGNAL_CANDIDATE":
                row["_session_day"] = day_str
                rows.append(row)
    return rows


def extract_feature_flags(candidate: Dict[str, Any]) -> Dict[str, bool]:
    """Boolean feature vector for calibration / importance."""
    intent = candidate.get("intent_filter") or {}
    chain_bias = candidate.get("chain_bias") or {}
    futures_ctx = estimate_futures_context(
        str(candidate.get("decision") or ""),
        futures_alignment=candidate.get("futures_alignment"),
        spot_v5_delta=_as_float(candidate.get("spot_5m_delta")),
        chain_bias_label=str(chain_bias.get("label") or ""),
    )
    evidence = build_direction_evidence(
        candidate=candidate,
        intent=intent,
        chain_bias=chain_bias,
        futures_ctx=futures_ctx,
    )
    flags = {key: False for key in CALIBRATED_FEATURES}
    for key in evidence:
        if key in flags:
            flags[key] = True
    return flags


def _tick_db_path(day: date) -> Path:
    return DATA_LIVE_OI / f"nifty_oi_ticks_{day.isoformat()}.sqlite"


def _load_spot_series(journal_dir: Path, day: date) -> List[Tuple[float, float]]:
    points: List[Tuple[float, float]] = []
    for row in _load_jsonl(journal_dir / f"decision_engine_{day.isoformat()}.jsonl", limit=5000):
        ts = _recorded_ts(row)
        spot = _as_float(row.get("spot"))
        if ts and spot > 0:
            points.append((ts, spot))
    for row in _load_jsonl(journal_dir / f"nifty_options_analytics_{day.isoformat()}.jsonl", limit=5000):
        if row.get("error"):
            continue
        ts = _recorded_ts(row)
        spot = _as_float(row.get("spot"))
        if ts and spot > 0:
            points.append((ts, spot))
    points.sort(key=lambda item: item[0])
    return points


def _query_option_ticks(
    db_path: Path,
    *,
    strike: int,
    option_type: str,
    after_ts: float,
    until_ts: Optional[float] = None,
) -> List[Tuple[float, float]]:
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        after_iso = datetime.fromtimestamp(after_ts).strftime("%Y-%m-%d %H:%M:%S")
        sql = """
            SELECT ts, ltp FROM option_ticks
            WHERE strike = ? AND option_type = ? AND ts >= ? AND ltp > 0
        """
        params: List[Any] = [strike, option_type, after_iso]
        if until_ts:
            sql += " AND ts <= ?"
            params.append(datetime.fromtimestamp(until_ts).strftime("%Y-%m-%d %H:%M:%S"))
        sql += " ORDER BY ts ASC"
        rows = cur.execute(sql, params).fetchall()
        conn.close()
    except sqlite3.Error:
        return []
    out: List[Tuple[float, float]] = []
    for ts_str, ltp in rows:
        try:
            ts = datetime.strptime(str(ts_str)[:19], "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            continue
        out.append((ts, _as_float(ltp)))
    return out


def _spot_at(spot_series: List[Tuple[float, float]], ts: float) -> float:
    if not spot_series:
        return 0.0
    best = spot_series[0][1]
    for t, price in spot_series:
        if t <= ts:
            best = price
        else:
            break
    return best


def simulate_counterfactual_trade(
    candidate: Dict[str, Any],
    *,
    journal_dir: Path = JOURNAL_DIR,
    lot_size: int = NIFTY_LOT_SIZE,
) -> Dict[str, Any]:
    """
    Hypothetical PnL if this candidate had been traded at signal time.
    Uses tick DB when available; otherwise spot-delta proxy.
    """
    entry = _as_float(candidate.get("entry_price"))
    if entry <= 0:
        return {"status": "NO_ENTRY_PRICE", "win": False, "pnl_net_rupees": 0.0}

    day_str = str(candidate.get("_session_day") or "")[:10]
    if not day_str:
        ev_at = str(candidate.get("evaluated_at") or candidate.get("recorded_at") or "")
        day_str = ev_at[:10]
    try:
        day = _parse_day(day_str)
    except ValueError:
        return {"status": "BAD_DATE", "win": False, "pnl_net_rupees": 0.0}

    entry_ts = _recorded_ts(candidate)
    if entry_ts is None:
        return {"status": "NO_TIMESTAMP", "win": False, "pnl_net_rupees": 0.0}

    strike = int(_as_float(candidate.get("strike")))
    entry_side = str(candidate.get("entry_side") or "")
    stop = round(entry * STOP_FRACTION, 2)
    target = round(entry * TARGET_FRACTION, 2)
    until_ts = entry_ts + MAX_HOLD_MINUTES * 60

    db_path = _tick_db_path(day)
    ticks = _query_option_ticks(
        db_path,
        strike=strike,
        option_type=entry_side,
        after_ts=entry_ts,
        until_ts=until_ts,
    )

    exit_price = entry
    exit_reason = "SESSION_END"
    mae_pct = 0.0
    mfe_pct = 0.0

    if ticks:
        for _, ltp in ticks:
            move_pct = ((ltp - entry) / entry) * 100.0
            mae_pct = min(mae_pct, move_pct)
            mfe_pct = max(mfe_pct, move_pct)
            if ltp >= target:
                exit_price = ltp
                exit_reason = "TARGET_HIT"
                break
            if ltp <= stop:
                exit_price = ltp
                exit_reason = "STOP_HIT"
                break
            exit_price = ltp
    else:
        spot_series = _load_spot_series(journal_dir, day)
        entry_spot = _as_float(candidate.get("spot")) or _spot_at(spot_series, entry_ts)
        end_spot = _spot_at(spot_series, until_ts)
        if entry_spot <= 0:
            return {"status": "NO_SPOT_PROXY", "win": False, "pnl_net_rupees": 0.0}
        spot_move = end_spot - entry_spot
        delta_proxy = 0.42
        decision = str(candidate.get("decision") or "")
        if decision == "BUY_CE":
            premium_move = spot_move * delta_proxy
        else:
            premium_move = -spot_move * delta_proxy
        exit_price = max(0.05, entry + premium_move)
        exit_reason = "SPOT_PROXY"
        move_pct = ((exit_price - entry) / entry) * 100.0
        mae_pct = min(0.0, move_pct)
        mfe_pct = max(0.0, move_pct)

    commission = _as_float((candidate.get("commission_check") or {}).get("round_trip_rupees"), 80.0)
    gross = (exit_price - entry) * lot_size
    net = gross - commission
    win = net > 0

    return {
        "status": "OK",
        "simulation_method": "ticks" if ticks else "spot_proxy",
        "entry_price": entry,
        "exit_price": round(exit_price, 2),
        "exit_reason": exit_reason,
        "pnl_gross_rupees": round(gross, 2),
        "pnl_net_rupees": round(net, 2),
        "pnl_pct": round(((exit_price - entry) / entry) * 100.0, 2) if entry else 0.0,
        "mae_pct": round(mae_pct, 2),
        "mfe_pct": round(mfe_pct, 2),
        "win": win,
    }


def build_labeled_dataset(
    journal_dir: Path = JOURNAL_DIR,
    *,
    days: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Merge candidates with actual or counterfactual outcomes."""
    day_list = days or list_available_journal_days(journal_dir)
    paper_by_day_key: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for day_str in day_list:
        trades = load_paper_trades_from_journal(journal_dir, _parse_day(day_str))
        paper_by_day_key[day_str] = {str(t.get("signal_key")): t for t in trades}

    labeled: List[Dict[str, Any]] = []
    for candidate in load_signal_candidates(journal_dir, days=day_list):
        day_str = str(candidate.get("_session_day") or "")
        key = str(candidate.get("signal_key") or "")
        trade = (paper_by_day_key.get(day_str) or {}).get(key)

        if trade and str(trade.get("status")) == "CLOSED":
            outcome = {
                "source": "actual",
                "taken": True,
                "win": _as_float(trade.get("pnl_net_rupees")) > 0,
                "pnl_net_rupees": _as_float(trade.get("pnl_net_rupees")),
                "pnl_pct": _as_float(trade.get("pnl_net_pct")),
                "exit_reason": trade.get("exit_reason"),
            }
        else:
            cf = simulate_counterfactual_trade(candidate, journal_dir=journal_dir)
            outcome = {
                "source": "counterfactual",
                "taken": bool(trade and trade.get("status") == "OPEN"),
                "win": bool(cf.get("win")),
                "pnl_net_rupees": _as_float(cf.get("pnl_net_rupees")),
                "pnl_pct": _as_float(cf.get("pnl_pct")),
                "exit_reason": cf.get("exit_reason"),
                "simulation": cf,
            }

        ev = candidate.get("ev_model")
        if not ev:
            ev = evaluate_trade_opportunity(candidate)

        features = extract_feature_flags(candidate)
        labeled.append(
            {
                "candidate_id": _candidate_id(candidate),
                "signal_key": key,
                "session_day": day_str,
                "decision": candidate.get("decision"),
                "strike": candidate.get("strike"),
                "evaluated_at": candidate.get("evaluated_at"),
                "legacy_paper_eligible": bool(candidate.get("paper_eligible")),
                "legacy_score": _as_float(candidate.get("total_score")),
                "legacy_grade": candidate.get("grade"),
                "ev_trade_eligible": bool(ev.get("trade_eligible")),
                "predicted_probability": _as_float((ev.get("direction") or {}).get("thesis_probability"), 50.0) / 100.0,
                "predicted_ev_rupees": _as_float((ev.get("expected_value") or {}).get("expected_value_rupees")),
                "market_state": ev.get("market_state"),
                "playbook_phase": candidate.get("playbook_phase"),
                "features": features,
                "outcome": outcome,
            }
        )
    return labeled


def _log_odds_from_rates(p_win: float, p_lose: float, alpha: float = LAPLACE_ALPHA) -> float:
    p_win = _clamp_rate(p_win, alpha)
    p_lose = _clamp_rate(p_lose, alpha)
    odds_win = p_win / (1.0 - p_win)
    odds_lose = p_lose / (1.0 - p_lose)
    lr = odds_win / odds_lose if odds_lose > 0 else 1.0
    return math.log(max(min(lr, 20.0), 0.05))


def _clamp_rate(rate: float, alpha: float) -> float:
    return max(alpha / (alpha + 10), min(1.0 - alpha / (alpha + 10), rate))


def calibrate_log_odds(
    labeled_rows: List[Dict[str, Any]],
    *,
    min_samples: int = MIN_FEATURE_SAMPLES,
    regime_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Learn log-odds weights from win/loss labels per feature."""
    if regime_key:
        rows = [r for r in labeled_rows if str(r.get("market_state") or "") == regime_key]
    else:
        rows = labeled_rows

    base_wins = sum(1 for r in rows if r["outcome"]["win"])
    base_n = len(rows)
    base_rate = base_wins / base_n if base_n else 0.5

    weights: Dict[str, float] = {}
    feature_stats: Dict[str, Dict[str, Any]] = {}

    for feature in CALIBRATED_FEATURES:
        present = [r for r in rows if r["features"].get(feature)]
        absent = [r for r in rows if not r["features"].get(feature)]
        p_n = len(present)
        a_n = len(absent)
        if p_n < min_samples or a_n < min_samples:
            weights[feature] = DEFAULT_EVIDENCE_LOG_ODDS.get(feature, 0.0)
            feature_stats[feature] = {
                "n_present": p_n,
                "n_absent": a_n,
                "calibrated": False,
                "win_rate_present": None,
                "win_rate_absent": None,
                "likelihood_ratio": None,
            }
            continue

        p_wins = sum(1 for r in present if r["outcome"]["win"])
        a_wins = sum(1 for r in absent if r["outcome"]["win"])
        p_rate = (p_wins + LAPLACE_ALPHA) / (p_n + 2 * LAPLACE_ALPHA)
        a_rate = (a_wins + LAPLACE_ALPHA) / (a_n + 2 * LAPLACE_ALPHA)
        log_odds = _log_odds_from_rates(p_rate, a_rate)
        weights[feature] = round(log_odds, 4)
        lr = (p_rate / (1 - p_rate)) / (a_rate / (1 - a_rate)) if a_rate < 1 else 1.0
        feature_stats[feature] = {
            "n_present": p_n,
            "n_absent": a_n,
            "calibrated": True,
            "win_rate_present": round(p_rate * 100, 1),
            "win_rate_absent": round(a_rate * 100, 1),
            "likelihood_ratio": round(lr, 3),
            "log_odds_weight": round(log_odds, 4),
        }

    return {
        "regime": regime_key or "GLOBAL",
        "sample_size": base_n,
        "base_win_rate": round(base_rate * 100, 1),
        "weights": weights,
        "feature_stats": feature_stats,
    }


def calibrate_all_regimes(labeled_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Global + per-market-state weights."""
    global_cal = calibrate_log_odds(labeled_rows)
    regimes = sorted({str(r.get("market_state") or "UNKNOWN") for r in labeled_rows})
    by_regime: Dict[str, Any] = {}
    for regime in regimes:
        sub = calibrate_log_odds(labeled_rows, regime_key=regime)
        if sub["sample_size"] >= MIN_FEATURE_SAMPLES * 2:
            by_regime[regime] = sub
    return {"global": global_cal, "by_regime": by_regime}


def feature_importance(labeled_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Rank features by absolute log-odds impact (calibrated or default)."""
    cal = calibrate_log_odds(labeled_rows)
    stats = cal.get("feature_stats") or {}
    ranked: List[Dict[str, Any]] = []
    for feature, meta in stats.items():
        weight = (cal.get("weights") or {}).get(feature, 0.0)
        ranked.append(
            {
                "feature": feature,
                "importance": round(abs(weight), 4),
                "direction": "positive" if weight > 0 else "negative" if weight < 0 else "neutral",
                "log_odds_weight": round(weight, 4),
                **meta,
            }
        )
    ranked.sort(key=lambda row: row["importance"], reverse=True)
    return ranked


def _brier_score(rows: List[Dict[str, Any]]) -> float:
    if not rows:
        return 0.0
    return sum((r["predicted_probability"] - (1.0 if r["outcome"]["win"] else 0.0)) ** 2 for r in rows) / len(rows)


def _log_loss(rows: List[Dict[str, Any]]) -> float:
    if not rows:
        return 0.0
    total = 0.0
    for r in rows:
        p = max(0.01, min(0.99, r["predicted_probability"]))
        y = 1.0 if r["outcome"]["win"] else 0.0
        total += -(y * math.log(p) + (1 - y) * math.log(1 - p))
    return total / len(rows)


def _calibration_buckets(rows: List[Dict[str, Any]], buckets: int = 10) -> List[Dict[str, Any]]:
    if not rows:
        return []
    sorted_rows = sorted(rows, key=lambda r: r["predicted_probability"])
    size = max(1, len(sorted_rows) // buckets)
    out: List[Dict[str, Any]] = []
    for i in range(0, len(sorted_rows), size):
        chunk = sorted_rows[i: i + size]
        if not chunk:
            continue
        pred = statistics.mean(r["predicted_probability"] for r in chunk)
        actual = sum(1 for r in chunk if r["outcome"]["win"]) / len(chunk)
        out.append(
            {
                "bucket": len(out) + 1,
                "n": len(chunk),
                "predicted_win_rate": round(pred * 100, 1),
                "actual_win_rate": round(actual * 100, 1),
                "gap_pp": round((pred - actual) * 100, 1),
            }
        )
    return out


def _confusion(
    rows: List[Dict[str, Any]],
    *,
    gate_key: str,
) -> Dict[str, Any]:
    tp = fp = tn = fn = 0
    for r in rows:
        predict_trade = bool(r.get(gate_key))
        win = bool(r["outcome"]["win"])
        if predict_trade and win:
            tp += 1
        elif predict_trade and not win:
            fp += 1
        elif not predict_trade and win:
            fn += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "true_positive": tp,
        "false_positive": fp,
        "true_negative": tn,
        "false_negative": fn,
        "precision": round(precision * 100, 1),
        "recall": round(recall * 100, 1),
        "f1": round(f1 * 100, 1),
        "false_accept_rate": round(fp / (fp + tn) * 100, 1) if (fp + tn) else 0.0,
        "false_reject_rate": round(fn / (fn + tp) * 100, 1) if (fn + tp) else 0.0,
    }


def _calibration_error(calibration: List[Dict[str, Any]]) -> Dict[str, float]:
    if not calibration:
        return {"ece": 0.0, "mce": 0.0}
    total_n = sum(b.get("n", 0) for b in calibration) or 1
    ece = 0.0
    mce = 0.0
    for bucket in calibration:
        n = bucket.get("n", 0)
        gap = abs(_as_float(bucket.get("gap_pp")) / 100.0)
        ece += (n / total_n) * gap
        mce = max(mce, gap)
    return {"ece": round(ece, 4), "mce": round(mce, 4)}


def _expectancy(rows: List[Dict[str, Any]]) -> float:
    if not rows:
        return 0.0
    return sum(_as_float(r["outcome"].get("pnl_net_rupees")) for r in rows) / len(rows)


def _profit_factor(rows: List[Dict[str, Any]]) -> float:
    wins = sum(_as_float(r["outcome"].get("pnl_net_rupees")) for r in rows if r["outcome"].get("win"))
    losses = abs(sum(_as_float(r["outcome"].get("pnl_net_rupees")) for r in rows if not r["outcome"].get("win")))
    return round(wins / losses, 3) if losses else 0.0


def compute_governance_metrics(labeled_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Daily model governance dashboard payload."""
    ok_rows = [r for r in labeled_rows if r["outcome"].get("source") in {"actual", "counterfactual"}]
    actual_rows = [r for r in ok_rows if r["outcome"]["source"] == "actual"]
    cf_rows = [r for r in ok_rows if r["outcome"]["source"] == "counterfactual"]

    ev_realized = [r["outcome"]["pnl_net_rupees"] for r in ok_rows if r.get("ev_trade_eligible")]
    ev_predicted = [r["predicted_ev_rupees"] for r in ok_rows if r.get("ev_trade_eligible")]

    legacy_conf = _confusion(ok_rows, gate_key="legacy_paper_eligible")
    ev_conf = _confusion(ok_rows, gate_key="ev_trade_eligible")

    shadow = {
        "legacy_would_trade": sum(1 for r in ok_rows if r["legacy_paper_eligible"]),
        "ev_would_trade": sum(1 for r in ok_rows if r["ev_trade_eligible"]),
        "divergence_count": sum(
            1 for r in ok_rows if r["legacy_paper_eligible"] != r["ev_trade_eligible"]
        ),
        "bad_trades_ev_would_reject": sum(
            1
            for r in ok_rows
            if r["legacy_paper_eligible"] and not r["ev_trade_eligible"] and not r["outcome"]["win"]
        ),
        "good_trades_ev_would_reject": sum(
            1
            for r in ok_rows
            if not r["ev_trade_eligible"] and r["outcome"]["win"]
        ),
        "bad_trades_ev_would_accept": sum(
            1 for r in ok_rows if r["ev_trade_eligible"] and not r["outcome"]["win"]
        ),
        "ev_improvement_rupees": round(
            sum(
                -r["outcome"]["pnl_net_rupees"]
                for r in ok_rows
                if r["legacy_paper_eligible"] and not r["ev_trade_eligible"]
            ),
            2,
        ),
    }

    regime_perf: Dict[str, Any] = {}
    for regime in sorted({str(r.get("market_state") or "UNKNOWN") for r in ok_rows}):
        sub = [r for r in ok_rows if str(r.get("market_state")) == regime]
        regime_perf[regime] = {
            "n": len(sub),
            "win_rate": round(sum(1 for r in sub if r["outcome"]["win"]) / len(sub) * 100, 1) if sub else 0,
            "legacy_precision": _confusion(sub, gate_key="legacy_paper_eligible")["precision"],
            "ev_precision": _confusion(sub, gate_key="ev_trade_eligible")["precision"],
        }

    calibration_buckets = _calibration_buckets(ok_rows)
    cal_err = _calibration_error(calibration_buckets)
    ev_taken = [r for r in ok_rows if r.get("ev_trade_eligible")]

    return {
        "sample_size": len(ok_rows),
        "actual_trades": len(actual_rows),
        "counterfactual_labeled": len(cf_rows),
        "brier_score": round(_brier_score(ok_rows), 4),
        "log_loss": round(_log_loss(ok_rows), 4),
        "calibration_error_ece": cal_err["ece"],
        "calibration_error_mce": cal_err["mce"],
        "calibration": calibration_buckets,
        "legacy_gate": legacy_conf,
        "ev_gate": ev_conf,
        "shadow_comparison": shadow,
        "ev_predicted_mean": round(statistics.mean(ev_predicted), 2) if ev_predicted else None,
        "ev_realized_mean": round(statistics.mean(ev_realized), 2) if ev_realized else None,
        "expectancy_rupees": round(_expectancy(ev_taken), 2),
        "profit_factor": _profit_factor(ev_taken),
        "prediction_bias_pp": round(
            (statistics.mean(r["predicted_probability"] for r in ok_rows) * 100)
            - (sum(1 for r in ok_rows if r["outcome"]["win"]) / max(len(ok_rows), 1) * 100),
            2,
        ) if ok_rows else 0.0,
        "regime_performance": regime_perf,
    }


def save_learned_weights(payload: Dict[str, Any], path: Path = WEIGHTS_PATH) -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def load_learned_weights(path: Path = WEIGHTS_PATH) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def run_daily_model_governance(
    trade_date: Optional[date] = None,
    *,
    journal_dir: Path = JOURNAL_DIR,
    lookback_days: int = 30,
    save_weights: bool = True,
) -> Dict[str, Any]:
    """EOD batch: label, calibrate, govern, persist."""
    d = trade_date or date.today()
    available = list_available_journal_days(journal_dir)
    if not available:
        return {"status": "NO_JOURNAL_DATA", "date": d.isoformat()}

    cutoff = d - timedelta(days=lookback_days)
    days = [day for day in available if _parse_day(day) >= cutoff]
    if d.isoformat() not in days:
        days.insert(0, d.isoformat())

    labeled = build_labeled_dataset(journal_dir, days=days)
    calibration = calibrate_all_regimes(labeled)
    importance = feature_importance(labeled)
    governance = compute_governance_metrics(labeled)

    from nifty.analytics.feature_drift import compute_feature_drift
    from nifty.analytics.model_promotion import evaluate_promotion_candidate
    from nifty.analytics.research_db import get_research_db

    drift_report = compute_feature_drift(journal_dir, as_of=d, labeled_rows=labeled)
    research_db = get_research_db()
    gov_history = research_db.load_governance_history(limit=90)
    if not gov_history:
        gov_history = [{"session_day": d.isoformat(), "metrics": governance}]
    promotion = evaluate_promotion_candidate(governance_history=gov_history)
    research_db.append_governance(d.isoformat(), governance)
    research_db.append_feature_drift(d.isoformat(), drift_report)

    counterfactual_rows = [
        {
            "candidate_id": row["candidate_id"],
            "signal_key": row["signal_key"],
            "session_day": row["session_day"],
            "evaluated_at": row["evaluated_at"],
            "legacy_paper_eligible": row["legacy_paper_eligible"],
            "ev_trade_eligible": row["ev_trade_eligible"],
            "predicted_probability": round(row["predicted_probability"] * 100, 1),
            "predicted_ev_rupees": row["predicted_ev_rupees"],
            "outcome": row["outcome"],
        }
        for row in labeled
    ]

    payload = {
        "event": "MODEL_GOVERNANCE",
        "date": d.isoformat(),
        "lookback_days": lookback_days,
        "sessions_included": days,
        "labeled_count": len(labeled),
        "governance": governance,
        "calibration": calibration,
        "feature_importance": importance[:15],
        "feature_drift": drift_report,
        "promotion": promotion,
        "research_db_counts": research_db.count_records(),
        "counterfactual_summary": {
            "total": len(counterfactual_rows),
            "wins_if_taken": sum(1 for r in counterfactual_rows if r["outcome"].get("win")),
            "losses_if_taken": sum(
                1 for r in counterfactual_rows if not r["outcome"].get("win") and r["outcome"].get("source") == "counterfactual"
            ),
        },
        "weights_path": str(WEIGHTS_PATH),
    }

    if save_weights and labeled:
        weights_doc = {
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "training_sessions": days,
            "sample_size": len(labeled),
            "global_weights": calibration["global"]["weights"],
            "regime_weights": {k: v["weights"] for k, v in calibration["by_regime"].items()},
            "feature_stats": calibration["global"]["feature_stats"],
        }
        save_learned_weights(weights_doc)
        payload["weights_saved"] = True

    out_path = journal_dir / f"model_governance_{d.isoformat()}.json"
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    cf_path = journal_dir / f"counterfactual_outcomes_{d.isoformat()}.jsonl"
    with cf_path.open("w", encoding="utf-8") as handle:
        for row in counterfactual_rows:
            handle.write(json.dumps(row, default=str, separators=(",", ":")) + "\n")

    payload["governance_path"] = str(out_path)
    payload["counterfactual_path"] = str(cf_path)
    return payload


def _selftest() -> None:
    # Synthetic labeled rows — same shape build_labeled_dataset produces.
    rows: List[Dict[str, Any]] = []
    for i in range(20):
        win = i % 3 != 0  # ~67% win rate when the feature is present
        rows.append(
            {
                "market_state": "TREND_UP",
                "features": {**{k: False for k in CALIBRATED_FEATURES}, "spot_confirms": True},
                "predicted_probability": 0.65,
                "predicted_ev_rupees": 400.0,
                "legacy_paper_eligible": True,
                "ev_trade_eligible": win,
                "outcome": {"win": win, "source": "actual", "pnl_net_rupees": 500.0 if win else -300.0},
            }
        )
    for i in range(10):
        lose = i % 4 != 0
        rows.append(
            {
                "market_state": "TREND_UP",
                "features": {**{k: False for k in CALIBRATED_FEATURES}},
                "predicted_probability": 0.40,
                "predicted_ev_rupees": 100.0,
                "legacy_paper_eligible": False,
                "ev_trade_eligible": False,
                "outcome": {"win": not lose, "source": "counterfactual", "pnl_net_rupees": 200.0 if not lose else -150.0},
            }
        )

    cal = calibrate_log_odds(rows)
    assert cal["sample_size"] == 30
    assert cal["feature_stats"]["spot_confirms"]["calibrated"] is True
    assert cal["feature_stats"]["spot_confirms"]["win_rate_present"] > cal["feature_stats"]["spot_confirms"]["win_rate_absent"]

    importance = feature_importance(rows)
    assert importance[0]["feature"] == "spot_confirms"  # only calibrated, non-default feature

    governance = compute_governance_metrics(rows)
    assert governance["sample_size"] == 30
    assert 0.0 <= governance["brier_score"] <= 1.0
    assert governance["legacy_gate"]["true_positive"] >= 0

    print("[analytics.model_learning] selftest OK: calibration, feature importance, governance metrics")


if __name__ == "__main__":
    _selftest()
