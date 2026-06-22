"""Calibrate the normalized OI-velocity alert threshold against the archive.

Scans the archived days, computes the in-session-adaptive normalized velocity
z-score for every (contract, 30s, window) sample, and reports how many samples
would clear each candidate z threshold per day. Pick the z that yields an alert
rate close to your target; optionally write it to .env.

    python -m nifty.replay.calibrate_oiv                 # report table
    python -m nifty.replay.calibrate_oiv --write --target 40
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from statistics import median
from typing import Dict, List

from nifty.paths import ENV_FILE
from nifty.analytics.oi_velocity import WINDOWS, ACTION_WINDOWS
from nifty.replay import loader

GRID = "30s"
STEPS = {w: max(1, sec // 30) for w, sec in WINDOWS.items()}
CANDIDATES = [1.0, 1.25, 1.5, 1.75, 2.0, 2.5]


def _day_scores(day: str) -> List[float]:
    """Per-grid adding-score (max action-window z) using a robust cross-day scale."""
    import pandas as pd

    opts, _spot = loader.load_day_dataframes(day)
    if opts.empty:
        return []
    # robust per-window scale from the day's |Δ| distribution (in-session adaptive proxy)
    per_window_abs: Dict[str, List[float]] = defaultdict(list)
    series: Dict[int, Dict[str, "pd.Series"]] = {}
    for token, grp in opts.groupby("token"):
        oi_grid = grp.set_index("ts_dt")["oi"].resample(GRID).last().ffill()
        series[token] = {}
        for w, steps in STEPS.items():
            d = oi_grid.diff(steps)
            series[token][w] = d
            per_window_abs[w].extend(float(abs(x)) for x in d.dropna() if x)
    scale = {}
    for w, vals in per_window_abs.items():
        if not vals:
            scale[w] = 25000.0
            continue
        med = median(vals)
        mad = median([abs(v - med) for v in vals]) or med
        scale[w] = max(25000.0, 1.4826 * mad if mad else med)

    scores: List[float] = []
    for token, by_w in series.items():
        idx = by_w["1m"].index
        for ts in idx:
            zs = []
            for w in ACTION_WINDOWS:
                val = by_w[w].get(ts)
                if val is None or val != val:  # NaN
                    continue
                zs.append(float(val) / scale[w])
            if zs:
                scores.append(max(zs))
    return scores


def _quantile(sorted_vals: List[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, int(round(p / 100.0 * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def calibrate(percentile: float = 95.0, write: bool = False) -> Dict[str, object]:
    days = loader.available_days()
    counts: Dict[float, int] = {z: 0 for z in CANDIDATES}
    all_scores: List[float] = []
    n_days = max(1, len(days))
    for day in days:
        scores = _day_scores(day)
        all_scores.extend(scores)
        for z in CANDIDATES:
            counts[z] += sum(1 for s in scores if s >= z)
        print(f"[calibrate] {day}: {len(scores)} velocity samples")

    # Pre-gate z-exceedance distribution (real alerts are far fewer — they also
    # require the key-area + sustained + volume gates on top of this z test).
    print("\n  z-threshold | pre-gate samples/day (proxy, NOT final alert count)")
    for z in CANDIDATES:
        print(f"     {z:<6}   | {counts[z] / n_days:.0f}")

    all_scores.sort()
    suggested = round(_quantile(all_scores, percentile), 2)
    print(f"\n  score distribution: p90={_quantile(all_scores,90):.2f} "
          f"p95={_quantile(all_scores,95):.2f} p99={_quantile(all_scores,99):.2f}")
    print(f"  -> suggested OIV_Z_ALERT/OIV_Z_DIM/OIV_Z_UNWIND = {suggested} "
          f"(top {100 - percentile:.0f}% of velocity samples clear the z test)")
    print("  NOTE: rough guide only. Authoritative calibration = set OIV_Z_* in .env, "
          "rebuild a day's replay (python -m nifty.replay.backtest <day> --rebuild) and "
          "check the actual signal count in the report.")

    if write:
        _write_env({"OIV_Z_ALERT": suggested, "OIV_Z_DIM": suggested, "OIV_Z_UNWIND": suggested})
        print(f"  -> wrote OIV_Z_* = {suggested} to {ENV_FILE}")
    return {"days": days, "counts": counts, "suggested_z": suggested}


def _write_env(values: Dict[str, object]) -> None:
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    keys = set(values)
    out, seen = [], set()
    for line in lines:
        k = line.split("=", 1)[0].strip() if "=" in line else ""
        if k in keys:
            out.append(f"{k}={values[k]}")
            seen.add(k)
        else:
            out.append(line)
    for k in keys - seen:
        out.append(f"{k}={values[k]}")
    ENV_FILE.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Calibrate OIV alert threshold on the archive")
    parser.add_argument("--percentile", type=float, default=95.0, help="Score percentile for the z threshold")
    parser.add_argument("--write", action="store_true", help="Write suggested OIV_Z_* to .env")
    args = parser.parse_args()
    calibrate(percentile=args.percentile, write=args.write)


if __name__ == "__main__":
    main()
