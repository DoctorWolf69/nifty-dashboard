# NIFTY F&O Desk — Project Summary

A self-hosted intraday options-trading **decision desk** for NIFTY, built around one thesis:

> **Bias is context. Participant action is truth.** Morning macro/bias sets the watchlist; live
> **Open Interest (OI) velocity** at key levels makes the trade call.

It streams live NIFTY option ticks from Zerodha Kite, detects sustained **writer-adds** (option
sellers defending a strike), grades setups with an 8-dimension confluence score, journals **paper**
trades (it never places real orders), and now also lets you **replay and backtest** any archived day.

---

## 1. What it does

- **Live OI-velocity dashboard** — subscribes to ATM ±4 NIFTY option strikes (CE+PE) + spot, computes
  1m/5m/15m OI velocity per strike, flags abnormal sustained writer-adds at key areas (round numbers,
  OI walls, EMAs, pivots, ORB, day H/L), and surfaces a live read of where walls are building.
- **Signal grading** — every alert is scored by a confluence grader (key area, sustained add, volume
  confirmation, spot alignment, commission viability, futures alignment, etc.). Grade A/B/C/WATCH;
  paper-eligible only above the score threshold with no blockers.
- **Paper trade book** — at most one open paper signal at a time; tracks entry/stop/target, marks to
  the live option price, and exits on target / stop / OI-conviction break. Commission-aware net P&L.
- **Morning + EOD pipelines** — pre-open it builds global/India bias, GIFT-gap, instrument choice, OI
  map (ceiling/floor/max-pain) and key levels; post-close it pulls NSE EOD data (FII/DII, India VIX,
  bhavcopy, participant OI) that feeds the next day's futures bias.
- **Shareable performance report** — a TradingView-style page (summary cards, equity curve, full
  trade table) at a public URL, plus a daily auto-email of the report.
- **Replay + backtest** — re-run the live engine over any archived day with a time slider, and
  backtest its signals against the actual recorded prices.

It is **read-only against the broker** — there is no order-placement code anywhere.

---

## 2. Architecture

```
nifty/
  paths.py            single source of truth for all on-disk locations
  jobs.py             CLI dispatcher for scheduled jobs (holiday-aware)
  core/               journal, commission gate, expiry rules, session timeline
  kite/               spot, key levels, manual token refresh
  sources/            GIFT client/monitor, finstack, NSE OI map, NSE EOD downloader
  morning/            phase capture (pipeline), live morning context, premarket scan
  analytics/          options Greeks/IV, futures (FII/DII), confluence grader
  eod/                EOD filing, intraday session report, email report
  dashboard/          state.py (engine) + app.py (FastAPI/Kite) + clock.py + templates/
  replay/             loader, timeline precompute, backtest, replay web service
deploy/               systemd units + timers, nginx, install.sh
data/  journal/        runtime archives (gitignored)
```

**Engine (`dashboard/state.py`)** — `OIVelocityState` holds per-strike tick history, computes
velocity, runs the alert + signal pipeline, and emits a single JSON `snapshot()` payload that powers
the whole UI. **App (`dashboard/app.py`)** wires the Kite websocket stream and FastAPI routes.
**Clock (`dashboard/clock.py`)** is a process-global time source that lets replay drive "now" from a
tick's timestamp; live uses real wall-clock and is unaffected.

**Producer → file → consumer:** scheduled jobs write dated JSON into `journal/`; the dashboard reads
them to light up bias, levels, OI map and FII/DII context. Missing artifact → that panel is blank,
the dashboard still runs on live OI.

---

## 3. Data storage

| Store | Path | Content |
|-------|------|---------|
| **Tick archive** | `data/live_nifty_oi/nifty_oi_ticks_YYYY-MM-DD.sqlite` | One row per tick per contract: ltp, **OI**, volume, order-book totals, best bid/ask, **full 5-level depth**. ~300 MB/day, ~400k–1M option ticks. `spot_ticks` table for NIFTY index. |
| **Journal** | `journal/*.jsonl` / `*.json` | Engine reasoning: alerts, scored candidates, paper-trade lifecycle, behavior/playbook/gamma, morning + EOD artifacts, rendered reports. |
| **Replay cache** | `data/replay/timeline_{day}.json.gz` | Precomputed frame timeline (see §6). |

Timestamps are server-local **IST**; option ticks before 09:15 are pre-open/stale snapshots, so
analysis filters to 09:15–15:30. Coverage is the **subscribed strikes only** (ATM ±4) — strikes
outside the window are not captured and cannot be backfilled.

---

