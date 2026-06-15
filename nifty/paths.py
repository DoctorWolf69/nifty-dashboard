"""Single source of truth for all on-disk locations used by the NIFTY desk.

Every module imports paths from here instead of computing them from ``__file__``.
This is what lets the code live in subpackages while still resolving ``.env``,
``journal/`` and ``data/`` relative to the repo root.
"""

from __future__ import annotations

from pathlib import Path

# nifty/paths.py -> parents[1] is the repo root (nifty-dashboard/)
PROJECT_ROOT = Path(__file__).resolve().parents[1]

ENV_FILE = PROJECT_ROOT / ".env"

JOURNAL_DIR = PROJECT_ROOT / "journal"

DATA_DIR = PROJECT_ROOT / "data"
DATA_LIVE_OI = DATA_DIR / "live_nifty_oi"
GIFT_DIR = DATA_DIR / "gift_nifty"
NSE_EOD_DIR = DATA_DIR / "nse_eod"

# Frequently referenced individual artifacts
IV_HISTORY_FILE = DATA_LIVE_OI / "iv_history.jsonl"
SIGNAL_JOURNAL_FILE = JOURNAL_DIR / "nifty_oi_signals.jsonl"


def ensure_dirs() -> None:
    """Create the runtime directories if they do not yet exist."""
    for directory in (JOURNAL_DIR, DATA_DIR, DATA_LIVE_OI, GIFT_DIR, NSE_EOD_DIR):
        directory.mkdir(parents=True, exist_ok=True)


ensure_dirs()
