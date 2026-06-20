"""FastAPI service for historical replay + backtest.

Reuses the live dashboard template (with window.REPLAY_MODE injected) and the
live engine, so every existing render function and the report renderer are shared.
Runs as its own always-on service so backtesting works outside market hours.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Dict, Optional

import uvicorn
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

from nifty.replay import loader, backtest
from nifty.replay.session import ReplayTimeline

_TEMPLATE = (
    Path(__file__).resolve().parents[1] / "dashboard" / "templates" / "index.html"
).read_text(encoding="utf-8")
_REPLAY_HTML = _TEMPLATE.replace(
    "<head>", "<head>\n<script>window.REPLAY_MODE=true;</script>", 1
)

# Cached timelines by day. First load builds (replays the whole day once, a few
# minutes); thereafter it reads the gzip cache on disk — instant.
_TIMELINES: Dict[str, ReplayTimeline] = {}
_LOCK = asyncio.Lock()


async def _get_timeline(day: str) -> ReplayTimeline:
    async with _LOCK:
        tl = _TIMELINES.get(day)
        if tl is None:
            tl = await asyncio.to_thread(ReplayTimeline, day)
            _TIMELINES[day] = tl
        return tl


def create_app() -> FastAPI:
    app = FastAPI(title="NIFTY Replay", version="0.1.0")

    @app.get("/replay", response_class=HTMLResponse)
    @app.get("/", response_class=HTMLResponse)
    async def replay_page() -> str:
        return _REPLAY_HTML

    @app.get("/api/replay/days")
    async def days() -> JSONResponse:
        return JSONResponse({"days": loader.available_days()})

    @app.post("/api/replay/load")
    async def load(date: str = Query(...)) -> JSONResponse:
        tl = await _get_timeline(date)
        return JSONResponse({"day": date, **tl.meta(), "frame_times": tl.frame_times()})

    @app.get("/api/replay/state")
    async def state(date: str = Query(...), t: str = Query(...)) -> JSONResponse:
        tl = await _get_timeline(date)
        return JSONResponse(tl.frame_at(t))

    @app.post("/api/replay/backtest")
    async def run_bt(date: str = Query(...)) -> JSONResponse:
        result = await asyncio.to_thread(backtest.run_backtest, date)
        return JSONResponse(result)

    @app.get("/api/replay/report", response_class=HTMLResponse)
    async def report(date: str = Query(...)) -> str:
        path = backtest.REPLAY_OUT / f"report_{date}.html"
        if not path.exists():
            await asyncio.to_thread(backtest.run_backtest, date)
        return path.read_text(encoding="utf-8")

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="NIFTY replay/backtest web service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
