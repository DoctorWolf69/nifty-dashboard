"""ATR- and context-normalized OI velocity scoring.

Raw ΔOI thresholds (e.g. "≥ 200k contracts in 5m") treat a 100k add at 09:20 on a
high-volatility expiry day the same as 100k at 14:00 on a calm far-dated day. This
module normalizes the signed ΔOI velocity of each contract into a context-aware
**z-score** so detection/scoring compares like with like:

    normalized = signed_ΔOI_velocity / expected_scale
    expected_scale = base_dispersion × atr_factor × dte_factor × tod_factor × liq_factor

`base_dispersion` is either an archive-derived historical std (preferred, keyed by
moneyness/time-of-day/DTE) or, when that key is absent, the live cross-sectional
dispersion of the current chain (in-session adaptive). ATR/time/DTE/liquidity enter
as multipliers on the expected scale.

Per contract it emits: velocity_score (headline signed z), velocity_percentile,
acceleration (short-window z minus long-window z), and the per-window scores.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from statistics import median
from typing import Any, Dict, List, Optional, Tuple

# window label -> seconds (also the order short→long for acceleration)
WINDOWS: Dict[str, int] = {"30s": 30, "1m": 60, "3m": 180, "5m": 300, "15m": 900}
ACTION_WINDOWS = ("1m", "3m", "5m")  # windows the alert gate keys on

# Defaults (overridable via the cfg dict / .env OIV_* keys).
DEFAULTS = {
    "atr_ref": 110.0,        # reference NIFTY ATR(14) pts/day; atr_factor = atr_14d/ref
    "scale_floor": 25000.0,  # min dispersion (contracts) to avoid div-by-noise
    "blend_hist": 0.6,       # weight on historical std when both available
}


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _tod_factor(now: datetime) -> float:
    """OI churns hardest near the open and into the close."""
    minutes = now.hour * 60 + now.minute
    if minutes < 9 * 60 + 45:      # 09:15–09:45
        return 1.6
    if minutes >= 15 * 60:         # 15:00–15:30
        return 1.4
    if 11 * 60 <= minutes < 13 * 60 + 30:  # lunch lull
        return 0.8
    return 1.0


def _dte_factor(dte: Optional[int]) -> float:
    if dte is None:
        return 1.0
    return {0: 1.6, 1: 1.3, 2: 1.1}.get(int(dte), 1.0)


def _tod_bin(now: datetime) -> str:
    minutes = now.hour * 60 + now.minute
    if minutes < 9 * 60 + 45:
        return "open"
    if minutes >= 15 * 60:
        return "close"
    if 11 * 60 <= minutes < 13 * 60 + 30:
        return "lunch"
    return "mid"


def _dte_bucket(dte: Optional[int]) -> str:
    if dte is None:
        return "na"
    dte = int(dte)
    if dte <= 0:
        return "expiry"
    if dte == 1:
        return "1"
    if dte <= 3:
        return "2-3"
    return "4+"


def _cdf_percentile(z: float) -> float:
    """Two-sided magnitude percentile from a normal CDF — 0..100."""
    return round(0.5 * (1.0 + math.erf(abs(z) / math.sqrt(2.0))) * 100.0, 1)


@dataclass
class OIVNorm:
    velocity_score: float = 0.0          # headline signed z (window with max |z|)
    velocity_percentile: float = 0.0     # 0..100 (magnitude vs normal)
    acceleration: float = 0.0            # z(1m) - z(5m); + accelerating / - decelerating
    adding_score: float = 0.0            # most-positive z across action windows
    unwind_score: float = 0.0            # most-negative z across action windows
    per_window: Dict[str, float] = field(default_factory=dict)
    raw: Dict[str, int] = field(default_factory=dict)
    expected_scale: Dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "velocity_score": round(self.velocity_score, 2),
            "velocity_percentile": self.velocity_percentile,
            "acceleration": round(self.acceleration, 2),
            "adding_score": round(self.adding_score, 2),
            "unwind_score": round(self.unwind_score, 2),
            "per_window": {k: round(v, 2) for k, v in self.per_window.items()},
            "raw": self.raw,
        }


class OIVelocityNormalizer:
    """Normalize a chain's ΔOI velocity into context-aware z-scores."""

    def __init__(
        self,
        *,
        atr_14d: Optional[float] = None,
        dte: Optional[int] = None,
        now: Optional[datetime] = None,
        baselines: Optional[Dict[str, Any]] = None,
        cfg: Optional[Dict[str, float]] = None,
    ) -> None:
        self.cfg = {**DEFAULTS, **(cfg or {})}
        self.now = now or datetime.now()
        self.dte = dte
        self.baselines = baselines or {}
        ref = self.cfg["atr_ref"] or 1.0
        self.atr_factor = _clamp((atr_14d / ref) if atr_14d else 1.0, 0.5, 2.0)
        self.tod_factor = _tod_factor(self.now)
        self.dte_factor = _dte_factor(dte)
        self._tod_bin = _tod_bin(self.now)
        self._dte_bucket = _dte_bucket(dte)

    # -- baselines --------------------------------------------------------
    def _hist_std(self, moneyness_offset: int, window: str) -> Optional[float]:
        key = f"{moneyness_offset}|{self._tod_bin}|{self._dte_bucket}|{window}"
        rec = self.baselines.get(key)
        if rec and rec.get("std"):
            return float(rec["std"])
        return None

    def _adaptive_scale(self, deltas: List[int]) -> float:
        """Cross-sectional dispersion of |ΔOI| across the live chain (robust)."""
        mags = [abs(d) for d in deltas if d]
        if not mags:
            return self.cfg["scale_floor"]
        med = median(mags)
        # MAD-based robust scale; fall back to median when MAD collapses
        mad = median([abs(m - med) for m in mags]) or med
        return max(self.cfg["scale_floor"], 1.4826 * mad if mad else med)

    # -- main -------------------------------------------------------------
    def normalize_chain(
        self, rows: List[Dict[str, Any]], spot: float
    ) -> Dict[int, OIVNorm]:
        """token -> OIVNorm for every row that carries velocity_* windows."""
        # cross-sectional adaptive dispersion + median OI, per window
        adaptive: Dict[str, float] = {}
        for w in WINDOWS:
            deltas = [int((r.get(f"velocity_{w}") or {}).get("delta") or 0) for r in rows]
            adaptive[w] = self._adaptive_scale(deltas)
        ois = [int(r.get("oi") or 0) for r in rows if int(r.get("oi") or 0) > 0]
        median_oi = median(ois) if ois else 0

        out: Dict[int, OIVNorm] = {}
        for row in rows:
            token = int(row.get("token") or 0)
            strike = int(row.get("strike") or 0)
            oi = int(row.get("oi") or 0)
            moneyness = round((strike - spot) / 100.0) if spot else 0
            liq_factor = _clamp(math.sqrt(oi / median_oi), 0.5, 2.5) if median_oi and oi else 1.0

            norm = OIVNorm()
            blend = self.cfg["blend_hist"]
            for w in WINDOWS:
                raw_delta = int((row.get(f"velocity_{w}") or {}).get("delta") or 0)
                hist = self._hist_std(moneyness, w)
                base = adaptive[w]
                if hist is not None:
                    base = blend * hist + (1 - blend) * base
                    # hist key already encodes tod/dte/moneyness → only ATR on top
                    scale = base * self.atr_factor
                else:
                    scale = base * self.atr_factor * self.dte_factor * self.tod_factor * liq_factor
                scale = max(scale, self.cfg["scale_floor"])
                z = raw_delta / scale
                norm.per_window[w] = z
                norm.raw[w] = raw_delta
                norm.expected_scale[w] = scale

            action = [norm.per_window[w] for w in ACTION_WINDOWS]
            norm.adding_score = max(action) if action else 0.0
            norm.unwind_score = min(action) if action else 0.0
            # headline = window with the largest absolute normalized move
            norm.velocity_score = max(norm.per_window.values(), key=abs, default=0.0)
            norm.velocity_percentile = _cdf_percentile(norm.velocity_score)
            norm.acceleration = norm.per_window.get("1m", 0.0) - norm.per_window.get("5m", 0.0)
            out[token] = norm
        return out


def days_to_expiry(expiry: str, today: Optional[date] = None) -> Optional[int]:
    try:
        exp = datetime.strptime(str(expiry)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    return (exp - (today or date.today())).days


def load_oiv_config() -> Dict[str, float]:
    """Read OIV_* tuning knobs from the environment (falls back to DEFAULTS)."""
    import os

    cfg = dict(DEFAULTS)
    for key, env in (("atr_ref", "OIV_ATR_REF"), ("scale_floor", "OIV_SCALE_FLOOR"), ("blend_hist", "OIV_BLEND_HIST")):
        val = os.getenv(env)
        if val:
            try:
                cfg[key] = float(val)
            except ValueError:
                pass
    return cfg


def load_oi_baselines(path: Optional["Path"] = None) -> Dict[str, Any]:
    """Load archive-derived baselines from data/oi_baselines.json; {} if absent."""
    import json
    from pathlib import Path

    from nifty.paths import DATA_DIR

    p = path or (DATA_DIR / "oi_baselines.json")
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("baselines", data) if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
