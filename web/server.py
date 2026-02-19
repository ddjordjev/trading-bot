from __future__ import annotations

import asyncio
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import Depends, FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from web.auth import verify_token, verify_ws_token
from web.schemas import (
    ActionResponse,
    AnalyticsSnapshot,
    BotStatus,
    DailyReportData,
    FullSnapshot,
    IntelSnapshot,
    LogEntry,
    ModificationSuggestionInfo,
    ModuleStatus,
    PatternInsightInfo,
    PositionInfo,
    StrategyInfo,
    StrategyScoreInfo,
    TradeRecord,
    TrendingCoinInfo,
    WickScalpInfo,
)

if TYPE_CHECKING:
    from bot import TradingBot

_bot: TradingBot | None = None
_start_time: float = 0.0
_log_buffer: deque[dict] = deque(maxlen=200)
_background_tasks: list = []


def _log_sink(message: object) -> None:
    record = message.record  # type: ignore[union-attr]
    _log_buffer.append(
        {
            "ts": record["time"].strftime("%H:%M:%S"),
            "level": record["level"].name,
            "msg": record["message"],
            "module": record["name"] or "",
        }
    )


def setup_log_capture() -> None:
    logger.add(_log_sink, level="DEBUG", format="{message}")


def set_bot(bot: TradingBot) -> None:
    global _bot, _start_time
    _bot = bot
    _start_time = time.time()


FRONTEND_DIR = Path(__file__).parent / "frontend" / "dist"
DOCS_DIR = Path(__file__).parent.parent / "docs"

