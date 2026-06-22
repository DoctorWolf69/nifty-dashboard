# Change Report — Normalized OI Velocity + Dual Grader

**For:** mentor review · **Scope:** NIFTY desk decision engine · **Status:** implemented, verified
on the local 1-day archive, **not yet validated across multiple days or live**.

---

## 1. Summary

Two changes to the scoring core:

1. **Normalized OI Velocity** — every place the engine used **raw ΔOI** (absolute contract counts)
   to detect/score writer activity now uses a **context-normalized z-score** instead, so a burst is
   judged against what is *normal for that strike, time-of-day, expiry proximity and volatility
   regime*. This **changes live signal selection** (by design).
2. **Dual grader** — a second, negative-inclusive grader (−100..+100) runs **in parallel** with the
   existing 0–100 grader as a **shadow** metric. It does **not** change which trades are taken; it is
   only for side-by-side comparison in the report/email so we can evaluate it before trusting it.

The key headline for review: **change #1 alters behavior and, on the one day tested, made P&L worse**
(see §5). It needs multi-day calibration before any judgement.

---

## 2. What changed — before → after

### 2.1 Detection & scoring (Feature A)

| Component | Before (raw ΔOI) | After (normalized) |
|-----------|------------------|--------------------|
| **Abnormal-alert gate** | `ΔOI(5m) ≥ max(200k, 3×chain median)` OR `ΔOI(1m) ≥ max(75k, …)` | `velocity_score ≥ OIV_Z_ALERT` (z≥1.5) OR `percentile ≥ OIV_PCT_ALERT` (≥90) |
| **Confluence `oi_velocity` dimension (13 pts)** | passed on the raw "chain outlier" flag | passes on normalized z / percentile |
| **Gamma-blast monitor** | raw `ΔOI ≤ −200k`, `OI ≥ 1,000,000` | normalized unwind `z ≤ −1.5`, `OI percentile ≥ 70` |
| **"Top CE/PE Writer Adds" ranking** | sorted by raw `ΔOI(5m)` | sorted by normalized adding-score |

**How the normalization works** (`nifty/analytics/oi_velocity.py`):
```
z = signed_ΔOI_velocity / expected_scale
expected_scale = base_dispersion × atr_factor × time_of_day_factor × dte_factor × liquidity_factor
```
- Windows captured: **30s, 1m, 3m, 5m, 15m**.
- `base_dispersion` = archive-derived std (`data/oi_baselines.json`, keyed by moneyness / time-bin /
  days-to-expiry / window) **blended** with the live cross-sectional dispersion of the chain; falls
  back to in-session estimate when no historical key exists (**hybrid**).
- `atr_factor` from the existing NIFTY **ATR(14)**; `dte_factor` (expiry day churns more);
  `time_of_day_factor` (open/close churn more); `liquidity_factor` from strike OI.
- Outputs per contract: **velocity_score** (headline z), **velocity_percentile**, **acceleration**
  (1m z minus 5m z → accelerating/decelerating).
- All thresholds are env-tunable (`OIV_*` in `.env`).

### 2.2 Grading (Feature B)