## 4. Daily operation (Mon–Fri IST, via systemd timers)

| Time | Job |
|------|-----|
| 08:15 | Morning pipeline (global/India bias, instrument, OI map, key levels) |
| 09:01 | Pre-market scan (GIFT gap, desk brief) |
| 09:10 | Dashboard starts |
| 15:35 | GIFT overnight monitor starts (runs to ~03:05) |
| 15:40 | Intraday session report |
| 16:05 | Dashboard stops |
| 18:00 / 18:30 / 19:00 | NSE EOD batches (FII/DII+VIX, equity bhavcopy/delivery, F&O bhavcopy/participant OI) |
| 19:30, 20:00 | EOD retry-missing |
| 19:35 | EOD filing → FII/DII bias for tomorrow |
| 20:15 | Regenerate report + email the zip |
| 03:05 | GIFT monitor stops |

Jobs auto-skip weekends and NSE holidays. Crash-restart is handled by systemd (`Restart=on-failure`).

**Daily token routine (~30s):** Kite access tokens expire every morning and require interactive 2FA.
Open `/kite/login` from any device → it redirects to Zerodha → after 2FA the token is saved to `.env`
and the stream starts; the page then bounces to the dashboard. No SSH needed.

---

## 5. Reports & sharing

- **Live/static report** — `/reports/report_latest.html` (served by nginx straight off disk, so it
  works any hour even after the dashboard stops at 16:05). Summary cards (net P&L, win rate, profit
  factor, max drawdown, avg hold), an equity curve, and the full trade table with entry/exit + hold
  duration. A live `/report` route also renders intraday while the dashboard is up.
- **Auto-email** — the 20:15 job zips the report + signal list + FII/DII filing and sends **one
  shared email** to everyone in `REPORT_EMAIL_TO` + `REPORT_EMAIL_CC` (Gmail app password). Forward
  to WhatsApp manually if needed (no reliable WhatsApp API).

---

## 6. Replay + backtest

Re-runs the **same engine** over an archived day so you see the exact OI velocity, writer-adds,
grades and signals it would have produced live.

- **How it works** — `replay/loader.py` reconstructs the engine's tick dicts from the SQLite archive;
  the engine is fed in time order with the clock frozen to each tick's timestamp. Because replaying a
  full day tick-by-tick is expensive (~100s feed + ~0.2s per evaluation ≈ 26 min for naive seeking),
  each day is **precomputed once** into a gzip frame timeline (full dashboard snapshots every 30s).
  First open of a day builds the cache (a few minutes; ~4 min for the heaviest day); thereafter
  scrubbing is an instant array lookup. Replay writes to a throwaway dir — it never touches the live
  journal or tick SQLite.
- **Replay slider** — `/replay`: pick a day, drag the slider (or Play at 1×–20×); the whole dashboard
  re-renders the historical state.
- **Backtest report** — "Run backtest" marks each generated signal to the **actual stored prices**:
  realized P&L plus max-favorable/adverse excursion (MFE/MAE), rendered in the same report style.
  Example (Jun 19): 17 signals, 7W/10L, net −₹1,741, profit factor 0.72.
- Runs as an **always-on service** (`nifty-replay`, port 8090, nginx `/replay`) so it works outside
  market hours. The live OI+price chart is omitted in replay (all OI/signal panels still populate).

---

## 7. Deployment

Linux VPS (Hostinger), DuckDNS subdomain + Let's Encrypt TLS, nginx reverse proxy.

- `nifty-dashboard` (port 8080) — live desk, started/stopped by timers.
- `nifty-replay` (port 8090) — always-on replay/backtest.
- nginx: `/` and `/kite/*` → 8080; `/replay` + `/api/replay` → 8090; `/reports/` → static `journal/`.
- `deploy/install.sh` installs all systemd units + timers and enables the replay service.
- Secrets (`KITE_*`, `REPORT_EMAIL_*`) live in `.env` on the server, never committed.

```bash
cd /opt/nifty-dashboard && git pull origin main
sudo bash ./deploy/install.sh /opt/nifty-dashboard nifty
# add /replay + /api/replay location blocks to the live nginx config, then:
sudo nginx -t && sudo systemctl reload nginx
```

---

## 8. Tech stack

Python 3.12 · FastAPI + uvicorn · KiteConnect (REST + websocket) · SQLite (WAL) · pandas/scipy ·
Chart.js (frontend) · systemd timers · nginx + certbot. No crypto/AI/DB stack — the desk is fully
self-contained.

---

## 9. Notable design decisions

