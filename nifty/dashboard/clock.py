"""Process-global clock so the engine's notion of "now" can be driven by replay.

Live path: methods fall through to the real wall clock — behaviour is identical
to calling time.time() / datetime.now() directly. Replay path (a separate
process): freeze the clock to each tick's timestamp before feeding it to the
engine, so OI-velocity windows, ORB capture and signal cooldowns all reckon time
from the historical tick, not the machine clock.

Only the replay process ever calls freeze(); the live dashboard never imports a
different default, so live stays byte-for-byte unchanged.
"""

from __future__ import annotations

import time as _time
from datetime import datetime
from typing import Optional


class _Clock:
    def __init__(self) -> None:
        self._frozen: Optional[datetime] = None

    def freeze(self, moment: datetime) -> None:
        """Pin 'now' to a specific (naive, IST) datetime — replay mode."""
        self._frozen = moment

    def live(self) -> None:
        """Resume real wall-clock time."""
        self._frozen = None

    @property
    def frozen(self) -> bool:
        return self._frozen is not None

    def now(self) -> datetime:
        return self._frozen if self._frozen is not None else datetime.now()

    def today(self):
        return self.now().date()

    def time(self) -> float:
        # Frozen datetimes are naive/IST; .timestamp() reads them in the machine
        # tz. Velocity uses only differences, so the absolute offset is irrelevant
        # and stays monotonic with the replayed ticks.
        if self._frozen is not None:
            return self._frozen.timestamp()
        return _time.time()

    def now_str(self) -> str:
        return self.now().strftime("%Y-%m-%d %H:%M:%S")


CLOCK = _Clock()
