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
- [ ] Revert commits `67e60ea..e21b88c` (newest-first), one revert commit each
- [ ] Confirm alert gate back to raw ΔOI thresholds (≥200k / ≥75k or 3× chain median)
- [ ] Confirm `oi_velocity` confluence dim back to raw chain-outlier flag
- [ ] Confirm gamma monitor back to raw `ΔOI ≤ −200k`, `OI ≥ 1,000,000`
- [ ] Confirm writer-add ranking sorts by raw ΔOI(5m)
- [ ] Confirm signed/dual grader + `_grader_comparison_block` removed
- [ ] Keep `CHANGE_REPORT_OIV.md` as a historical record (code only reverted)
- [ ] Smoke-test: `python -m nifty.replay.backtest 2026-06-19 --rebuild` runs clean

## Phase 2 — Report A (post-revert baseline)
- [ ] Replay 2026-06-19 to regenerate trades under reverted code
- [ ] Run `python -m nifty.jobs session-report` → standard `journal/report_2026-06-19.html`
- [ ] Save to `reports_comparison/report_A_post_revert_2026-06-19.html`

## Phase 3 — Dynamic Trade Management (mentor spec)
- [ ] Remove fixed profit target (`TARGET_HIT`, `entry*1.50`); keep catastrophic stop (`entry*0.70`)
- [ ] Track per update: entry / current / peak conviction
- [ ] Track per update: current P&L %, MFE %, MAE %, max profit %
- [ ] New exits: `THESIS_INVALIDATED`, `CONVICTION_FADE`, `CONFIRMATION_LOST`,
      `PARTICIPANT_REVERSAL`, `OI_CONVICTION_BROKEN`
- [ ] Journal on close: entry/peak/exit conviction, MFE, MAE, max profit %,
      profit captured %, profit given back %, exit reason
- [ ] Surface new fields in EOD report

## Phase 4 — Report B (post-mentor)
- [ ] Replay 2026-06-19 under mentor code, run `nifty.jobs session-report`
- [ ] Save to `reports_comparison/report_B_post_mentor_2026-06-19.html`
- [ ] Write `reports_comparison/COMPARISON.md`

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
