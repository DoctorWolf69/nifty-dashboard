"""SourceRouter: one active tick source at a time, with failover hysteresis.

Both providers stream in parallel (Kite primary, Dhan warm standby); the
router forwards only the ACTIVE source's ticks to the engine - feeding both
would double-feed the velocity history and manufacture phantom moves from
inter-broker price differences.

Failover rules (all injectable-clock, testable without sleeping):
  kite -> dhan  when kite has been silent > failover_after seconds AND the
                standby is demonstrably alive (delivered within standby_fresh).
                The liveness guard doubles as idle-market protection: outside
                trading hours neither source delivers, so no spurious flip.
  dhan -> kite  when kite has delivered continuously for recover_after
                seconds (hysteresis - one recovered tick must not flap).

Every transition is recorded on the engine's broker timeline.

Self-check: python -m nifty.market.router
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

PRIMARY = "kite"
STANDBY = "dhan"


class SourceRouter:
    def __init__(
        self,
        state: Any,
        *,
        failover_after: float = 20.0,
        recover_after: float = 30.0,
        standby_fresh: float = 10.0,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self.state = state
        self.failover_after = failover_after
        self.recover_after = recover_after
        self.standby_fresh = standby_fresh
        self.now_fn = now_fn
        self.active = PRIMARY
        self.last_delivery: Dict[str, float] = {PRIMARY: 0.0, STANDBY: 0.0}
        self._primary_back_since: Optional[float] = None

    def sink(self, source: str) -> Callable[[List[Dict[str, Any]]], None]:
        """Per-provider callback: providers always deliver here; only the
        active source's ticks reach the engine."""
        def _on_ticks(ticks: List[Dict[str, Any]]) -> None:
            self.last_delivery[source] = self.now_fn()
            if source == self.active:
                self.state.update_ticks(ticks)
        return _on_ticks

    def step(self) -> None:
        """Periodic supervisor tick - call every few seconds."""
        now = self.now_fn()
        kite_age = now - self.last_delivery[PRIMARY]
        standby_alive = (now - self.last_delivery[STANDBY]) <= self.standby_fresh

        if self.active == PRIMARY:
            kite_ever = self.last_delivery[PRIMARY] > 0
            if kite_ever and kite_age > self.failover_after and standby_alive:
                self.active = STANDBY
                self._primary_back_since = None
                self.state.active_provider = STANDBY
                self.state.record_broker_event(
                    STANDBY, "ACTIVE", f"kite silent {kite_age:.0f}s - failover"
                )
            return

        # Standby active: watch for sustained primary recovery.
        if kite_age <= self.standby_fresh:
            if self._primary_back_since is None:
                self._primary_back_since = now
            elif now - self._primary_back_since >= self.recover_after:
                self.active = PRIMARY
                self._primary_back_since = None
                self.state.active_provider = PRIMARY
                self.state.record_broker_event(
                    PRIMARY, "ACTIVE", "kite stream recovered - failback"
                )
        else:
            self._primary_back_since = None


def _selftest() -> None:
    class FakeState:
        def __init__(self) -> None:
            self.ticks: list = []
            self.events: list = []
            self.active_provider = PRIMARY
        def update_ticks(self, ticks): self.ticks.extend(ticks)
        def record_broker_event(self, provider, event, detail=""):
            self.events.append((provider, event))

    clock = {"t": 1000.0}
    state = FakeState()
    router = SourceRouter(state, failover_after=20, recover_after=30,
                          standby_fresh=10, now_fn=lambda: clock["t"])
    kite, dhan = router.sink(PRIMARY), router.sink(STANDBY)

    kite([{"n": 1}]); dhan([{"n": 91}])
    assert state.ticks == [{"n": 1}]                     # standby dropped

    clock["t"] += 15; router.step()
    assert router.active == PRIMARY                       # within tolerance

    clock["t"] += 10                                      # kite silent 25s...
    router.step()
    assert router.active == PRIMARY                       # ...but standby stale too: no flip

    dhan([{"n": 92}]); router.step()                      # standby alive -> failover
    assert router.active == STANDBY and state.events[-1] == (STANDBY, "ACTIVE")
    dhan([{"n": 93}])
    assert state.ticks[-1] == {"n": 93}                   # dhan now feeds the engine

    kite([{"n": 2}]); router.step()                       # kite back - not yet
    assert router.active == STANDBY
    clock["t"] += 15; kite([{"n": 3}]); router.step()
    assert router.active == STANDBY                       # 15s < recover_after
    clock["t"] += 20; kite([{"n": 4}]); router.step()     # 35s sustained -> failback
    assert router.active == PRIMARY and state.events[-1] == (PRIMARY, "ACTIVE")
    assert {"n": 3} not in state.ticks                    # kite ticks dropped while standby active

    print("[market.router] selftest OK: failover, idle guard, hysteresis failback")


if __name__ == "__main__":
    _selftest()
