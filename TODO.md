# TODO — Revert OIV, Dynamic Trade Management, v2 Widgets

Working repo: `nifty-dashboard` (branch `main`). Comparison day: **2026-06-19** (only archived day).

Goal: (1) revert the OI-velocity normalization + dual-grader changes, (2) add the mentor's
Dynamic Trade Management, (3) port feasible v2 dashboard widgets. Produce **two copies of the
normal EOD report** (`nifty.jobs session-report`) — one on the reverted baseline, one after the
mentor changes — for side-by-side comparison.

---

## Phase 0 — Scaffolding
- [x] Create this `TODO.md`
- [x] Create `reports_comparison/` for the two EOD reports + `COMPARISON.md`

## Phase 1 — Revert OIV normalization + dual grader
- [x] Revert commits `67e60ea..e21b88c` (newest-first), one revert commit each
- [x] Confirm alert gate back to raw ΔOI thresholds (≥200k / ≥75k or 3× chain median)
- [x] Confirm `oi_velocity` confluence dim back to raw chain-outlier flag
- [x] Confirm gamma monitor back to raw `ΔOI ≤ −200k`, `OI ≥ 1,000,000`
- [x] Confirm writer-add ranking sorts by raw ΔOI(5m)
- [x] Confirm signed/dual grader + `_grader_comparison_block` removed
- [x] Keep `CHANGE_REPORT_OIV.md` as a historical record (code only reverted)
- [x] Smoke-test: `python -m nifty.replay.backtest 2026-06-19 --rebuild` runs clean
      → reproduces the report's "Before" numbers exactly (17 signals, 7/10, PF 0.72, −₹1,741)

## Phase 2 — Report A (post-revert baseline)
- [x] Replay 2026-06-19 to regenerate trades under reverted code (`nifty.replay.seed_journal`)
- [x] Run `nifty.eod.session_report --date 2026-06-19` → standard `journal/report_2026-06-19.html`
- [x] Save to `reports_comparison/report_A_post_revert_2026-06-19.html` (17 signals, net −1,741)

## Phase 3 — Dynamic Trade Management (mentor spec)
- [x] Remove fixed profit target (`TARGET_HIT`, exit on `entry*1.50`); keep catastrophic stop (`entry*0.70`)
- [x] Track per update: entry / current / peak conviction (`_conviction_score`)
- [x] Track per update: current P&L %, MFE %, MAE %, max profit %
- [x] New exits: `OI_CONVICTION_BROKEN` (thesis INVALIDATED, immediate), `PARTICIPANT_REVERSAL`,
      `CONFIRMATION_LOST`, `CONVICTION_FADE`
- [x] Journal on close: entry/peak/exit conviction, MFE, MAE, max profit %,
      profit captured %, profit given back %, exit reason
- [x] Surface new fields in EOD report (Conv E→P→X, MFE/MAE, Captured/Given-back columns)

## Phase 4 — Report B (post-mentor)
- [x] Replay 2026-06-19 under mentor code, run `nifty.eod.session_report --date 2026-06-19`
- [x] Save to `reports_comparison/report_B_post_mentor_2026-06-19.html` (28 trades, net −769, PF 0.89)
- [x] Write `reports_comparison/COMPARISON.md` (A −1,741/0.72 vs B −769/0.89)

## Phase 5 — Port v2 widgets (structural + feasible data)
- [ ] Tab-based layout (Live / Context / Chain / Journal) over existing panels
- [ ] Customizable widgets: show/hide/reorder with `localStorage` persistence
- [ ] Left rail (morning/session) shell
- [ ] Action panel (right sidebar) shell
- [ ] Scoreboard columns backed by existing data (Trade=paper_eligible, Blockers, Entry/Conv if present)
- [ ] **Deferred (need EV/ML backend)**: Market Intelligence Lab, Model Governance, Intelligence tab,
      scoreboard columns P% / Conf% / EV₹ / Risk / Why / Intent / MP / VReg / Liq / Opt

## Phase 6 — Verify & finalize
- [ ] Dashboard UI loads; tabs + widget show/hide/reorder persist across reload
- [ ] Backtest re-runs cleanly and produces a report
- [ ] All boxes ticked