| | Before | After |
|--|--------|-------|
| **Positive grader** | 8 dims, each **0 or +weight**, total 0–100, A/B/C/WATCH | unchanged |
| **Paper eligibility** | score ≥ 65 **and** no blockers | **unchanged** (still the positive grader only) |
| **Signed grader** | — | new: each dim **+weight (pass) / −weight (fail)**, total **−100..+100**, signed A/B/C/WATCH — **shadow only** |
| **Report / email** | single grade column | adds **Signed** column + an **aggregate comparison** (grade agreement; which grader's score separated winners from losers better by realized P&L) |

---

## 3. What was working and how it was affected

**Unchanged / not at risk (verified):**
- Live Kite stream, dashboard panels, paper-trade lifecycle, stop/target/exit logic, commission gate,
  morning/EOD pipelines, reports, email, replay/backtest plumbing — all untouched in logic.
- **Paper-trade eligibility decision** — still driven solely by the original positive grader. The
  signed grader cannot change which trades fire.
- **Replay clock isolation** — confirmed the engine still runs live with the wall clock; replay
  unaffected.

**Deliberately changed (this is the point of the task):**
- The **set of signals the engine generates** changes, because the alert gate and `oi_velocity`
  dimension now use the normalized metric. The gamma monitor and writer-add ranking also change.
- The morning/EOD report now renders an extra column + comparison block; the email body has one extra
  line. Cosmetic/additive.

**Other scoring components were explicitly left identical** (key area, sustained add, volume confirm,
spot confirm, writer price, commission, ATM proximity) — only the ΔOI-magnitude basis changed.

---

## 4. Why this is sounder than before (rationale)

Raw thresholds (200k/75k) treat all contexts equally: 100k added at 09:20 on a high-volatility expiry
day was scored the same as 100k at 14:00 on a calm far-dated day, even though the first is unremarkable
and the second is a genuine outlier. Normalizing to a z-score against a context-aware baseline makes
"abnormal" mean *abnormal for these conditions*, which is the correct statistical framing for an
outlier detector.

---

## 5. Before/after evidence (single day: 2026-06-19, replay backtest)

> Same archived ticks, same engine, only the scoring basis differs.

| Metric | Before (raw ΔOI) | After (normalized, default OIV_Z=1.5) |
|--------|------------------|---------------------------------------|
| Signals generated | 17 | 18 |
| Wins / Losses | 7 / 10 | 6 / 12 |
| Win rate | 41% | 33% |
| Net P&L (after commission) | **−₹1,741** | **−₹5,207** |
| Profit factor | 0.72 | 0.32 |

**Honest read:** on this one day the normalized scoring selected *more aggressive / worse* trades at
the default threshold. This is **a single day and an un-calibrated threshold** — not evidence the
approach is bad, but a clear signal that **(a)** thresholds need calibrating and **(b)** we must
evaluate across all available days (and ideally more) before drawing conclusions. The default
`OIV_Z_ALERT=1.5` was a starting guess, not a tuned value.

---

## 6. Open items / what I'd like the mentor to weigh in on

1. **Threshold calibration.** What target signal frequency / selectivity do we want? Current default
   z=1.5 may be too loose. `python -m nifty.replay.calibrate_oiv` gives a distribution guide; the
   authoritative check is rebuilding a day's replay and counting signals.
2. **Baseline thinness.** Only **5 archived days** exist, so the "historical average OI change" is
   weak and the normalizer leans on the in-session estimate. Baselines improve as days accumulate.
3. **ATR is daily(14), not intraday** — the volatility factor is coarse. Worth refining later?
4. **Signed grader bands** (A ≥ +60, B ≥ +30, C ≥ 0) are a first proposal — open to better cutoffs.
5. Should the signed grader eventually gate trades (e.g. require both graders), or stay shadow?

---

## 7. How to review / reproduce

```bash
git pull origin main
python -m nifty.jobs oi-baselines                       # build baselines from the archive
python -m nifty.replay.backtest 2026-06-19 --rebuild    # rebuild a day with new scoring
#   -> data/replay/report_2026-06-19.html  (Signed column + Grader comparison + MFE/MAE)
python -m nifty.replay.calibrate_oiv                    # threshold distribution guide
```

**Code to review:** `nifty/analytics/oi_velocity.py` (normalizer), `nifty/analytics/confluence.py`
(oi_velocity dim + signed grader), `nifty/dashboard/state.py` (`_attach_oiv`, alert gate, gamma,
ranking), `nifty/eod/session_report.py` (`_grader_comparison_block`). Commits `67e60ea..e21b88c`.

**Rollback:** all thresholds are env-driven; reverting commit range `67e60ea..e21b88c` restores the
exact prior raw-ΔOI behavior. The signed grader can be ignored with zero impact (shadow only).
