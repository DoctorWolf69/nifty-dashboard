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