- **Read-only by design** — no order placement, so a bug can never fire a live trade.
- **Centralized paths + injectable clock** — let the same engine run live and in replay without
  divergence.
- **Precompute-and-cache replay** — the only feasible way to scrub/backtest a full tick day
  interactively.
- **Static report files** — reports are reachable any hour, independent of the dashboard's lifecycle.

## 10. Deliberately out of scope (possible next steps)

- Trailing stops / partial profit-booking exit logic.
- Full-option-chain archival (all strikes + IV/greeks) for richer future backtests.
- Live OI+price chart inside replay; finer-than-30s backtest cadence.

---

# 11. Appendix — Dashboard panels: exact rules, thresholds & blockers

Every panel below is driven by `OIVelocityState.snapshot()` in `dashboard/state.py`; the grader is in
`analytics/confluence.py` and the cost gate in `core/commission.py`. Velocity windows are **1m = 60s,
5m = 300s, 15m = 900s**, computed per contract from its tick history. All thresholds are the literal
constants in code.

## 11.1 "Top CE/PE Writer Adds" + "Abnormal Alerts" panel

An option row is flagged as an **abnormal OI alert** only if **ALL** of these hold:

| Gate | Condition | Constant |
|------|-----------|----------|
| Key area | row sits on a flagged level (see 11.2) | `key_area = True` |
| Sustained | ≥ 3 one-minute buckets of history exist | `SUSTAINED_ADD_MINUTES = 3` |
| Repeated add | ≥ 3 of the last minutes had **OI delta > 0** | `MIN_POSITIVE_MINUTE_ADDS = 3` |
| Volume confirmed | ≥ 3 of those add-minutes also had **volume delta > 0** | `MIN_VOLUME_CONFIRMED_MINUTES = 3` |
| Outlier (either) | **chain_outlier** OR **pct_outlier** | see below |

- **chain_outlier** = `v5.delta ≥ max(200000, median_5m×3)` **OR** `v1.delta ≥ max(75000, median_1m×3)`
  (median = median positive OI delta across the chain that tick).
- **pct_outlier** = `v5.pct ≥ 8%` **OR** `v1.pct ≥ 4%`.

**Direction label** (decides whether it counts as real writing):
- **`WRITERS ADDING`** = price-confirmed: `(CE and spot ≤ strike)` or `(PE and spot ≥ strike)`.
- **`OI ADDING - PRICE NOT CONFIRMED`** = OI up but the option is ITM/ambiguous → treated cautiously
  (still scored, but fails the `writer_price` dimension → `WRITER_NOT_CONFIRMED` blocker).

## 11.2 Key-area detection (`_key_area_reasons`)

A strike is a "key area" if **any** reason matches (these also feed the `key_area` grader dimension):

| Reason | Rule |
|--------|------|
| `near spot` | `|spot − strike| / spot ≤ 0.35%` (`KEY_AREA_DISTANCE_PCT`) |
| `psychological 500 strike` / `round 100 strike` | strike % 500 == 0 / strike % 100 == 0 |
| `top OI wall` | strike is in the **top-2 OI** strikes on its side (CE or PE) |
| day high / low, open, prev close, ORB high / low | within **60 pts** of that level |
| EMA/pivot/Camarilla/Fib labels | within the session technical-level tolerance |
| morning key levels | within 60 pts of a `key_levels` flat level |

## 11.3 PE/CE strike behavior (`_analyze_pe_strike_row`)

Per strike, using 5m velocity and spot 5m delta (`add ≥ +2%`, `unwind ≤ −2%`, `spot flat = |Δ| ≤ 8 pts`):

| Behavior | Condition | Read |
|----------|-----------|------|
| `PE_ADD_SPOT_UP` | PE adding **and** spot up | support confirmed (bullish) |
| `PE_ADD_SPOT_FLAT` | PE adding, spot flat | writers stacking, needs follow-through |
| `PE_ADD_SPOT_DOWN` | PE adding, spot down | divergence — support not working |
| `PE_UNWIND` | PE OI ≤ −2% | support leaving (bearish) |
| `CE_DOMINANT` | CE OI ≥ +2% | overhead pressure |
| `QUIET` | none of the above | no footprint |

## 11.4 Intraday Playbook panel (`_detect_intraday_playbook`)

A gap-aware state machine. **ORB watch comes first** and hard-blocks entries:

- **`ORB_WATCH`** (09:15–09:30) / **`EXPIRY_WATCH`** (09:15–09:45 on expiry) — watch only, no fresh entries.
- Then by gap (`GAP_PLAYBOOK_THRESHOLD = 30 pts` vs prev close): **GAP_DOWN** branch resolves to one of
  `GAP_WEAK · ORB_HOLD · PE_DIVERGENCE · PE_BUILD_930 · EXTENSION · PE_UNWIND · CE_PUSH ·
  RECLAIM_FAILED · ORB_RECLAIMED` based on whether ORB low held, ORB high reclaimed, and PE/CE
  writer behavior at support. **GAP_UP** → `GAP_UP`; no gap → `FLAT_OPEN`.
- The panel also shows pass/warn/fail checks: ORB no-trade, gap context, ORB low held, ORB high
  reclaim, 23100/23200 PE-vs-spot, 9:30 PE build, can-extend, PE unwind, CE push.

## 11.5 Gamma Blast monitor (`_detect_gamma_blast`)

Only strikes within **0.45%** of spot (`GAMMA_NEAR_SPOT_PCT`) are examined.

- **`COMPRESSION`** = both CE and PE OI ≥ **1,000,000** (`GAMMA_HEAVY_OI_MIN`).
- **`GAMMA_BLAST_UP_RISK`** = compression **and** `CE 5m delta ≤ −200,000` (`GAMMA_UNWIND_DELTA_MIN`) and
  `PE delta ≥ 0` and `spot ≥ strike` (call writers covering above → squeeze up).
- **`GAMMA_BLAST_DOWN_RISK`** = mirror (PE unwinding below spot).
- **`EXPIRY_DECAY_UNWIND`** = both sides unwinding ≤ −200,000.

## 11.6 Confluence Scoreboard panel (the grader)

Every writer alert is scored on **8 weighted dimensions (max 100)**:

| Dimension | Max | Passes when |
|-----------|----:|-------------|
| `key_area` | 12 | alert is at a flagged key area |
| `oi_sustained` | 15 | ≥ 3 positive OI minutes |
| `volume_confirm` | 15 | ≥ 3 volume-confirmed add minutes |
| `oi_velocity` | 13 | chain/pct outlier flag present |
| `spot_confirm` | 15 | **BUY_CE:** PE behavior = `PE_ADD_SPOT_UP`; **BUY_PE:** spot 5m ≤ +8 pts |
| `writer_price` | 10 | direction = `WRITERS ADDING` |
| `commission` | 10 | cost gate passes (11.7) |
| `atm_proximity` | 10 | `|spot − strike| ≤ 150 pts` (score scales linearly to 0 at 150) |

**Grade:** A ≥ 80% · B ≥ 65% · C ≥ 50% · else WATCH. **Paper-eligible** = `total_score ≥ 65`
(`TRADE_MIN_CONFLUENCE`) **AND zero blockers**.

**Blockers** (any one → not paper-eligible; shown in the Blockers column):

| Blocker | Raised when |
|---------|-------------|
| `ORB_NO_TRADE` | inside 09:15–09:30 (09:45 expiry) window |
| `LATE_SESSION` | after **15:15** IST (`LATE_SESSION_SIGNAL_CUTOFF`) |
| `MAX_OPEN` | already 1 open paper signal (`MAX_OPEN_SIGNALS = 1`) |
| `THESIS_STACK` | a position is open and same-thesis stacking is blocked |
| `STRIKE_TOO_FAR` | strike > 150 pts from spot |
| `STRIKE_SPACING` | within **100 pts** of an open position's strike (`MIN_OPEN_STRIKE_SPACING`) |
| `DIRECTION_CONFLICT` | single-direction book and this decision ≠ the open one |
| `PE_SPOT_NOT_CONFIRMED` | BUY_CE but PE behavior ≠ `PE_ADD_SPOT_UP` |
| `SPOT_NOT_WEAK` | BUY_PE but spot 5m > +8 pts |
| `COMMISSION_TOO_THIN` | cost gate fails (11.7) |
| `WRITER_NOT_CONFIRMED` | direction = `OI ADDING - PRICE NOT CONFIRMED` |
| `COOLDOWN` | < **600 s** since the last signal on this key |
| `NO_ENTRY_CONTRACT` | entry leg missing or entry price ≤ 0 |
| `FUTURES_MACRO_CONFLICT` | option side fights FII index-futures + live futures OI (11.9) |

## 11.7 Commission gate (`commission_conviction_check`)

Passes when the **gross at target** covers costs with conviction:

```
gross_target ≥ max( min_gross_rupees(500) , round_trip_cost × min_net_profit_multiple(3) )
```

