#!/usr/bin/env python3
"""
Persistent JSONL/JSON stores for the NIFTY F&O desk.

All intraday archives land under journal/ with date-stamped filenames.
"""

from __future__ import annotations

import json
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from nifty.paths import PROJECT_ROOT as BASE_DIR
JOURNAL_DIR = BASE_DIR / "journal"


def ist_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_str(day: Optional[date] = None) -> str:
    return (day or date.today()).isoformat()


class NiftyJournalStore:
    """Thread-safe append-only desk journals."""

    def __init__(self, journal_dir: Path = JOURNAL_DIR) -> None:
        self.journal_dir = journal_dir
        self.journal_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._dedupe_keys: set[str] = set()

    def _dated_path(self, prefix: str, day: Optional[date] = None, ext: str = "jsonl") -> Path:
        return self.journal_dir / f"{prefix}_{today_str(day)}.{ext}"

    def append_jsonl(self, path: Path, row: Dict[str, Any], dedupe_key: Optional[str] = None) -> bool:
        payload = dict(row)
        payload.setdefault("recorded_at", ist_now())
        with self.lock:
            if dedupe_key:
                if dedupe_key in self._dedupe_keys:
                    return False
                self._dedupe_keys.add(dedupe_key)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, default=str, separators=(",", ":")) + "\n")
        return True

    def write_json_snapshot(self, path: Path, payload: Dict[str, Any]) -> None:
        body = dict(payload)
        body.setdefault("recorded_at", ist_now())
        with self.lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(body, indent=2, default=str), encoding="utf-8")

    def append_alert(self, alert: Dict[str, Any], day: Optional[date] = None) -> bool:
        contract = str(alert.get("contract") or "")
        direction = str(alert.get("direction") or "")
        minute_bucket = ist_now()[:16]
        dedupe = f"alert:{contract}:{direction}:{minute_bucket}"
        return self.append_jsonl(
            self._dated_path("nifty_alerts", day),
            {"event": "ALERT", **alert},
            dedupe_key=dedupe,
        )

    def append_behavior(self, event: Dict[str, Any], day: Optional[date] = None) -> bool:
        contract = str(event.get("contract") or "")
        kind = str(event.get("kind") or "")
        minute_bucket = ist_now()[:16]
        dedupe = f"beh:{kind}:{contract}:{minute_bucket}"
        return self.append_jsonl(
            self._dated_path("nifty_behavior", day),
            {"event": "BEHAVIOR", **event},
            dedupe_key=dedupe,
        )

    def append_signal_event(self, event: Dict[str, Any]) -> bool:
        return self.append_paper_trade(event)

    def append_paper_trade(self, event: Dict[str, Any], day: Optional[date] = None) -> bool:
        """All paper lifecycle rows: SIGNAL_GENERATED, SIGNAL_UPDATE, SIGNAL_CLOSED."""
        return self.append_jsonl(
            self._dated_path("nifty_paper_trades", day),
            event,
            dedupe_key=None,
        )

    def append_signal_rejection(self, rejected: Dict[str, Any], day: Optional[date] = None) -> bool:
        key = str(rejected.get("signal_key") or "")
        reason = str(rejected.get("reason") or "")
        dedupe = f"rej:{key}:{reason}:{ist_now()[:16]}"
        return self.append_jsonl(
            self._dated_path("nifty_signal_rejected", day),
            rejected,
            dedupe_key=dedupe,
        )

    @staticmethod
    def _count_jsonl_lines(path: Path) -> int:
        if not path.exists():
            return 0
        count = 0
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    count += 1
        return count

    def journal_day_summary(self, day: Optional[date] = None) -> Dict[str, Any]:
        """Paths and line counts for today's desk journals (review after session)."""
        d = day or date.today()
        files = {
            "paper_trades": self._dated_path("nifty_paper_trades", d),
            "signal_candidates": self._dated_path("nifty_signal_candidates", d),
            "signal_rejected": self._dated_path("nifty_signal_rejected", d),
            "alerts": self._dated_path("nifty_alerts", d),
            "behavior": self._dated_path("nifty_behavior", d),
            "playbook": self._dated_path("nifty_playbook", d),
        }
        legacy_paper = JOURNAL_DIR / "nifty_oi_signals.jsonl"
        return {
            "date": today_str(d),
            "files": {
                key: {
                    "path": str(path),
                    "lines": self._count_jsonl_lines(path),
                }
                for key, path in files.items()
            },
            "legacy_paper_trades": {
                "path": str(legacy_paper),
                "lines": self._count_jsonl_lines(legacy_paper),
            },
        }

    def append_signal_candidate(self, candidate: Dict[str, Any], day: Optional[date] = None) -> bool:
        key = str(candidate.get("signal_key") or "")
        minute_bucket = ist_now()[:16]
        dedupe = f"cand:{key}:{minute_bucket}"
        return self.append_jsonl(
            self._dated_path("nifty_signal_candidates", day),
            candidate,
            dedupe_key=dedupe,
        )

    def append_options_analytics(self, payload: Dict[str, Any], day: Optional[date] = None) -> bool:
        dedupe = f"opt_analytics:{ist_now()[:16]}"
        return self.append_jsonl(
            self._dated_path("nifty_options_analytics", day),
            {"event": "OPTIONS_ANALYTICS", **payload},
            dedupe_key=dedupe,
        )

    def append_session_context(self, context: Dict[str, Any], day: Optional[date] = None) -> bool:
        dedupe = f"session:{ist_now()[:16]}"
        return self.append_jsonl(
            self._dated_path("nifty_session", day),
            {"event": "SESSION_CONTEXT", **context},
            dedupe_key=dedupe,
        )

    def append_gamma_state(self, gamma: Dict[str, Any], day: Optional[date] = None) -> bool:
        active = str((gamma or {}).get("active_signal") or "NONE")
        dedupe = f"gamma:{active}:{ist_now()[:16]}"
        return self.append_jsonl(
            self._dated_path("nifty_gamma", day),
            {"event": "GAMMA_STATE", **gamma},
            dedupe_key=dedupe,
        )

    def write_daily_levels(self, label: str, levels: Dict[str, Any], day: Optional[date] = None) -> None:
        path = self._dated_path("daily_levels", day, ext="json")
        existing: Dict[str, Any] = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                existing = {}
        existing[label] = {**levels, "recorded_at": ist_now()}
        self.write_json_snapshot(path, existing)

    def write_morning_desk(self, payload: Dict[str, Any], day: Optional[date] = None) -> None:
        self.write_json_snapshot(self._dated_path("morning_desk", day, ext="json"), payload)

    def write_global_desk(self, payload: Dict[str, Any], day: Optional[date] = None) -> None:
        self.write_json_snapshot(self._dated_path("global_desk", day, ext="json"), payload)

    def write_instrument_selection(self, payload: Dict[str, Any], day: Optional[date] = None) -> None:
        self.write_json_snapshot(self._dated_path("instrument_selection", day, ext="json"), payload)

    def write_oi_map(self, payload: Dict[str, Any], day: Optional[date] = None) -> None:
        self.write_json_snapshot(self._dated_path("oi_map", day, ext="json"), payload)

    def write_key_levels(self, payload: Dict[str, Any], day: Optional[date] = None) -> None:
        self.write_json_snapshot(self._dated_path("key_levels", day, ext="json"), payload)

    def append_bias_verdict(self, verdict: Dict[str, Any], day: Optional[date] = None) -> bool:
        label = str(verdict.get("verdict") or "")
        dedupe = f"bias:{label}:{ist_now()[:16]}"
        return self.append_jsonl(
            self._dated_path("bias_verdict", day),
            {"event": "BIAS_VERDICT", **verdict},
            dedupe_key=dedupe,
        )

    def build_signal_context(self, state: Any) -> Dict[str, Any]:
        session_context = getattr(state, "session_context", {}) or {}
        gift = session_context.get("gift_nifty") or {}
        tech = session_context.get("technical_levels") or {}
        return {
            "spot": getattr(state, "spot", 0.0),
            "orb_high": getattr(state, "orb_high", 0.0),
            "orb_low": getattr(state, "orb_low", 0.0),
            "day_high": getattr(state, "day_high", 0.0),
            "day_low": getattr(state, "day_low", 0.0),
            "prev_close": getattr(state, "prev_close", 0.0),
            "active_sessions": [row.get("label") for row in session_context.get("active_sessions") or []],
            "gift_premium": gift.get("premium_vs_nse_close"),
            "gift_overnight_bias": gift.get("overnight_bias"),
            "india_vix": tech.get("india_vix"),
            "ema_20": tech.get("ema_20"),
            "ema_50": tech.get("ema_50"),
            "ema_100": tech.get("ema_100"),
            "ema_200": tech.get("ema_200"),
            "combined_bias": (getattr(state, "morning_context", {}) or {}).get("combined_bias"),
            "chosen_instrument": (getattr(state, "morning_context", {}) or {}).get("chosen_instrument"),
            "bias_verdict": getattr(state, "_bias_verdict", None),
        }
