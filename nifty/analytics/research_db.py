#!/usr/bin/env python3
"""Institutional learning database — every candidate becomes a research record.

Ported faithfully from quant-desk-engine's nifty_research_db.py
(mentor-authored). No schema or logic changed. Fully self-contained.
Only adaptation: DB path under nifty.paths.DATA_DIR instead of a path
relative to the standalone script's own directory.

Not yet wired into the live pipeline.
Self-check: python -m nifty.analytics.research_db
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from nifty.paths import DATA_DIR

RESEARCH_DB_PATH = DATA_DIR / "research" / "quant_research.sqlite"
MODEL_VERSION = "ev_v1"
WEIGHT_VERSION_KEY = "weight_version"


class ResearchDatabase:
    """SQLite store for candidates, replays, predictions, and governance history."""

    def __init__(self, db_path: Path = RESEARCH_DB_PATH) -> None:
        self.db_path = db_path
        self.lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _ensure_schema(self) -> None:
        with self.lock:
            conn = self._connect()
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS research_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    candidate_id TEXT UNIQUE NOT NULL,
                    signal_key TEXT,
                    session_day TEXT,
                    evaluated_at TEXT,
                    decision TEXT,
                    strike INTEGER,
                    legacy_score REAL,
                    legacy_eligible INTEGER,
                    ev_eligible INTEGER,
                    thesis_probability REAL,
                    expected_value_rupees REAL,
                    risk_level TEXT,
                    market_state TEXT,
                    regime TEXT,
                    model_version TEXT,
                    weight_version TEXT,
                    attribution_json TEXT,
                    ranking_json TEXT,
                    outcome_source TEXT,
                    outcome_win INTEGER,
                    pnl_net_rupees REAL,
                    counterfactual_json TEXT,
                    features_json TEXT,
                    created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS replay_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    experiment_id TEXT NOT NULL,
                    candidate_id TEXT,
                    label TEXT,
                    elapsed_sec INTEGER,
                    snapshot_json TEXT,
                    recorded_at TEXT
                );
                CREATE TABLE IF NOT EXISTS model_predictions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    candidate_id TEXT,
                    model_name TEXT,
                    probability REAL,
                    confidence REAL,
                    expected_value_rupees REAL,
                    trade_decision INTEGER,
                    attribution_json TEXT,
                    recorded_at TEXT
                );
                CREATE TABLE IF NOT EXISTS governance_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_day TEXT,
                    metrics_json TEXT,
                    recorded_at TEXT
                );
                CREATE TABLE IF NOT EXISTS feature_drift_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_day TEXT,
                    report_json TEXT,
                    recorded_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_research_day ON research_records(session_day);
                CREATE INDEX IF NOT EXISTS idx_replay_exp ON replay_snapshots(experiment_id);
                CREATE INDEX IF NOT EXISTS idx_predictions_cand ON model_predictions(candidate_id);
                """
            )
            conn.commit()

    def upsert_research_record(self, record: Dict[str, Any]) -> None:
        with self.lock:
            conn = self._connect()
            conn.execute(
                """
                INSERT INTO research_records (
                    candidate_id, signal_key, session_day, evaluated_at, decision, strike,
                    legacy_score, legacy_eligible, ev_eligible, thesis_probability,
                    expected_value_rupees, risk_level, market_state, regime,
                    model_version, weight_version, attribution_json, ranking_json,
                    outcome_source, outcome_win, pnl_net_rupees, counterfactual_json,
                    features_json, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(candidate_id) DO UPDATE SET
                    ev_eligible=excluded.ev_eligible,
                    thesis_probability=excluded.thesis_probability,
                    expected_value_rupees=excluded.expected_value_rupees,
                    attribution_json=excluded.attribution_json,
                    ranking_json=excluded.ranking_json,
                    outcome_source=excluded.outcome_source,
                    outcome_win=excluded.outcome_win,
                    pnl_net_rupees=excluded.pnl_net_rupees,
                    counterfactual_json=excluded.counterfactual_json
                """,
                (
                    record.get("candidate_id"),
                    record.get("signal_key"),
                    record.get("session_day"),
                    record.get("evaluated_at"),
                    record.get("decision"),
                    record.get("strike"),
                    record.get("legacy_score"),
                    int(bool(record.get("legacy_eligible"))),
                    int(bool(record.get("ev_eligible"))),
                    record.get("thesis_probability"),
                    record.get("expected_value_rupees"),
                    record.get("risk_level"),
                    record.get("market_state"),
                    record.get("regime"),
                    record.get("model_version", MODEL_VERSION),
                    record.get("weight_version"),
                    json.dumps(record.get("attribution") or {}, default=str),
                    json.dumps(record.get("ranking") or {}, default=str),
                    record.get("outcome_source"),
                    int(bool(record.get("outcome_win"))) if record.get("outcome_win") is not None else None,
                    record.get("pnl_net_rupees"),
                    json.dumps(record.get("counterfactual") or {}, default=str),
                    json.dumps(record.get("features") or {}, default=str),
                    record.get("created_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            conn.commit()

    def append_replay_snapshot(
        self,
        *,
        experiment_id: str,
        candidate_id: str,
        label: str,
        elapsed_sec: int,
        snapshot: Dict[str, Any],
    ) -> None:
        with self.lock:
            conn = self._connect()
            conn.execute(
                """
                INSERT INTO replay_snapshots
                (experiment_id, candidate_id, label, elapsed_sec, snapshot_json, recorded_at)
                VALUES (?,?,?,?,?,?)
                """,
                (
                    experiment_id,
                    candidate_id,
                    label,
                    elapsed_sec,
                    json.dumps(snapshot, default=str),
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            conn.commit()

    def append_model_prediction(self, prediction: Dict[str, Any]) -> None:
        with self.lock:
            conn = self._connect()
            conn.execute(
                """
                INSERT INTO model_predictions
                (candidate_id, model_name, probability, confidence, expected_value_rupees,
                 trade_decision, attribution_json, recorded_at)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    prediction.get("candidate_id"),
                    prediction.get("model_name"),
                    prediction.get("probability"),
                    prediction.get("confidence"),
                    prediction.get("expected_value_rupees"),
                    int(bool(prediction.get("trade_decision"))),
                    json.dumps(prediction.get("attribution") or {}, default=str),
                    prediction.get("recorded_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            conn.commit()

    def append_governance(self, session_day: str, metrics: Dict[str, Any]) -> None:
        with self.lock:
            conn = self._connect()
            conn.execute(
                """
                INSERT INTO governance_history (session_day, metrics_json, recorded_at)
                VALUES (?,?,?)
                """,
                (session_day, json.dumps(metrics, default=str), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            conn.commit()

    def append_feature_drift(self, session_day: str, report: Dict[str, Any]) -> None:
        with self.lock:
            conn = self._connect()
            conn.execute(
                """
                INSERT INTO feature_drift_history (session_day, report_json, recorded_at)
                VALUES (?,?,?)
                """,
                (session_day, json.dumps(report, default=str), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            conn.commit()

    def load_governance_history(self, limit: int = 60) -> List[Dict[str, Any]]:
        with self.lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT session_day, metrics_json, recorded_at FROM governance_history ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for row in rows:
            try:
                metrics = json.loads(row["metrics_json"])
            except json.JSONDecodeError:
                metrics = {}
            out.append({"session_day": row["session_day"], "metrics": metrics, "recorded_at": row["recorded_at"]})
        return out

    def count_records(self) -> Dict[str, int]:
        with self.lock:
            conn = self._connect()
            research = conn.execute("SELECT COUNT(*) FROM research_records").fetchone()[0]
            replay = conn.execute("SELECT COUNT(*) FROM replay_snapshots").fetchone()[0]
            preds = conn.execute("SELECT COUNT(*) FROM model_predictions").fetchone()[0]
        return {"research_records": research, "replay_snapshots": replay, "model_predictions": preds}


_research_db: Optional[ResearchDatabase] = None


def get_research_db() -> ResearchDatabase:
    global _research_db
    if _research_db is None:
        _research_db = ResearchDatabase()
    return _research_db


def _selftest() -> None:
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="research-db-selftest-")) / "test.sqlite"
    db = ResearchDatabase(tmp)

    db.upsert_research_record({
        "candidate_id": "c1", "signal_key": "23000:PE:BUY_CE", "session_day": "2026-06-19",
        "decision": "BUY_CE", "strike": 23000, "legacy_eligible": True, "ev_eligible": True,
        "thesis_probability": 68.0, "expected_value_rupees": 2200.0, "outcome_win": True,
        "pnl_net_rupees": 500.0,
    })
    counts = db.count_records()
    assert counts["research_records"] == 1

    # Upsert with the same candidate_id updates rather than duplicates.
    db.upsert_research_record({"candidate_id": "c1", "signal_key": "23000:PE:BUY_CE",
                               "session_day": "2026-06-19", "ev_eligible": False})
    assert db.count_records()["research_records"] == 1

    db.append_governance("2026-06-19", {"sample_size": 10})
    db.append_feature_drift("2026-06-19", {"status": "OK"})
    history = db.load_governance_history()
    assert len(history) == 1 and history[0]["metrics"]["sample_size"] == 10

    db.append_replay_snapshot(experiment_id="e1", candidate_id="c1", label="t0", elapsed_sec=0, snapshot={"a": 1})
    db.append_model_prediction({"candidate_id": "c1", "model_name": "ev_v1", "probability": 0.68})
    counts2 = db.count_records()
    assert counts2["replay_snapshots"] == 1 and counts2["model_predictions"] == 1

    # get_research_db() is a process-wide singleton.
    assert get_research_db() is get_research_db()

    print("[analytics.research_db] selftest OK: schema, upsert-dedup, governance/drift history, singleton")


if __name__ == "__main__":
    _selftest()