Target premium = `entry × 1.50`. Round-trip cost models Zerodha flat brokerage (₹20/order or 0.03%,
lower) + STT (sell 0.0625%) + exchange + SEBI + GST + stamp, on `lot_size = 65`. Configurable via
`.env` (`NIFTY_LOT_SIZE`, `NIFTY_BROKERAGE_PER_ORDER`, `NIFTY_MIN_NET_PROFIT_MULTIPLE`,
`NIFTY_MIN_GROSS_RUPEES`).

## 11.8 "System Trade Decisions" panel — paper take + manage

**Taking a paper trade** (`_maybe_take_paper_signal`): from the eligible candidates, pick the highest
`total_score`; if both BUY_CE and BUY_PE are eligible (fresh book), keep only the higher-scoring side
(single-direction book). Hard pre-checks: not in ORB/expiry/late window, < 1 open, no same-key open,
≥ 600 s since this key, entry price > 0.

On entry the position records: `entry_price`, **`stop_price = entry × 0.70` (−30% catastrophic stop)**,
`target_price = entry × 1.50` (+50%, stored for reference/journaling only — it does **not** drive an
exit), commission fields, confluence score/grade/dimensions.

**Managing / exit** (`_update_open_signals`, every snapshot): marks to live option ltp, re-evaluates OI
conviction and entry-confirmation factors every tick, and exits dynamically — there is no fixed profit
target. Priority order: catastrophic stop → thesis invalidation → participant reversal → confirmation
loss → conviction fade.

| Exit reason | Condition |
|-------------|-----------|
| `STOP_HIT` | current ≤ stop (−30%, unchanged catastrophic floor) |
| `OI_CONVICTION_BROKEN` | OI conviction level is `INVALIDATED` |
| `PARTICIPANT_REVERSAL` | followed writer's 5m OI change ≤ `PLAYBOOK_VELOCITY_UNWIND_PCT` (−2.0%) — the writer is covering |
| `CONFIRMATION_LOST` | ≥ `CONFIRMATION_LOST_MIN` (2) of the entry-time confirmation factors (thesis / commission / participant) no longer hold |
| `CONVICTION_FADE` | conviction score has dropped ≥ `CONVICTION_FADE_DROP` (30 pts) below its peak for `CONVICTION_FADE_STREAK` (2) consecutive updates |
| `EXPIRED_SERIES_PURGE` | stale signal from a rolled-off expiry |

Each close also records `profit_captured_pct` (P&L at exit) and `profit_given_back_pct` (drawdown from
the position's own max favorable excursion), alongside `exit_conviction` and an optional `exit_note`.

**Open-position OI conviction** (`_evaluate_open_oi_conviction`) → STRONG / WEAK / INVALIDATED:
- **BUY_CE:** STRONG if PE `PE_ADD_SPOT_UP`; WEAK if `PE_ADD_SPOT_FLAT/QUIET/CE_DOMINANT`;
  **INVALIDATED** if `PE_UNWIND` or `PE_ADD_SPOT_DOWN`.
- **BUY_PE:** STRONG if CE still adding (≥ +2%) and spot not rising (≤ +8 pts); **INVALIDATED** if PE+spot
  confirm support, or CE covers while spot rises; WEAK otherwise.

## 11.9 Futures Layer panel + alignment (`evaluate_fut_opt_alignment`)

Compares the option decision against **EOD FII index-futures net** + **live front-future OI behavior**:
- `ALIGNED` / `CAUTION` / `NEUTRAL` / `CONFLICT`.
- **`CONFLICT` → `FUTURES_MACRO_CONFLICT` blocker** when, e.g., BUY_CE while macro bearish + FII net
  short beyond the extreme threshold + live futures showing short-build/long-unwind (mirror for BUY_PE).
  Only blocks if `ENABLE_FUTURES_ALIGNMENT_BLOCK` is on.

## 11.10 Session-timing gates (apply across the desk)

| Window | Effect |
|--------|--------|
| 09:15–09:30 (ORB) | no fresh entries (`ORB_NO_TRADE`); 09:15–09:45 on expiry |
| Expiry pre-09:45 | NIFTY options entries blocked (BankNifty primary) |
| after 15:15 | no fresh paper signals (`LATE_SESSION`); open positions still managed |

## 11.11 Context panels (read-only, from journal artifacts)

`Morning Context`, `Key Levels`, `Sessions`, `Technicals`, `GIFT`, `Options Analytics` (Greeks/IV),
and `Journal Status` render whatever the morning/EOD jobs wrote into `journal/`. If an artifact is
missing the panel is blank/UNKNOWN — the desk still runs on live OI. These are **context**, not gates,
except where they feed a grader dimension (e.g. morning `combined_bias`, futures EOD context).
