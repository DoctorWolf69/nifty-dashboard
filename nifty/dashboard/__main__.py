"""Entry point: python -m nifty.dashboard [--host ... --port ...]."""

from __future__ import annotations

import argparse
import uvicorn
from datetime import datetime

from nifty.dashboard.state import OIVelocityState, LiveDataStore, DATA_DIR, ist_now
from nifty.dashboard.app import (
    load_kite,
    env_credentials,
    connect_market_stream,
    create_app,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live NIFTY OI velocity dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--center-strike", type=int, default=None)
    parser.add_argument("--strike-step", type=int, default=100)
    parser.add_argument("--strikes-each-side", type=int, default=3)
    parser.add_argument("--expiry", default=None, help="Optional YYYY-MM-DD expiry override")
    parser.add_argument("--no-futures", action="store_true", help="Do not subscribe NIFTY index futures")
    parser.add_argument("--no-persist", action="store_true", help="Disable SQLite tick persistence")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    data_store = None
    if not args.no_persist:
        db_path = DATA_DIR / f"nifty_oi_ticks_{datetime.now().strftime('%Y-%m-%d')}.sqlite"
        data_store = LiveDataStore(db_path)
        data_store.connect()
    state = OIVelocityState(data_store=data_store)
    kite = load_kite()

    if kite is None:
        api_key, api_secret, access_token = env_credentials()
        if not api_key or not api_secret:
            setup_error = "KITE_API_KEY and KITE_API_SECRET are required in .env before login."
        elif not access_token:
            setup_error = "KITE_ACCESS_TOKEN is missing. Open /kite/login and complete Zerodha login."
        else:
            setup_error = "Kite credentials are incomplete or invalid. Check .env."
        state.set_status(
            "SETUP_REQUIRED",
            setup_error,
        )
        print(f"[{ist_now()}] Kite access token missing. Open http://{args.host}:{args.port}/kite/login after setting key/secret.")
    else:
        try:
            api_key, _api_secret, access_token = env_credentials()
            spot, expiry, instruments = connect_market_stream(state, args, api_key, access_token)

            print(f"[{ist_now()}] NIFTY spot={spot:.2f}, expiry={expiry}")
            print(f"[{ist_now()}] Tracking options: {', '.join(item.tradingsymbol for item in instruments)}")
            fut_list = state._future_instrument_list()
            if fut_list:
                print(f"[{ist_now()}] Tracking futures: {', '.join(item.tradingsymbol for item in fut_list)}")
        except Exception as exc:
            state.set_status("ERROR", str(exc))
            print(f"[{ist_now()}] Kite setup failed: {exc}")

    print(f"[{ist_now()}] Dashboard: http://{args.host}:{args.port}")
    uvicorn.run(create_app(state, args), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