app = FastAPI(title="Trading Bot Dashboard", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------- helpers ---------------


def _bot_status() -> BotStatus:
    if not _bot:
        return BotStatus()
    return BotStatus(
        running=_bot._running,
        trading_mode=_bot.settings.trading_mode,
        exchange_name=_bot.settings.exchange.upper(),
        exchange_url=_bot.settings.platform_url,
        balance=_bot.target._current_balance,
        daily_pnl=_bot.target.todays_pnl,
        daily_pnl_pct=_bot.target.todays_pnl_pct,
        tier=_bot.target.tier.value,
        tier_progress_pct=_bot.target.progress_pct,
        daily_target_pct=_bot.target.daily_target_pct,
        total_growth_pct=_bot.target.total_growth_pct,
        uptime_seconds=time.time() - _start_time if _start_time else 0,
        manual_stop_active=_bot.target.manual_stop,
        strategies_count=len(_bot._strategies),
        dynamic_strategies_count=len(_bot._dynamic_strategies),
    )


async def _positions() -> list[PositionInfo]:
    if not _bot:
        return []
    try:
        positions = await _bot.exchange.fetch_positions()
    except Exception:
        return []
    result = []
    for pos in positions:
        if pos.amount <= 0:
            continue
        ts = _bot.orders.trailing.active_stops.get(pos.symbol)
        sp = _bot.orders.scaler.get(pos.symbol)
        result.append(
            PositionInfo(
                symbol=pos.symbol,
                side=pos.side.value,
                amount=pos.amount,
                entry_price=pos.entry_price,
                current_price=pos.current_price,
                pnl_pct=pos.pnl_pct,
                pnl_usd=pos.unrealized_pnl,
                leverage=pos.leverage,
                market_type=pos.market_type,
                strategy=pos.strategy,
                stop_loss=ts.current_stop if ts else pos.stop_loss,
                notional_value=pos.notional_value,
                age_minutes=(time.time() - pos.opened_at.timestamp()) / 60,
                breakeven_locked=ts.breakeven_locked if ts else False,
                scale_mode=sp.mode.value if sp else "",
                scale_phase=sp.phase.value if sp else "",
                dca_count=sp.adds if sp else 0,
                trade_url=_bot.settings.symbol_platform_url(pos.symbol, pos.market_type),
            )
        )
    return result


def _intel_snapshot() -> IntelSnapshot | None:
    if not _bot or not _bot.intel:
        return None
    c = _bot.intel.condition
    return IntelSnapshot(
        regime=c.regime.value,
        fear_greed=c.fear_greed,
        fear_greed_bias=c.fear_greed_bias,
        liquidation_24h=c.liquidation_24h,
        mass_liquidation=c.mass_liquidation,
        liquidation_bias=c.liquidation_bias,
        macro_event_imminent=c.macro_event_imminent,
        macro_exposure_mult=c.macro_exposure_mult,
        macro_spike_opportunity=c.macro_spike_opportunity,
        next_macro_event=c.next_macro_event,
        whale_bias=c.whale_bias,
        overleveraged_side=c.overleveraged_side,
        position_size_multiplier=c.position_size_multiplier,
        should_reduce_exposure=c.should_reduce_exposure,
        preferred_direction=c.preferred_direction,
    )


def _wick_scalps() -> list[WickScalpInfo]:
    if not _bot:
        return []
    result = []
    for sym, ws in _bot.orders.wick_scalper.active_scalps.items():
        result.append(
            WickScalpInfo(
                symbol=sym,
                scalp_side=ws.scalp_side,
                entry_price=ws.entry_price,
                amount=ws.amount,
                age_minutes=ws.age_minutes,
                max_hold_minutes=ws.max_hold_minutes,
            )
        )
    return result


def _recent_logs() -> list[LogEntry]:
    return [LogEntry(**e) for e in _log_buffer]


# --------------- REST endpoints ---------------


@app.get("/api/status", response_model=BotStatus)
async def get_status(_: str = Depends(verify_token)):
    return _bot_status()


@app.get("/api/positions", response_model=list[PositionInfo])
async def get_positions(_: str = Depends(verify_token)):
    return await _positions()


@app.get("/api/trades", response_model=list[TradeRecord])
async def get_trades(_: str = Depends(verify_token)):
    if not _bot:
        return []
    records = []
    for t in _bot.orders._trade_log[-100:]:
        records.append(
            TradeRecord(
                timestamp=t.get("timestamp", ""),
                symbol=t.get("symbol", ""),
                side=t.get("side", ""),
                action=t.get("action", ""),
                amount=t.get("amount", 0),
                price=t.get("price", 0),
                strategy=t.get("strategy", ""),
                pnl=t.get("pnl", 0),
            )
        )
    return records


@app.get("/api/intel", response_model=IntelSnapshot | None)
async def get_intel(_: str = Depends(verify_token)):
    return _intel_snapshot()


@app.get("/api/trending", response_model=list[TrendingCoinInfo])
async def get_trending(_: str = Depends(verify_token)):
    if not _bot:
        return []
    coins = []
    for coin in _bot.scanner.hot_movers:
        coins.append(
            TrendingCoinInfo(
                symbol=coin.symbol,
                name=coin.name,
                price=coin.price,
                volume_24h=coin.volume_24h,
                market_cap=coin.market_cap,
                change_1h=coin.change_1h,
                change_24h=coin.change_24h,
                is_low_liquidity=coin.is_low_liquidity,
                has_dynamic_strategy=coin.trading_pair in _bot._dynamic_strategies,
            )
        )
    return coins


@app.get("/api/strategies", response_model=list[StrategyInfo])
async def get_strategies(_: str = Depends(verify_token)):
    if not _bot:
        return []
    result = []
    from bot import SCALP_ONLY_STRATEGIES

    for s in _bot._strategies:
        result.append(
            StrategyInfo(
                name=s.name,
                symbol=s.symbol,
                market_type=s.market_type,
                leverage=s.leverage,
                mode="winners" if s.name in SCALP_ONLY_STRATEGIES else "pyramid",
                is_dynamic=False,
            )
        )
    for _sym, s in _bot._dynamic_strategies.items():
        result.append(
            StrategyInfo(
                name=s.name,
                symbol=s.symbol,
                market_type=s.market_type,
                leverage=s.leverage,
                mode="pyramid",
                is_dynamic=True,
            )
        )
    return result


@app.get("/api/modules", response_model=list[ModuleStatus])
async def get_modules(_: str = Depends(verify_token)):
    if not _bot:
        return []
    return [
        ModuleStatus(
            name="intel",
            display_name="Market Intelligence",
            enabled=_bot.intel is not None and _bot.settings.intel_enabled,
            description="Fear & Greed, liquidations, macro calendar, whale sentiment",
            stats={"regime": _bot.intel.condition.regime.value if _bot.intel else "off"},
        ),
        ModuleStatus(
            name="scanner",
            display_name="Trending Scanner",
            enabled=True,
            description="CryptoBubbles-style trending coin scanner",
            stats={"trending_count": len(_bot.scanner.hot_movers)},
        ),
        ModuleStatus(
            name="news",
            display_name="News Monitor",
            enabled=_bot.settings.news_enabled,
            description="RSS feed monitoring for spike correlation",
            stats={"recent_count": len(_bot._recent_news)},
        ),
        ModuleStatus(
            name="volatility",
            display_name="Volatility Detector",
            enabled=True,
            description="Price spike detection engine",
            stats={"threshold": _bot.settings.spike_threshold_pct},
        ),
    ]


@app.get("/api/daily-report", response_model=DailyReportData)
async def get_daily_report(_: str = Depends(verify_token)):
    if not _bot:
        return DailyReportData()
    t = _bot.target
    history = [r.model_dump() for r in t.history]
    best = t.best_day
    worst = t.worst_day
    return DailyReportData(
        compound_report=t.compound_report(),
        history=history,
        winning_days=t.winning_days,
        losing_days=t.losing_days,
        target_hit_days=t.target_hit_days,
        avg_daily_pnl_pct=t.avg_daily_pnl_pct,
        best_day=best.model_dump() if best else None,
        worst_day=worst.model_dump() if worst else None,
        projected=t.projected_balance,
    )


# --------------- analytics endpoints ---------------


@app.get("/api/analytics", response_model=AnalyticsSnapshot)
async def get_analytics(_: str = Depends(verify_token)):
    if not _bot:
        return AnalyticsSnapshot()
    scores = [
        StrategyScoreInfo(**s.model_dump())
        for s in sorted(_bot.analytics.scores.values(), key=lambda x: x.total_pnl, reverse=True)
    ]
    patterns = [PatternInsightInfo(**p.model_dump()) for p in _bot.analytics.patterns]
    suggestions = [ModificationSuggestionInfo(**s.model_dump()) for s in _bot.analytics.suggestions]
    hourly = _bot.trade_db.get_hourly_performance()
    regime = _bot.trade_db.get_regime_performance()
    return AnalyticsSnapshot(
        strategy_scores=scores,
        patterns=patterns,
        suggestions=suggestions,
        total_trades_logged=_bot.trade_db.trade_count(),
        hourly_performance=hourly,
        regime_performance=regime,
    )


@app.post("/api/analytics/refresh", response_model=ActionResponse)
async def refresh_analytics(_: str = Depends(verify_token)):
    if not _bot:
        return ActionResponse(success=False, message="Bot not initialized")
    _bot.analytics.refresh()
    return ActionResponse(success=True, message=f"Analytics refreshed: {len(_bot.analytics.scores)} strategies scored")


# --------------- action endpoints ---------------


@app.post("/api/bot/start", response_model=ActionResponse)
async def bot_start(_: str = Depends(verify_token)):
    if not _bot:
        return ActionResponse(success=False, message="Bot instance not initialized")
    if _bot._running:
        return ActionResponse(success=False, message="Bot is already running")
    _background_tasks.append(asyncio.create_task(_bot.start()))
    return ActionResponse(success=True, message="Bot starting")


@app.post("/api/bot/stop", response_model=ActionResponse)
async def bot_stop(_: str = Depends(verify_token)):
    if not _bot:
        return ActionResponse(success=False, message="Bot instance not initialized")
    if not _bot._running:
        return ActionResponse(success=False, message="Bot is not running")
    await _bot.stop()
    return ActionResponse(success=True, message="Bot stopped")


@app.post("/api/position/{symbol}/close", response_model=ActionResponse)
async def close_position(symbol: str, _: str = Depends(verify_token)):
    if not _bot:
        return ActionResponse(success=False, message="Bot not initialized")
    from core.models import Signal, SignalAction

    sig = Signal(
        symbol=symbol,
        action=SignalAction.CLOSE,
        strategy="dashboard",
        reason="Manual close from dashboard",
    )
    try:
        await _bot.orders.execute_signal(sig)
        return ActionResponse(success=True, message=f"Closed {symbol}")
    except Exception as e:
        return ActionResponse(success=False, message=str(e))


@app.post("/api/position/{symbol}/take-profit", response_model=ActionResponse)
async def take_profit(symbol: str, pct: float = Query(default=50, ge=1, le=100), _: str = Depends(verify_token)):
    if not _bot:
        return ActionResponse(success=False, message="Bot not initialized")
    try:
        positions = await _bot.exchange.fetch_positions()
        pos = next((p for p in positions if p.symbol == symbol and p.amount > 0), None)
        if not pos:
            return ActionResponse(success=False, message=f"No open position for {symbol}")
        close_amount = pos.amount * (pct / 100)
        from core.models import Order, OrderSide, OrderType

        order = Order(
            symbol=symbol,
            side=OrderSide.SELL if pos.side.value == "buy" else OrderSide.BUY,
            order_type=OrderType.MARKET,
            amount=close_amount,
            leverage=pos.leverage,
            market_type=pos.market_type,
            strategy="dashboard_partial",
        )
        _result = await _bot.exchange.place_order(order)
        return ActionResponse(success=True, message=f"Took {pct}% profit on {symbol} ({close_amount:.6f})")
    except Exception as e:
        return ActionResponse(success=False, message=str(e))


@app.post("/api/position/{symbol}/tighten-stop", response_model=ActionResponse)
async def tighten_stop(symbol: str, pct: float = Query(default=2, ge=0.1, le=50), _: str = Depends(verify_token)):
    if not _bot:
        return ActionResponse(success=False, message="Bot not initialized")
    ts = _bot.orders.trailing.active_stops.get(symbol)
    if not ts:
        return ActionResponse(success=False, message=f"No trailing stop for {symbol}")
    positions = await _bot.exchange.fetch_positions()
    pos = next((p for p in positions if p.symbol == symbol), None)
    if not pos:
        return ActionResponse(success=False, message=f"No position for {symbol}")
    new_stop = pos.current_price * (1 - pct / 100) if pos.side.value == "buy" else pos.current_price * (1 + pct / 100)
    ts.current_stop = new_stop
    return ActionResponse(success=True, message=f"Stop tightened to {new_stop:.6f} ({pct}% from current)")


@app.post("/api/close-all", response_model=ActionResponse)
async def close_all(_: str = Depends(verify_token)):
    if not _bot:
        return ActionResponse(success=False, message="Bot not initialized")
    await _bot._close_all_positions("Dashboard: close all")
    return ActionResponse(success=True, message="All positions closed")


@app.post("/api/stop-trading", response_model=ActionResponse)
async def stop_trading(_: str = Depends(verify_token)):
    if not _bot:
        return ActionResponse(success=False, message="Bot not initialized")
    _bot.target.STOP_FILE.touch()
    return ActionResponse(success=True, message="Trading halted (STOP file created)")


@app.post("/api/resume-trading", response_model=ActionResponse)
async def resume_trading(_: str = Depends(verify_token)):
    if not _bot:
        return ActionResponse(success=False, message="Bot not initialized")
    _bot.target.STOP_FILE.unlink(missing_ok=True)
    return ActionResponse(success=True, message="Trading resumed (STOP file removed)")


@app.post("/api/module/{name}/toggle", response_model=ActionResponse)
async def toggle_module(name: str, _: str = Depends(verify_token)):
    if not _bot:
        return ActionResponse(success=False, message="Bot not initialized")
    if name == "intel":
        if _bot.intel:
            await _bot.intel.stop()
            _bot.intel = None
            return ActionResponse(success=True, message="Intel disabled")
        else:
            from intel import MarketIntel

            _bot.intel = MarketIntel(
                coinglass_key=_bot.settings.coinglass_api_key,
                symbols=_bot.settings.intel_symbol_list,
            )
            await _bot.intel.start()
            return ActionResponse(success=True, message="Intel enabled")
    elif name == "news":
        _bot.settings.news_enabled = not _bot.settings.news_enabled
        state = "enabled" if _bot.settings.news_enabled else "disabled"
        return ActionResponse(success=True, message=f"News {state}")
    return ActionResponse(success=False, message=f"Unknown module: {name}")


# --------------- WebSocket ---------------


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    if not await verify_ws_token(websocket):
        return
    await websocket.accept()
    try:
        while True:
            snapshot = FullSnapshot(
                status=_bot_status(),
                positions=await _positions(),
                intel=_intel_snapshot(),
                wick_scalps=_wick_scalps(),
                logs=_recent_logs(),
            )
            await websocket.send_json(snapshot.model_dump())
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        logger.debug("Dashboard WebSocket disconnected")
    except Exception as e:
        logger.debug("Dashboard WebSocket error: {}", e)


# --------------- static files ---------------


@app.get("/docs/summary")
async def serve_summary():
    summary_path = DOCS_DIR / "summary.html"
    if summary_path.exists():
        return FileResponse(summary_path, media_type="text/html")
    return HTMLResponse("<h1>Summary not found</h1>", status_code=404)


if FRONTEND_DIR.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        file_path = FRONTEND_DIR / full_path
        if file_path.exists() and file_path.is_file():
            return FileResponse(file_path)
        return FileResponse(FRONTEND_DIR / "index.html")
