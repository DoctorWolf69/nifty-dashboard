"""FastAPI app + Kite wiring for the NIFTY OI velocity dashboard.

State/engine lives in nifty.dashboard.state; this module wires the live Kite
stream and the HTTP routes. Entry point is nifty.dashboard.__main__.
"""

from __future__ import annotations

from pathlib import Path

# Brings in constants, helpers (ist_now/as_float/as_int), data classes,
# OIVelocityState, and the third-party names (KiteConnect, FastAPI, ...).
from nifty.dashboard.state import *  # noqa: F401,F403

_INDEX_HTML = (Path(__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")


def env_credentials() -> Tuple[str, str, str]:
    load_dotenv(ENV_FILE)
    api_key = os.getenv("KITE_API_KEY", "").strip()
    api_secret = os.getenv("KITE_API_SECRET", "").strip()
    access_token = os.getenv("KITE_ACCESS_TOKEN", "").strip()
    return api_key, api_secret, access_token


def make_kite(api_key: str, access_token: str) -> KiteConnect:
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    return kite


def save_env_value(key: str, value: str, env_path: Path = ENV_FILE) -> None:
    lines: List[str] = []
    found = False
    if env_path.exists():
        with env_path.open("r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    output: List[str] = []
    for line in lines:
        if line.startswith(f"{key}="):
            output.append(f"{key}={value}")
            found = True
        else:
            output.append(line)
    if not found:
        output.append(f"{key}={value}")
    with env_path.open("w", encoding="utf-8") as handle:
        handle.write("\n".join(output).rstrip() + "\n")


def load_kite() -> Optional[KiteConnect]:
    api_key, _api_secret, access_token = env_credentials()
    if not api_key or not access_token:
        return None
    kite = make_kite(api_key, access_token)
    # Validate the saved token with a cheap authenticated call. Kite access
    # tokens roll daily / are invalidated when a new session is generated, so a
    # stale .env token must not silently start a doomed ticker on the 09:10
    # auto-start. On failure return None → the app shows SETUP_REQUIRED and the
    # user re-logs in to mint a fresh token.
    try:
        kite.profile()
    except Exception as exc:
        print(f"[kite] saved access token rejected ({exc}); re-login required via /kite/login.")
        return None
    return kite




def parse_expiry(value: Any) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def resolve_nifty_instruments(
    kite: KiteConnect,
    center_strike: Optional[int],
    strike_step: int,
    strikes_each_side: int,
    expiry: Optional[str],
) -> Tuple[float, str, List[InstrumentState]]:
    quote = kite.quote([NSE_NIFTY_SYMBOL])
    spot = as_float(quote.get(NSE_NIFTY_SYMBOL, {}).get("last_price"))
    center = center_strike or nearest(spot, strike_step)
    wanted_strikes = {center + (offset * strike_step) for offset in range(-strikes_each_side, strikes_each_side + 1)}

    instruments = kite.instruments("NFO")
    today = date.today()
    nifty_options = []
    for item in instruments:
        if item.get("name") != "NIFTY":
            continue
        if item.get("instrument_type") not in {"CE", "PE"}:
            continue
        if int(float(item.get("strike") or 0)) not in wanted_strikes:
            continue
        exp = parse_expiry(item.get("expiry"))
        if exp < today:
            continue
        if expiry and str(exp) != expiry:
            continue
        nifty_options.append((exp, item))

    if not nifty_options:
        raise SystemExit("No NIFTY option instruments found for requested strikes/expiry.")

    selected_expiry = min(exp for exp, _ in nifty_options)
    selected: List[InstrumentState] = []
    for exp, item in nifty_options:
        if exp != selected_expiry:
            continue
        selected.append(
            InstrumentState(
                token=int(item["instrument_token"]),
                tradingsymbol=str(item["tradingsymbol"]),
                strike=int(float(item["strike"])),
                option_type=str(item["instrument_type"]),
                expiry=str(exp),
            )
        )
    selected.sort(key=lambda row: (row.strike, row.option_type))
    return spot, str(selected_expiry), selected


def resolve_nifty_futures(kite: KiteConnect, *, include_next_month: bool = True) -> List[InstrumentState]:
    """Front (+ optional next) NIFTY index future contracts for live OI stream."""
    instruments = kite.instruments("NFO")
    today = date.today()
    candidates: List[Tuple[date, Dict[str, Any]]] = []
    for item in instruments:
        if item.get("name") != "NIFTY":
            continue
        if str(item.get("instrument_type")) != "FUT":
            continue
        exp = parse_expiry(item.get("expiry"))
        if exp < today:
            continue
        candidates.append((exp, item))
    if not candidates:
        return []

    candidates.sort(key=lambda pair: pair[0])
    front_exp = candidates[0][0]
    selected: List[InstrumentState] = []
    for exp, item in candidates:
        if exp == front_exp:
            selected.append(
                InstrumentState(
                    token=int(item["instrument_token"]),
                    tradingsymbol=str(item["tradingsymbol"]),
                    strike=0,
                    option_type="FUT",
                    expiry=str(exp),
                    series_role="front",
                )
            )
        elif include_next_month and exp != front_exp and not any(s.series_role == "next" for s in selected):
            selected.append(
                InstrumentState(
                    token=int(item["instrument_token"]),
                    tradingsymbol=str(item["tradingsymbol"]),
                    strike=0,
                    option_type="FUT",
                    expiry=str(exp),
                    series_role="next",
                )
            )
    return selected


def start_kite_ticker(api_key: str, access_token: str, state: OIVelocityState) -> None:
    tokens = [NIFTY_SPOT_TOKEN, *list(state.instruments.keys()), *list(state.futures.keys())]
    if not tokens:
        state.set_status("SETUP_REQUIRED", "No instruments resolved yet. Complete Kite login first.")
        return

    # Retire any previous ticker before opening a new one. Without this, a stale
    # socket (e.g. after the access token rolls or a re-login) keeps auto-
    # reconnecting, fails the websocket upgrade with 403, and its on_close/on_error
    # stomp the live status to CLOSED/ERROR — which is why a re-login alone never
    # recovered and only a full process restart did. close() = stop_retry() +
    # drop connection on that ticker only; it does NOT stop the shared reactor.
    old = getattr(state, "ticker", None)
    if old is not None:
        try:
            old.close()
        except Exception:
            pass

    state.ticker_gen += 1
    gen = state.ticker_gen
    ticker = KiteTicker(api_key, access_token)
    state.ticker = ticker

    def on_ticks(ws: KiteTicker, ticks: List[Dict[str, Any]]) -> None:
        if gen != state.ticker_gen:
            return
        state.update_ticks(ticks)

    def on_connect(ws: KiteTicker, _response: Any) -> None:
        if gen != state.ticker_gen:
            # Superseded by a newer ticker — shut this stale socket down.
            try:
                ws.close()
            except Exception:
                pass
            return
        state.set_status("CONNECTED")
        ws.subscribe(tokens)
        ws.set_mode(ws.MODE_FULL, tokens)

    def on_close(_ws: KiteTicker, code: int, reason: str) -> None:
        if gen != state.ticker_gen:
            return  # a retired ticker closing — must not touch the live status
        state.set_status("CLOSED", f"{code}: {reason}")

    def on_error(_ws: KiteTicker, code: int, reason: str) -> None:
        if gen != state.ticker_gen:
            return
        state.set_status("ERROR", f"{code}: {reason}")

    ticker.on_ticks = on_ticks
    ticker.on_connect = on_connect
    ticker.on_close = on_close
    ticker.on_error = on_error
    ticker.connect(threaded=True)


def connect_market_stream(
    state: OIVelocityState,
    args: argparse.Namespace,
    api_key: str,
    access_token: str,
) -> Tuple[float, str, List[InstrumentState]]:
    kite = make_kite(api_key, access_token)
    spot, expiry, instruments = resolve_nifty_instruments(
        kite=kite,
        center_strike=args.center_strike,
        strike_step=args.strike_step,
        strikes_each_side=max(1, args.strikes_each_side),
        expiry=args.expiry,
    )
    state.set_instruments(instruments, spot=spot, expiry=expiry)
    if not getattr(args, "no_futures", False):
        futures = resolve_nifty_futures(kite, include_next_month=True)
        state.set_futures(futures)
    state.set_kite(kite)
    start_kite_ticker(api_key, access_token, state)
    return spot, expiry, instruments


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

    @app.on_event("startup")
    async def _start_engine_loop() -> None:
        if engine_loop_enabled:
            print(f"[{ist_now()}] engine loop ON (interval {os.getenv('ENGINE_LOOP_INTERVAL', '1.0')}s)")
            asyncio.create_task(_engine_loop())

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
        kite = KiteConnect(api_key=api_key)
        login_url = kite.login_url()
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
            kite = KiteConnect(api_key=api_key)
            session = kite.generate_session(request_token, api_secret=api_secret)
            access_token = str(session["access_token"])
            os.environ["KITE_ACCESS_TOKEN"] = access_token
            save_env_value("KITE_ACCESS_TOKEN", access_token)
            spot, expiry, instruments = connect_market_stream(state, args, api_key, access_token)
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
