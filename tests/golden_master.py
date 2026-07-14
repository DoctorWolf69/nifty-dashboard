"""Golden-master harness for the replay engine (Migration Phase 0).

capture: replay a day fresh through the current engine, scrub run-to-run noise,
         store every frame + generated signal as the fixture (gzipped JSON).
check:   rebuild the same day and deep-diff against the fixture.
         Exit 0 = behavior identical. Any real diff prints its exact path.

Fields in KNOWN_P2 leak the wall clock (Migration Phase 2 fixes them); their
diffs are reported as warnings, not failures. Until P2 lands, avoid running
capture/check across the 09:15-09:45 IST wall window - the ORB gate reads the
real clock there and can flip actual decision fields (blockers), which WILL
fail the check. That failure is correct: it is the bug, observed.

Usage (from the repo root):
    python tests/golden_master.py capture 2026-06-19
    python tests/golden_master.py check   2026-06-19

The fixture lives in tests/golden/ and is machine-local (gitignored): its
whole point is comparing runs on THIS machine across code changes. Regenerate
after any DELIBERATE behavior change and note why in the commit message.
"""

from __future__ import annotations

import gzip
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FIXTURE_DIR = Path(__file__).resolve().parent / "golden"

# Meaningless run-to-run noise: tempdir paths, construction-time wall stamps.
SCRUB = {
    "started_at",
    "signal_journal_file",
    "paper_trades_journal",
    "signal_candidates_journal",
    "live_enriched_at",     # LiveMorningContext wall-clock stamp
    "journal_summary",      # whole subtree: replay tempdir paths + today-dated names
}

# Wall-clock leaks tolerated as warnings while they were being fixed.
# Migration Phase 2 injected the engine clock into the ORB/expiry gates,
# morning context day, FII/DII context day, and greeks/IV time-to-expiry,
# so every formerly-leaking field is now deterministic and compared
# strictly. Add a field here only while a newly found leak awaits its fix.
KNOWN_P2: set = set()


def _split(node, p2_bag, path=""):
    """Copy `node` minus SCRUB keys; divert KNOWN_P2 values into p2_bag."""
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key in SCRUB:
                continue
            if key in KNOWN_P2:
                p2_bag[f"{path}.{key}"] = value
                continue
            out[key] = _split(value, p2_bag, f"{path}.{key}")
        return out
    if isinstance(node, list):
        return [_split(v, p2_bag, f"{path}[{i}]") for i, v in enumerate(node)]
    return node


DIFF_CAP = 20


def _diffs(a, b, path="$", out=None):
    """Collect up to DIFF_CAP difference paths between two JSON-ish trees."""
    if out is None:
        out = []
    if len(out) >= DIFF_CAP:
        return out
    if type(a) is not type(b):
        out.append(f"{path}: type {type(a).__name__} != {type(b).__name__}")
    elif isinstance(a, dict):
        for key in sorted(a.keys() | b.keys()):
            if len(out) >= DIFF_CAP:
                break
            if key not in a:
                out.append(f"{path}.{key}: only in new run")
            elif key not in b:
                out.append(f"{path}.{key}: only in fixture")
            else:
                _diffs(a[key], b[key], f"{path}.{key}", out)
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append(f"{path}: length {len(b)} (fixture) != {len(a)} (new)")
        else:
            for i, (x, y) in enumerate(zip(a, b)):
                if len(out) >= DIFF_CAP:
                    break
                _diffs(x, y, f"{path}[{i}]", out)
    elif a != b:
        out.append(f"{path}: fixture={b!r} new={a!r}")
    return out


def _git_head() -> str:
    out = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
        capture_output=True, text=True,
    )
    return out.stdout.strip() or "unknown"


def _build(day: str):
    from nifty.replay.session import build_timeline

    t0 = time.perf_counter()
    timeline = build_timeline(day)
    build_secs = round(time.perf_counter() - t0, 1)
    # Round-trip through JSON exactly like the cache does (default=str), so
    # fixture and fresh run compare like-for-like types.
    raw = json.loads(json.dumps(
        {"frames": timeline["frames"], "signals": timeline["signals"]},
        default=str,
    ))
    p2_bag: dict = {}
    data = {
        "day": day,
        "commit": _git_head(),
        "build_secs": build_secs,
        "frame_count": len(raw["frames"]),
        "signal_count": len(raw["signals"]),
        "frames": [
            {"t": f["t"], "payload": _split(f["payload"], p2_bag, f"frame[{i}]")}
            for i, f in enumerate(raw["frames"])
        ],
        "signals": _split(raw["signals"], p2_bag, "signals"),
        "p2_fields": p2_bag,
    }
    return data


def _fixture_path(day: str) -> Path:
    return FIXTURE_DIR / f"golden_{day}.json.gz"


def capture(day: str) -> int:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    data = _build(day)
    path = _fixture_path(day)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(data, fh)
    print(f"[capture] {day} @ {data['commit']}: {data['frame_count']} frames, "
          f"{data['signal_count']} signals, build {data['build_secs']}s")
    print(f"[capture] fixture -> {path} ({path.stat().st_size:,} bytes)")
    return 0


def check(day: str) -> int:
    path = _fixture_path(day)
    if not path.exists():
        print(f"[check] no fixture for {day}: run capture first ({path})")
        return 2
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        fixture = json.load(fh)
    # Re-scrub the stored frames so growing SCRUB/KNOWN_P2 later never forces
    # a recapture; newly diverted P2 values merge over the captured bag.
    fixture_p2 = dict(fixture.get("p2_fields") or {})
    fixture["frames"] = [
        {"t": f["t"], "payload": _split(f["payload"], fixture_p2, f"frame[{i}]")}
        for i, f in enumerate(fixture["frames"])
    ]
    fixture["signals"] = _split(fixture["signals"], fixture_p2, "signals")
    fixture["p2_fields"] = fixture_p2
    fresh = _build(day)

    print(f"[check] fixture @ {fixture['commit']} (build {fixture['build_secs']}s) "
          f"vs new @ {fresh['commit']} (build {fresh['build_secs']}s)")

    warnings = 0
    for key in sorted(fixture["p2_fields"].keys() | fresh["p2_fields"].keys()):
        old, new = fixture["p2_fields"].get(key), fresh["p2_fields"].get(key)
        if old != new:
            warnings += 1
            if warnings <= 5:
                print(f"[check] P2-WARN {key}: fixture={old!r} new={new!r}")
    if warnings > 5:
        print(f"[check] P2-WARN ... and {warnings - 5} more wall-clock fields")

    diffs = _diffs(
        {"frames": fresh["frames"], "signals": fresh["signals"]},
        {"frames": fixture["frames"], "signals": fixture["signals"]},
    )
    if diffs:
        for line in diffs:
            print(f"[check] FAIL {line}")
        if len(diffs) >= DIFF_CAP:
            print(f"[check] FAIL ... (capped at {DIFF_CAP})")
        return 1
    print(f"[check] PASS {fresh['frame_count']} frames, "
          f"{fresh['signal_count']} signals identical "
          f"({warnings} wall-clock warnings)")
    return 0


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in {"capture", "check"}:
        print(__doc__)
        return 2
    command, day = sys.argv[1], sys.argv[2]
    return capture(day) if command == "capture" else check(day)


if __name__ == "__main__":
    sys.exit(main())
