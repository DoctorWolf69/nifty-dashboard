"""Build archive-derived OI-velocity baselines (the 'historical average OI change').

Scans the tick archive and aggregates the dispersion of |ΔOI velocity| per
(moneyness_offset, time-of-day bin, days-to-expiry bucket, window) into
`data/oi_baselines.json`, which `OIVelocityNormalizer` blends with its live
in-session estimate. Re-runnable as more days accumulate.

    python -m nifty.jobs oi-baselines          # all archived days
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from typing import Any, Dict, Tuple

from nifty.paths import DATA_DIR
from nifty.analytics.oi_velocity import WINDOWS, _tod_bin, _dte_bucket, days_to_expiry
from nifty.replay import loader

BASELINE_PATH = DATA_DIR / "oi_baselines.json"
GRID = "30s"
# window seconds -> number of 30s grid steps
STEPS = {w: max(1, sec // 30) for w, sec in WINDOWS.items()}


def _accumulate_day(day: str, acc: Dict[Tuple[int, str, str, str], list]) -> int:
    import pandas as pd

    opts, spot = loader.load_day_dataframes(day)
    if opts.empty or spot.empty:
        return 0
    dte_bucket = _dte_bucket(days_to_expiry(loader.day_expiry(day), __import__("datetime").date.fromisoformat(day)))

    spot_grid = (
        spot.set_index("ts_dt")["ltp"].resample(GRID).last().ffill()
    )
    samples = 0
    for token, grp in opts.groupby("token"):
        strike = int(grp["strike"].iloc[0])
        oi_grid = grp.set_index("ts_dt")["oi"].resample(GRID).last().ffill()
        if len(oi_grid) < 2:
            continue
        sp = spot_grid.reindex(oi_grid.index).ffill().bfill()
        for w, steps in STEPS.items():
            delta = oi_grid.diff(steps).abs()
            for ts, dval in delta.items():
                if pd.isna(dval) or dval <= 0:
                    continue
                spot_at = sp.get(ts)
                if not spot_at or spot_at <= 0:
                    continue
                moneyness = int(round((strike - spot_at) / 100.0))
                key = (moneyness, _tod_bin(ts.to_pydatetime()), dte_bucket, w)
                rec = acc[key]
                rec[0] += float(dval)          # sum
                rec[1] += float(dval) ** 2      # sumsq
                rec[2] += 1                      # n
                samples += 1
    return samples


def build_baselines() -> Dict[str, Any]:
    days = loader.available_days()
    acc: Dict[Tuple[int, str, str, str], list] = defaultdict(lambda: [0.0, 0.0, 0])
    total = 0
    for day in days:
        n = _accumulate_day(day, acc)
        total += n
        print(f"[oi-baselines] {day}: {n} samples")

    baselines: Dict[str, Dict[str, float]] = {}
    for (moneyness, tod, dte, w), (s, ss, n) in acc.items():
        if n < 5:
            continue
        mean = s / n
        var = max(0.0, ss / n - mean * mean)
        baselines[f"{moneyness}|{tod}|{dte}|{w}"] = {
            "mean": round(mean, 1),
            "std": round(math.sqrt(var), 1),
            "n": n,
        }

    payload = {"days": days, "samples": total, "keys": len(baselines), "baselines": baselines}
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(BASELINE_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    print(f"[oi-baselines] wrote {len(baselines)} keys from {total} samples -> {BASELINE_PATH}")
    return payload


def main() -> None:
    build_baselines()


if __name__ == "__main__":
    main()
