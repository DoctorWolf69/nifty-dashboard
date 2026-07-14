"""Dhan headless authentication - TOTP-minted 24h tokens, cached to disk.

Ported from the proven TRADING/DHAN broker.py. Unlike Kite (daily human
2FA), Dhan can mint its own token: with DHAN_PIN + DHAN_TOTP_SECRET set the
desk renews automatically ~5 minutes before expiry. Static DHAN_ACCESS_TOKEN
is the manual fallback. No interactive prompt here - this runs under
systemd.

.env keys: DHAN_CLIENT_ID, DHAN_PIN, DHAN_TOTP_SECRET  (or DHAN_ACCESS_TOKEN)
"""

from __future__ import annotations

import datetime as dt
import json
import os
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from nifty.paths import DATA_DIR, ENV_FILE

TOKEN_FILE = DATA_DIR / "dhan_token.json"
GENERATE_TOKEN_URL = "https://auth.dhan.co/app/generateAccessToken"
TOKEN_EXPIRY_BUFFER = 300  # treat as expired this many seconds early
IST = ZoneInfo("Asia/Kolkata")


def generate_access_token(client_id: str, pin: str, totp_secret: str) -> dict:
    """Mint a fresh 24h token via TOTP (requires TOTP enabled on the account)."""
    import pyotp
    import requests

    totp_code = pyotp.TOTP(totp_secret.strip()).now()
    response = requests.post(
        GENERATE_TOKEN_URL,
        params={"dhanClientId": client_id, "pin": pin.strip(), "totp": totp_code},
        timeout=20,
    )
    try:
        body = response.json()
    except ValueError:
        body = {}
    access_token = body.get("accessToken") or body.get("access_token")
    if not response.ok or not access_token:
        detail = body.get("errorMessage") or body.get("message") or response.text[:200]
        raise RuntimeError(f"generateAccessToken failed ({response.status_code}): {detail}")
    return {
        "access_token": access_token,
        "client_id": str(body.get("dhanClientId") or client_id),
        "expiry_time": body.get("expiryTime"),
    }


def _token_expired(payload: dict) -> bool:
    expiry = payload.get("expiry_time")
    if expiry:
        try:
            exp = dt.datetime.fromisoformat(str(expiry))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=IST)
            return dt.datetime.now(IST) >= exp - dt.timedelta(seconds=TOKEN_EXPIRY_BUFFER)
        except ValueError:
            pass
    try:
        cached_date = dt.date.fromisoformat(payload["date"])
    except (KeyError, ValueError):
        return True
    return cached_date != dt.datetime.now(IST).date()


def _load_cached() -> Optional[dict]:
    try:
        payload = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return None if _token_expired(payload) else payload


def _save(client_id: str, access_token: str, expiry_time=None) -> None:
    TOKEN_FILE.write_text(
        json.dumps(
            {
                "access_token": access_token,
                "client_id": client_id,
                "date": dt.datetime.now(IST).date().isoformat(),
                "expiry_time": expiry_time,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def is_configured() -> bool:
    load_dotenv(ENV_FILE)
    return bool(
        os.getenv("DHAN_CLIENT_ID", "").strip()
        and (
            (os.getenv("DHAN_PIN", "").strip() and os.getenv("DHAN_TOTP_SECRET", "").strip())
            or os.getenv("DHAN_ACCESS_TOKEN", "").strip()
        )
    )


def get_credentials(force_new: bool = False) -> Tuple[str, str]:
    """Return (client_id, access_token); auto-mints/renews when TOTP is set."""
    load_dotenv(ENV_FILE)
    client_id = os.getenv("DHAN_CLIENT_ID", "").strip()
    env_token = os.getenv("DHAN_ACCESS_TOKEN", "").strip()
    pin = os.getenv("DHAN_PIN", "").strip()
    totp_secret = os.getenv("DHAN_TOTP_SECRET", "").strip()
    if not client_id:
        raise RuntimeError("DHAN_CLIENT_ID missing from .env")

    if pin and totp_secret:
        cached = None if force_new else _load_cached()
        if cached and cached.get("access_token"):
            return cached["client_id"], cached["access_token"]
        result = generate_access_token(client_id, pin, totp_secret)
        _save(result["client_id"], result["access_token"], result.get("expiry_time"))
        print(f"[dhan] token minted via TOTP, valid until {result.get('expiry_time') or '+24h'}")
        return result["client_id"], result["access_token"]

    if env_token:
        return client_id, env_token
    cached = _load_cached()
    if cached:
        return cached["client_id"], cached["access_token"]
    raise RuntimeError("No Dhan token: set DHAN_PIN+DHAN_TOTP_SECRET or DHAN_ACCESS_TOKEN")
