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
    return make_kite(api_key, access_token)


def nearest(value: float, step: int) -> int:
    return int(round(value / step) * step)


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
    ticker = KiteTicker(api_key, access_token)

    def on_ticks(ws: KiteTicker, ticks: List[Dict[str, Any]]) -> None:
        state.update_ticks(ticks)

    def on_connect(ws: KiteTicker, _response: Any) -> None:
        state.set_status("CONNECTED")
        ws.subscribe(tokens)
        ws.set_mode(ws.MODE_FULL, tokens)

    def on_close(_ws: KiteTicker, code: int, reason: str) -> None:
        state.set_status("CLOSED", f"{code}: {reason}")

    def on_error(_ws: KiteTicker, code: int, reason: str) -> None:
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

    @app.get("/", response_class=HTMLResponse)
    async def home() -> str:
        return _INDEX_HTML

    @app.get("/api/ping")
    async def api_ping() -> JSONResponse:
        return JSONResponse(state.quick_status())

    @app.get("/api/state")
    async def api_state() -> JSONResponse:
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
        snapshot = state.snapshot()
        return JSONResponse(
            {
                "signals": snapshot.get("signals", []),
                "journal_file": snapshot.get("signal_journal_file"),
            }
        )

    @app.get("/kite/login")
    async def kite_login() -> JSONResponse:
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
        return JSONResponse(
            {
                "ok": True,
                "login_url": kite.login_url(),
                "has_api_secret": bool(api_secret),
                "has_access_token": bool(access_token),
                "note": "Open login_url. After login, Zerodha redirects to /kite/callback with request_token.",
            }
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
                "<h2>Kite login successful</h2>"
                "<p>Live stream started. Return to the dashboard tab.</p>"
                f"<p>NIFTY spot: {spot:.2f}, expiry: {expiry}, contracts: {len(instruments)}</p>"
                "<p>The generated access token was saved to .env for automatic restart today.</p>"
            )
        except Exception as exc:
            state.set_status("ERROR", f"Kite login failed: {exc}")
            return f"<h2>Kite login failed</h2><p>{exc}</p>"

    @app.post("/kite/postback")
    async def kite_postback() -> JSONResponse:
        return JSONResponse({"ok": True, "note": "Read-only dashboard: postbacks ignored."})

    return app
