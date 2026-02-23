"""Lightweight command server for secondary bots.

Runs on aiohttp (already a dependency) instead of the full FastAPI dashboard.
Handles forwarded commands from the hub and exposes /health + /metrics for Docker
and Prometheus scraping.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from aiohttp import web
from loguru import logger

if TYPE_CHECKING:
    from bot import TradingBot

_start_time: float = 0.0

_bot: TradingBot | None = None
_background_tasks: set[object] = set()


def set_bot(bot: TradingBot) -> None:
    global _bot
    _bot = bot


async def _nudge_hub(*, full_snapshot: bool = False) -> None:
    """Fire-and-forget: send an immediate status report to the hub.

    This ensures the dashboard reflects action results (close-all, halt,
    resume) without waiting for the next regular report cycle.
    """
    if not _bot:
        return
    import contextlib

    with contextlib.suppress(Exception):
        if full_snapshot:
            # Full snapshot includes "positions", which clears stale open-position
            # cards immediately after close-all/stop/resume actions.
            await _bot._write_deployment_status()
        else:
            await _bot._quick_hub_check()


def _json(success: bool, message: str) -> web.Response:
    return web.json_response({"success": success, "message": message})


async def health(_request: web.Request) -> web.Response:
    running = _bot is not None
    return web.json_response({"status": "ok", "bot_running": running})


async def metrics(_request: web.Request) -> web.Response:
    from web.metrics import collect_metrics

    uptime = time.time() - _start_time if _start_time else 0
    body = collect_metrics(_bot, uptime)
    return web.Response(body=body, content_type="text/plain; version=0.0.4", charset="utf-8")


async def close_position(request: web.Request) -> web.Response:
    if not _bot:
        return _json(False, "Bot not initialized")
    data = await request.json()
    symbol = data.get("symbol", "")
    if not symbol:
        return _json(False, "Missing symbol")
    from core.models import Signal, SignalAction

    sig = Signal(symbol=symbol, action=SignalAction.CLOSE, strategy="dashboard", reason="Manual close from hub")
    try:
        await _bot.orders.execute_signal(sig)
        return _json(True, f"Closed {symbol}")
    except Exception as e:
        return _json(False, str(e))


async def take_profit(request: web.Request) -> web.Response:
    if not _bot:
        return _json(False, "Bot not initialized")
    data = await request.json()
    symbol = data.get("symbol", "")
    pct = max(1, min(100, data.get("pct", 25)))
    if not symbol:
        return _json(False, "Missing symbol")
    try:
        positions = await _bot.exchange.fetch_positions()
        pos = next((p for p in positions if p.symbol == symbol and p.amount > 0), None)
        if not pos:
            return _json(False, f"No open position for {symbol}")
        close_amount = pos.amount * (pct / 100)
        from core.models import MarketType, OrderSide, OrderType

        close_side = OrderSide.SELL if pos.side.value == "buy" else OrderSide.BUY
        mkt = MarketType(pos.market_type) if pos.market_type in ("spot", "futures") else MarketType.SPOT
        await _bot.exchange.place_order(
            symbol=symbol,
            side=close_side,
            order_type=OrderType.MARKET,
            amount=close_amount,
            leverage=pos.leverage,
            market_type=mkt,
        )
        return _json(True, f"Took {pct}% profit on {symbol} ({close_amount:.6f})")
    except Exception as e:
        return _json(False, str(e))


async def tighten_stop(request: web.Request) -> web.Response:
    if not _bot:
        return _json(False, "Bot not initialized")
    data = await request.json()
    symbol = data.get("symbol", "")
    pct = max(0.1, min(50, data.get("pct", 2)))
    if not symbol:
        return _json(False, "Missing symbol")
    ts = _bot.orders.trailing.active_stops.get(symbol)
    if not ts:
        return _json(False, f"No trailing stop for {symbol}")
    positions = await _bot.exchange.fetch_positions()
    pos = next((p for p in positions if p.symbol == symbol), None)
    if not pos:
        return _json(False, f"No position for {symbol}")
    if not pos.current_price:
        return _json(False, "No current price available")
    new_stop = pos.current_price * (1 - pct / 100) if pos.side.value == "buy" else pos.current_price * (1 + pct / 100)
    ts.current_stop = new_stop
    return _json(True, f"Stop tightened to {new_stop:.6f} ({pct}% from current)")


async def close_all(_request: web.Request) -> web.Response:
    if not _bot:
        return _json(False, "Bot not initialized")
    await _bot._close_all_positions("Hub: close all")
    import asyncio

    task = asyncio.create_task(_nudge_hub(full_snapshot=True))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return _json(True, "All positions closed")


async def stop_trading(_request: web.Request) -> web.Response:
    if not _bot:
        return _json(False, "Bot not initialized")
    _bot.target.STOP_FILE.touch()
    import asyncio

    task = asyncio.create_task(_nudge_hub(full_snapshot=True))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return _json(True, "Trading halted")


async def resume_trading(_request: web.Request) -> web.Response:
    if not _bot:
        return _json(False, "Bot not initialized")
    _bot.target.STOP_FILE.unlink(missing_ok=True)
    import asyncio

    task = asyncio.create_task(_nudge_hub(full_snapshot=True))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return _json(True, "Trading resumed")


async def bot_start(_request: web.Request) -> web.Response:
    if not _bot:
        return _json(False, "Bot not initialized")
    if _bot._running:
        return _json(False, "Already running")
    import asyncio

    task = asyncio.create_task(_bot.start())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return _json(True, "Bot starting")


async def bot_stop(_request: web.Request) -> web.Response:
    if not _bot:
        return _json(False, "Bot not initialized")
    if not _bot._running:
        return _json(False, "Not running")
    await _bot.stop()
    return _json(True, "Bot stopped")


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/metrics", metrics)
    app.router.add_post("/api/position/close", close_position)
    app.router.add_post("/api/position/take-profit", take_profit)
    app.router.add_post("/api/position/tighten-stop", tighten_stop)
    app.router.add_post("/api/close-all", close_all)
    app.router.add_post("/api/stop-trading", stop_trading)
    app.router.add_post("/api/resume-trading", resume_trading)
    app.router.add_post("/api/bot/start", bot_start)
    app.router.add_post("/api/bot/stop", bot_stop)
    return app


async def start_command_server(bot: TradingBot, host: str = "0.0.0.0", port: int = 9035) -> web.AppRunner:
    global _start_time
    _start_time = time.time()
    set_bot(bot)
    app = create_app()
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("Command server listening on {}:{}", host, port)
    return runner
