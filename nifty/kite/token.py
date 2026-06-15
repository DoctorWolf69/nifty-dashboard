"""Manual Kite access-token refresh (fallback to the browser redirect flow).

On the server the normal path is the dashboard's /kite/login -> /kite/callback
redirect, which saves the token automatically. This CLI is the manual fallback
for local/desktop use or when the dashboard isn't running:

    python -m nifty.kite.token

It prints the Zerodha login URL, you paste back the request_token from the
redirect, and the new access token is written to .env (KITE_ACCESS_TOKEN).
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from kiteconnect import KiteConnect

from nifty.paths import ENV_FILE


def _save_env_value(key: str, value: str) -> None:
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    out, found = [], False
    for line in lines:
        if line.startswith(f"{key}="):
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def main() -> None:
    load_dotenv(ENV_FILE)
    api_key = os.getenv("KITE_API_KEY", "").strip()
    api_secret = os.getenv("KITE_API_SECRET", "").strip()
    if not api_key or not api_secret:
        raise SystemExit("KITE_API_KEY and KITE_API_SECRET must be set in .env first.")

    kite = KiteConnect(api_key=api_key)
    existing = os.getenv("KITE_ACCESS_TOKEN", "").strip()
    if existing:
        try:
            kite.set_access_token(existing)
            profile = kite.profile()
            print(f"Current token already valid — {profile['user_name']} ({profile['email']}).")
            return
        except Exception:
            print("Existing token invalid — running login flow.")

    print("\nOpen this URL, log in (2FA), then copy the request_token from the redirect URL:")
    print(f"\n  {kite.login_url()}\n")
    request_token = input("Paste request_token here: ").strip()

    data = kite.generate_session(request_token, api_secret=api_secret)
    access_token = data["access_token"]
    _save_env_value("KITE_ACCESS_TOKEN", access_token)
    print(f"\nSaved KITE_ACCESS_TOKEN to {ENV_FILE}")
    print(f"Token: {access_token[:8]}...{access_token[-4:]}")


if __name__ == "__main__":
    main()
