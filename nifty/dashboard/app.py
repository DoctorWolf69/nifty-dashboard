"""FastAPI routes for the NIFTY OI velocity dashboard.

The engine lives in nifty.dashboard.state; all broker (Zerodha Kite) wiring
lives in nifty.kite.provider. This module only serves HTTP. Entry point is
nifty.dashboard.__main__.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from datetime import date
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

from nifty.dashboard.state import OIVelocityState, ist_now
from nifty.kite.provider import build_login_url, complete_login, env_credentials

_INDEX_HTML = (Path(__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")


def create_app(state: OIVelocityState, args: argparse.Namespace) -> FastAPI:
    app = FastAPI(title="NIFTY OI Velocity Dashboard", version="0.1.0")

    # ENGINE_LOOP=1: the engine evaluates on its own cadence and keeps trading
    # (paper) with no browser attached; HTTP serves the cached projection.
    # Unset (default): legacy behavior — every /api/state poll runs the full
    # pipeline. Flip the default after an observed live week.
    engine_loop_enabled = os.getenv("ENGINE_LOOP", "").strip() == "1"

    async def _engine_loop() -> None:
        interval = float(os.getenv("ENGINE_LOOP_INTERVAL", "1.0"))
        loop = asyncio.get_running_loop()
        while True:
            t0 = loop.time()
            try:
                state.projection = await asyncio.to_thread(state.snapshot)
            except Exception as exc:  # keep the loop alive; a dead loop = unmanaged book
                print(f"[{ist_now()}] engine loop error: {exc}")
            await asyncio.sleep(max(0.1, interval - (loop.time() - t0)))

    async def _dhan_standby() -> None:
        """Warm-standby failover (DHAN_FAILOVER=1): mint a TOTP token, map the
        tracked contracts to Dhan security ids, stream in parallel, and let
        the router flip sources when Kite goes silent. Every transition lands
        on the broker timeline."""
        from nifty.dhan import auth as dhan_auth
        from nifty.dhan.provider import DhanFeed, resolve_security_ids
        from nifty.dashboard.state import NIFTY_SPOT_TOKEN

        # Wait for instrument resolution (Kite login may happen after boot).
        while not state.instruments:
            await asyncio.sleep(10)
        try:
            client_id, token = await asyncio.to_thread(dhan_auth.get_credentials)
            sid_map = await asyncio.to_thread(
                resolve_security_ids, list(state.instruments.values())
            )
        except Exception as exc:
            state.record_broker_event("dhan", "SETUP_FAILED", str(exc))
            return
        if not sid_map:
            state.record_broker_event("dhan", "SETUP_FAILED", "no contracts mapped")
            return
        feed = DhanFeed(client_id, token, sid_map, NIFTY_SPOT_TOKEN,
                        on_ticks=state.router.sink("dhan"))
        feed.start()
        state.record_broker_event(
            "dhan", "STANDBY", f"{len(sid_map)} contracts + spot subscribed"
        )
        while True:
            state.router.step()
            await asyncio.sleep(5)

    @app.on_event("startup")
    async def _start_engine_loop() -> None:
        if engine_loop_enabled:
            print(f"[{ist_now()}] engine loop ON (interval {os.getenv('ENGINE_LOOP_INTERVAL', '1.0')}s)")
            asyncio.create_task(_engine_loop())
        if os.getenv("DHAN_FAILOVER", "").strip() == "1":
            from nifty.dhan import auth as dhan_auth
            if dhan_auth.is_configured():
                asyncio.create_task(_dhan_standby())
            else:
                state.record_broker_event(
                    "dhan", "SETUP_FAILED", "DHAN_FAILOVER=1 but credentials missing in .env"
                )

    @app.get("/", response_class=HTMLResponse)
    async def home() -> str:
        return _INDEX_HTML

    @app.get("/api/ping")
    async def api_ping() -> JSONResponse:
        return JSONResponse(state.quick_status())

    @app.get("/api/state")
    async def api_state() -> JSONResponse:
        if engine_loop_enabled and state.projection is not None:
            return JSONResponse(state.projection)
        payload = await asyncio.to_thread(state.snapshot)
        return JSONResponse(payload)

    @app.get("/api/chart")
    async def api_chart(
        contract: str = Query(default=""),
        strike: str = Query(default=""),
    ) -> JSONResponse:
        strike_val = int(strike) if str(strike).isdigit() else None
        payload = state.build_chart_data(
            contract=contract or None,
            strike=strike_val,
        )
        return JSONResponse(payload)

    @app.get("/api/signals")
    async def api_signals() -> JSONResponse:
        if engine_loop_enabled and state.projection is not None:
            snapshot = state.projection
        else:
            snapshot = await asyncio.to_thread(state.snapshot)
        return JSONResponse(
            {
                "signals": snapshot.get("signals", []),
                "journal_file": snapshot.get("signal_journal_file"),
            }
        )

    def _render_report(date_str: str) -> str:
        from datetime import datetime as _dt
        from nifty.eod.session_report import render_report_html

        trade_date = _dt.strptime(date_str, "%Y-%m-%d").date() if date_str else date.today()
        return render_report_html(trade_date)

    @app.get("/report", response_class=HTMLResponse)
    async def report_today() -> str:
        return await asyncio.to_thread(_render_report, "")

    @app.get("/report/{date_str}", response_class=HTMLResponse)
    async def report_for_date(date_str: str) -> str:
        try:
            return await asyncio.to_thread(_render_report, date_str)
        except ValueError:
            return "<h1>Bad date</h1><p>Use /report/YYYY-MM-DD</p>"

    @app.get("/kite/login")
    async def kite_login(format: str = Query(default="")):
        api_key, api_secret, access_token = env_credentials()
        if not api_key:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "KITE_API_KEY is missing. Add it to .env or environment.",
                },
                status_code=400,
            )
        login_url = build_login_url(api_key)
        if format == "json":
            return JSONResponse(
                {
                    "ok": True,
                    "login_url": login_url,
                    "has_api_secret": bool(api_secret),
                    "has_access_token": bool(access_token),
                    "note": "Open login_url. After login, Zerodha redirects to /kite/callback with request_token.",
                }
            )
        # Default: auto-bounce to Zerodha, with a manual button + copy fallback
        # (covers mobile browsers that block the JS redirect).
        safe_url = login_url.replace("'", "%27")
        return HTMLResponse(
            f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta http-equiv="refresh" content="0; url={safe_url}" />
  <title>Kite login — redirecting…</title>
  <style>
    body {{ font-family: Segoe UI, system-ui, sans-serif; background:#0f1117; color:#e5e7eb; margin:0; padding:48px 24px; text-align:center; }}
    a.btn {{ display:inline-block; background:#387ed1; color:#fff; text-decoration:none; padding:14px 22px; border-radius:8px; font-size:1.05rem; font-weight:600; margin-top:8px; }}
    button {{ background:#181b24; color:#e5e7eb; border:1px solid #2a2f3a; border-radius:8px; padding:10px 16px; font-size:0.9rem; cursor:pointer; margin-top:14px; }}
    .muted {{ color:#9ca3af; font-size:0.85rem; margin-top:18px; word-break:break-all; }}
  </style>
</head>
<body>
  <h2>Opening Zerodha login…</h2>
  <p class="muted">If it doesn't open automatically, tap the button:</p>
  <p><a class="btn" href="{safe_url}">Login with Zerodha</a></p>
  <button onclick="navigator.clipboard.writeText('{safe_url}').then(()=>{{this.textContent='Copied!'}})">Copy login URL</button>
  <p class="muted">{login_url}</p>
  <script>
    // Belt-and-suspenders: also push the redirect from JS.
    setTimeout(function(){{ window.location.href = '{safe_url}'; }}, 150);
  </script>
</body>
</html>"""
        )

    @app.get("/kite/callback", response_class=HTMLResponse)
    async def kite_callback(
        request_token: str = Query(default=""),
        status: str = Query(default=""),
    ) -> str:
        api_key, api_secret, _access_token = env_credentials()
        if not request_token:
            state.set_status("ERROR", f"Kite callback missing request_token. Status={status}")
            return "<h2>Kite callback missing request_token</h2><p>Check the Zerodha redirect URL and login status.</p>"
        if not api_key or not api_secret:
            state.set_status("ERROR", "KITE_API_KEY and KITE_API_SECRET are required to exchange request_token.")
            return "<h2>Kite API key/secret missing</h2><p>Add KITE_API_KEY and KITE_API_SECRET to .env, then retry login.</p>"

        try:
            spot, expiry, instruments = complete_login(state, args, request_token)
            return (
                "<!DOCTYPE html><html lang=\"en\"><head>"
                "<meta charset=\"utf-8\" />"
                "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />"
                "<meta http-equiv=\"refresh\" content=\"2; url=/\" />"
                "<title>Kite login successful</title>"
                "<style>body{font-family:Segoe UI,system-ui,sans-serif;background:#0f1117;color:#e5e7eb;"
                "margin:0;padding:48px 24px;text-align:center;line-height:1.5}"
                "a{color:#60a5fa}</style></head><body>"
                "<h2>Kite login successful</h2>"
                "<p>Live stream started — taking you to the dashboard…</p>"
                f"<p>NIFTY spot: {spot:.2f} · expiry: {expiry} · contracts: {len(instruments)}</p>"
                "<p>Token saved to .env for automatic restart today.</p>"
                "<p><a href=\"/\">Open the dashboard now</a></p>"
                "<script>setTimeout(function(){window.location.href='/';},2000);</script>"
                "</body></html>"
            )
        except Exception as exc:
            state.set_status("ERROR", f"Kite login failed: {exc}")
            return f"<h2>Kite login failed</h2><p>{exc}</p>"

    @app.post("/kite/postback")
    async def kite_postback() -> JSONResponse:
        return JSONResponse({"ok": True, "note": "Read-only dashboard: postbacks ignored."})

    return app
