"""Precompute a day's replay once, cache it, then serve frames instantly.

Replaying a full day tick-by-tick through the engine costs ~100s of feed plus
~0.2s per signal evaluation — far too slow to do on every slider move. So we do
it ONCE per day: feed all ticks, run the engine on a fixed cadence, and snapshot
the full dashboard payload every STEP_SEC into a compact timeline cached to
`data/replay/timeline_{day}.json.gz`. The slider then maps to the nearest frame
(an array lookup — instant), and the engine is never in the request path.

The same build pass yields the generated signals used by the backtest.
"""

from __future__ import annotations

import gzip
import json
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from nifty.paths import DATA_DIR
from nifty.dashboard.clock import CLOCK
from nifty.dashboard.state import OIVelocityState
from nifty.replay import loader

STEP_SEC = 30.0          # frame + evaluation cadence (slider granularity)
FEED_COALESCE_SEC = 2.0  # collapse sub-2s ticks to the latest per contract

REPLAY_OUT = DATA_DIR / "replay"


def _timeline_path(day: str) -> Path:
    return REPLAY_OUT / f"timeline_{day}.json.gz"


def _epoch(ts: str) -> float:
    return datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S").timestamp()


def build_timeline(day: str) -> Dict[str, Any]:
    """Single full replay pass → list of frames + generated signals. Persists gzip."""
    REPLAY_OUT.mkdir(parents=True, exist_ok=True)
    replay_dir = Path(tempfile.mkdtemp(prefix=f"nifty-replay-{day}-"))
    state = OIVelocityState(replay_dir=replay_dir)
    state.set_instruments(loader.load_instruments(day), spot=0.0, expiry=loader.day_expiry(day))

    frames: List[Dict[str, Any]] = []
    start_ts: Optional[str] = None
    end_ts: Optional[str] = None
    last_eval: Optional[datetime] = None
    pending: Dict[int, Dict[str, Any]] = {}
    pending_dt: Optional[datetime] = None
    pending_ts: Optional[str] = None

    def flush_pending() -> None:
        nonlocal pending, pending_dt
        if pending and pending_dt is not None:
            CLOCK.freeze(pending_dt)
            state.update_ticks(list(pending.values()))
            pending = {}

    try:
        for ts, dt, bucket in loader.iter_tick_groups(day):
            if start_ts is None:
                start_ts = ts
            end_ts = ts
            # coalesce sub-FEED_COALESCE_SEC ticks: keep latest per token
            if pending_dt is not None and (dt - pending_dt).total_seconds() >= FEED_COALESCE_SEC:
                flush_pending()
            for tick in bucket:
                pending[int(tick["instrument_token"])] = tick
            pending_dt = dt
            pending_ts = ts
            # frame on cadence
            if last_eval is None or (dt - last_eval).total_seconds() >= STEP_SEC:
                flush_pending()
                CLOCK.freeze(dt)
                payload = state.snapshot()
                frames.append({"t": ts, "epoch": _epoch(ts), "payload": payload})
                last_eval = dt
        flush_pending()
        if pending_ts is not None and (not frames or frames[-1]["t"] != pending_ts):
            CLOCK.freeze(datetime.strptime(pending_ts[:19], "%Y-%m-%d %H:%M:%S"))
            frames.append({"t": pending_ts, "epoch": _epoch(pending_ts), "payload": state.snapshot()})
    finally:
        CLOCK.live()

    timeline = {
        "day": day,
        "start": start_ts,
        "end": end_ts,
        "step_sec": STEP_SEC,
        "frame_count": len(frames),
        "frames": frames,
        "signals": list(state.signals),
    }
    with gzip.open(_timeline_path(day), "wt", encoding="utf-8") as fh:
        json.dump(timeline, fh, default=str)
    return timeline


def load_timeline(day: str, rebuild: bool = False) -> Dict[str, Any]:
    path = _timeline_path(day)
    if path.exists() and not rebuild:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    return build_timeline(day)


class ReplayTimeline:
    """In-memory wrapper over a (cached) precomputed timeline for fast frame seeks."""

    def __init__(self, day: str, rebuild: bool = False) -> None:
        self.day = day
        self.data = load_timeline(day, rebuild=rebuild)
        self.frames: List[Dict[str, Any]] = self.data.get("frames", [])

    def meta(self) -> Dict[str, Any]:
        return {
            "day": self.day,
            "start": self.data.get("start"),
            "end": self.data.get("end"),
            "step_sec": self.data.get("step_sec"),
            "frame_count": len(self.frames),
        }

    def frame_times(self) -> List[str]:
        return [fr["t"] for fr in self.frames]

    def frame_at(self, target_ts: str) -> Dict[str, Any]:
        """Nearest frame at or before target_ts (clamped to range)."""
        if not self.frames:
            return {"replay": {"day": self.day, "cursor": target_ts, **self.meta()}}
        chosen = self.frames[0]
        for fr in self.frames:
            if fr["t"] <= target_ts:
                chosen = fr
            else:
                break
        payload = dict(chosen["payload"])
        payload["replay"] = {"day": self.day, "cursor": chosen["t"], **self.meta()}
        return payload

    @property
    def signals(self) -> List[Dict[str, Any]]:
        return self.data.get("signals", [])
