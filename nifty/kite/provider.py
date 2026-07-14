"""Kite (Zerodha) market-data provider wiring.

Everything that touches the KiteConnect SDK for the live desk lives here:
credentials, instrument resolution, the websocket ticker, and the daily
login/token exchange. Moved verbatim from nifty.dashboard.app (Migration
Phase 4) so the engine and the HTTP layer no longer import the broker SDK -
`import nifty.dashboard.state` works on a box without kiteconnect installed.
"""

from __future__ import annotations

import argparse
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv

from nifty.paths import ENV_FILE
from nifty.dashboard.state import (
    InstrumentState,
    NIFTY_SPOT_TOKEN,
    NSE_NIFTY_SYMBOL,
    OIVelocityState,
    as_float,
    nearest,
)

try:
    from kiteconnect import KiteConnect, KiteTicker
except ImportError as exc:  # pragma: no cover - runtime setup check
    raise SystemExit(
        "Missing dependency: kiteconnect. Run `pip install kiteconnect` or "
        "`pip install -r requirements.txt` after adding it."
    ) from exc


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


def build_login_url(api_key: str) -> str:
    """Zerodha interactive-login URL for the daily token mint."""
    return KiteConnect(api_key=api_key).login_url()


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

    kite_sink = state.router.sink("kite")

    def on_ticks(ws: KiteTicker, ticks: List[Dict[str, Any]]) -> None:
        if gen != state.ticker_gen:
            return
        kite_sink(ticks)

    def on_connect(ws: KiteTicker, _response: Any) -> None:
        if gen != state.ticker_gen:
            # Superseded by a newer ticker — shut this stale socket down.
            try:
                ws.close()
            except Exception:
                pass
            return
        state.set_status("CONNECTED")
        state.record_broker_event("kite", "CONNECTED")
        ws.subscribe(tokens)
        ws.set_mode(ws.MODE_FULL, tokens)

    def on_close(_ws: KiteTicker, code: int, reason: str) -> None:
        if gen != state.ticker_gen:
            return  # a retired ticker closing — must not touch the live status
        state.set_status("CLOSED", f"{code}: {reason}")
        state.record_broker_event("kite", "CLOSED", f"{code}: {reason}")

    def on_error(_ws: KiteTicker, code: int, reason: str) -> None:
        if gen != state.ticker_gen:
            return
        state.set_status("ERROR", f"{code}: {reason}")
        state.record_broker_event("kite", "ERROR", f"{code}: {reason}")

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


def complete_login(
    state: OIVelocityState,
    args: argparse.Namespace,
    request_token: str,
) -> Tuple[float, str, List[InstrumentState]]:
    """Exchange the Zerodha request token, persist it, start the stream."""
    api_key, api_secret, _access_token = env_credentials()
    if not api_key or not api_secret:
        raise RuntimeError("KITE_API_KEY and KITE_API_SECRET are required to exchange request_token.")
    kite = KiteConnect(api_key=api_key)
    session = kite.generate_session(request_token, api_secret=api_secret)
    access_token = str(session["access_token"])
    os.environ["KITE_ACCESS_TOKEN"] = access_token
    save_env_value("KITE_ACCESS_TOKEN", access_token)
    return connect_market_stream(state, args, api_key, access_token)
