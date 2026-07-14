# Golden-Master Baseline Notes

Every deliberate re-capture of the golden fixture gets an entry here explaining
what moved and why. A re-capture without an entry is a process violation.

---

## 2026-07-14 — Post Migration-Phase-2 baseline (replay fidelity)

**Fixture:** `golden_2026-06-19.json.gz`, engine `2.1.0`, config `9872b249`.
**Replaces:** pre-P2 baseline captured at commit `04f0b90`.

Phase 2 injected the engine clock and as-of-day context into every path that
previously read the wall clock. The evidence run (post-P2 engine vs pre-P2
fixture) showed 3,885 changed wall-clock fields and the following material
diffs — **all attributable, none unexplained**:

| Diff | Old (wall-clock bug) | New | Fix |
|---|---|---|---|
| `morning_context.is_expiry_day` + `expiry_rules.*` | `True`, BANKNIFTY primary, 09:45 window — because the *build day* (Tue) was an expiry day | `False`, NIFTY primary, 09:30 window — 19 Jun 2026 is a Friday | day-threading into `load_morning_bundle` |
| `options_analytics.atm_iv` (and all greeks/IV) | `3.0` (=300% — solver garbage for an already-expired option) | `0.1008` (10.08%, sane ATM IV) | engine clock into `year_fraction_to_expiry` |
| `*.futures_eod.source_date` | `2026-07-13/14` (today-relative) | `2026-06-18` (prev trading day of the replayed day) | as-of-day FII/DII load |
| `morning_context.trade_date` | `2026-07-14` | `2026-06-19` | day-threading |
| ORB / late-session gate fields | frozen to whatever wall time the build ran at | follow the replayed clock through the day | `CLOCK.now()` into gates |

**Headline:** the replayed day now generates **25 signals vs 28** under the
pre-P2 engine. The three suppressed signals were only ever possible because
the gates read the build machine's wall clock — they are trades the live desk
would have blocked (ORB window / late session / non-expiry rules).

**Consequences to be aware of:**
- Backtest results produced before this date measured an engine with
  wrong-day expiry rules, empty/garbage IV, wrong-day FII context, and
  build-time-dependent ORB gates. Treat all pre-P2 backtest numbers
  (including the 19 Jun "7W/10L, net −₹1,741") as invalid; re-run.
- Replay of days archived before session-context journaling existed still
  runs with empty technical levels (documented fidelity boundary — the
  read-back needs the live desk's `nifty_session_{day}.jsonl`).
- The harness's KNOWN_P2 warn-list is now empty; every field is compared
  strictly. Add to it only while a newly found leak awaits its fix.
