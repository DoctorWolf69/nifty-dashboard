# NIFTY Desk — Dashboard + Morning/EOD Pipeline

Self-contained NIFTY F&O desk: a live Kite-streamed OI-velocity dashboard plus the
morning-context and NSE EOD jobs that feed it bias, key levels, OI map and FII/DII
futures positioning. Runs on a Linux server under systemd.

## Layout

```
nifty/
  paths.py            # central .env / journal / data locations (import these, never hardcode)
  jobs.py             # CLI for scheduled jobs: morning | premarket | eod | eod-filing | session-report
  core/               # journal, commission, expiry rules, session timeline
  kite/               # spot + key levels + manual token refresh
  sources/            # GIFT client/monitor, finstack bridge, NSE OI map, NSE EOD downloader
  morning/            # phase capture (pipeline), live morning context, premarket scan
  analytics/          # options Greeks/GEX, futures (FII/DII), confluence grader
  eod/                # EOD filing, intraday session report
  dashboard/          # state.py (engine) + app.py (FastAPI/Kite) + __main__.py + templates/index.html
deploy/               # systemd units+timers, nginx, install.sh
data/  journal/       # runtime archives (auto-created)
```

## Server setup

```bash
# 1. Code + venv
sudo mkdir -p /opt/nifty-dashboard && sudo chown $USER /opt/nifty-dashboard
rsync -a nifty-dashboard/ /opt/nifty-dashboard/        # or git clone
cd /opt/nifty-dashboard
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. Credentials
cp .env.example .env && nano .env                       # set KITE_API_KEY / KITE_API_SECRET

# 3. Timezone (timers are scheduled in IST)
sudo timedatectl set-timezone Asia/Kolkata

# 4. systemd units + timers
sudo ./deploy/install.sh /opt/nifty-dashboard nifty     # APPDIR USER
systemctl list-timers 'nifty-*'

# 5. nginx + TLS (so the Kite redirect URL is https)
sudo cp deploy/nginx/nifty-dashboard.conf /etc/nginx/sites-available/nifty-dashboard
sudo ln -s /etc/nginx/sites-available/nifty-dashboard /etc/nginx/sites-enabled/
sudo certbot --nginx -d your-domain.example
```

In the Kite developer console set the app **Redirect URL** to
`https://your-domain.example/kite/callback`.

## Daily token (10-second routine)

Kite access tokens expire every morning and require an interactive 2FA login — this
cannot be fully automated without violating Zerodha's ToS. The redirect flow makes it
trivial and needs no SSH:

1. The dashboard is started by its timer at **09:10 IST** (or `sudo systemctl start nifty-dashboard`).
2. From any device open `https://your-domain.example/kite/login`, tap the returned `login_url`, approve 2FA.
3. Zerodha redirects to `/kite/callback`, which saves the token to `.env` **and starts the live stream** — no restart.

Fallback (manual, on the box): `/opt/nifty-dashboard/.venv/bin/python -m nifty.kite.token`.

## Daily schedule (Mon–Fri IST, via systemd timers)

| Time | Job | Unit |
|------|-----|------|
| 08:15 | Morning pipeline (global/India bias, instrument, OI map, key levels) | `nifty-morning.timer` |
| 09:01 | Pre-market scan (GIFT gap, desk brief) | `nifty-premarket.timer` |
| 09:10 | Start dashboard | `nifty-dashboard-start.timer` |
| 15:35 | Start GIFT monitor (→ ~03:05) | `nifty-gift-start.timer` |
| 15:40 | Intraday session report | `nifty-session-report.timer` |
| 16:05 | Stop dashboard | `nifty-dashboard-stop.timer` |
| 18:00 | NSE EOD: FII/DII + India VIX | `nifty-eod-fii.timer` |
| 18:30 | NSE EOD: equity bhavcopy/delivery/activity | `nifty-eod-cm.timer` |
| 19:00 | NSE EOD: F&O bhavcopy/participant OI/vol | `nifty-eod-fo.timer` |
| 19:30, 20:00 | NSE EOD retry-missing | `nifty-eod-retry.timer` |
| 19:35 | **EOD filing → FII/DII for tomorrow's dashboard** | `nifty-eod-filing.timer` |

Jobs auto-skip weekends and NSE holidays (override with `--force`).

## How the context lights up (producer → file → consumer)

The dashboard reads dated JSON artifacts the jobs write into `journal/`:

| Dashboard feature | Producer | Artifact |
|-------------------|----------|----------|
| Global/India bias, GIFT gap | morning (08:15) | `global_desk_*`, `morning_desk_*` |
| Instrument + conviction | morning | `instrument_selection_*` |
| OI ceiling/floor/max-pain | morning | `oi_map_*` |
| Pivots/Camarilla/Fib/MA | morning | `key_levels_*` |
| Combined brief | premarket (09:01) | `desk_brief_*` |
| **FII/DII index-futures bias** | EOD filing (prev day 19:35) | `nse_eod_filing_*` |
| EMA 20/50/100/200 (live) | live Kite API | — |

Missing artifact → that field shows blank/UNKNOWN; the dashboard still runs on live OI.

## Data archival
Every tick → `data/live_nifty_oi/nifty_oi_ticks_YYYY-MM-DD.sqlite` (`option_ticks` + `spot_ticks`);
every signal/alert → dated JSONL in `journal/`. Budget ~300 MB/day of ticks.

## Run manually (any host)
```bash
python -m nifty.dashboard --host 127.0.0.1 --port 8080     # dashboard
python -m nifty.jobs morning                                # one-off pipeline
python -m nifty.jobs eod --targets fii_dii,india_vix
python -m nifty.jobs eod-filing
```
