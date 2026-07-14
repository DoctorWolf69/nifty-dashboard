"""Dual-write verifier (Migration Phase 3).

After a live day with both stores writing, confirm the slim store captured
exactly what the legacy store did, minus the deliberate omissions:

  expected slim ticks  = legacy option rows where (oi, ltp, volume) changed
                         for that contract
  expected slim spots  = legacy spot rows where ltp changed

Prints PASS/FAIL with counts and the size ratio. Three clean days => retire
the legacy writer (flip --no-persist strategy, see MIGRATION_PLAN.md P3).

Usage (from the repo root):
    python tests/verify_slim.py 2026-07-15
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "data" / "live_nifty_oi"


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    day = sys.argv[1]
    legacy_path = LIVE / f"nifty_oi_ticks_{day}.sqlite"
    slim_path = LIVE / f"nifty_slim_{day}.sqlite"
    for p in (legacy_path, slim_path):
        if not p.exists():
            print(f"[verify] missing {p} - need a dual-write day")
            return 2

    legacy = sqlite3.connect(f"file:{legacy_path}?mode=ro", uri=True)
    slim = sqlite3.connect(f"file:{slim_path}?mode=ro", uri=True)

    # Expected option change-rows from the legacy stream, per contract.
    expected_opt = 0
    last: dict = {}
    for token, oi, ltp, volume in legacy.execute(
        "SELECT token, oi, ltp, volume FROM option_ticks ORDER BY ts, rowid"
    ):
        sig = (oi, ltp, volume)
        if last.get(token) != sig:
            last[token] = sig
            expected_opt += 1

    expected_spot = 0
    prev_ltp = None
    for (ltp,) in legacy.execute("SELECT ltp FROM spot_ticks ORDER BY ts, rowid"):
        if ltp != prev_ltp:
            prev_ltp = ltp
            expected_spot += 1

    got_opt = slim.execute("SELECT COUNT(*) FROM tick").fetchone()[0]
    got_spot = slim.execute("SELECT COUNT(*) FROM spot_tick").fetchone()[0]
    got_candles = slim.execute("SELECT COUNT(*) FROM candle_1m").fetchone()[0]
    got_instruments = slim.execute("SELECT COUNT(*) FROM instrument").fetchone()[0]

    legacy_mb = legacy_path.stat().st_size / 1048576
    slim_mb = slim_path.stat().st_size / 1048576

    # The stores attach at slightly different moments (slim starts with the
    # process, legacy with connect()), so allow a whisker of drift.
    tolerance_opt = max(10, expected_opt // 1000)   # 0.1%
    tolerance_spot = max(10, expected_spot // 1000)
    ok_opt = abs(got_opt - expected_opt) <= tolerance_opt
    ok_spot = abs(got_spot - expected_spot) <= tolerance_spot

    print(f"[verify] {day}")
    print(f"  option ticks : slim {got_opt:,} vs expected {expected_opt:,} "
          f"({'OK' if ok_opt else 'MISMATCH'})")
    print(f"  spot ticks   : slim {got_spot:,} vs expected {expected_spot:,} "
          f"({'OK' if ok_spot else 'MISMATCH'})")
    print(f"  candles      : {got_candles:,} rows, instruments: {got_instruments}")
    print(f"  size         : {legacy_mb:.1f} MB -> {slim_mb:.1f} MB "
          f"({legacy_mb / slim_mb:.1f}x smaller)" if slim_mb else "")
    if ok_opt and ok_spot:
        print("[verify] PASS")
        return 0
    print("[verify] FAIL - investigate before retiring the legacy writer")
    return 1


if __name__ == "__main__":
    sys.exit(main())
