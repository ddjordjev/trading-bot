from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from loguru import logger

from config.settings import get_settings
from shared.state import SharedState
from web.auth import verify_token, verify_ws_token
from web.schemas import (
    ActionResponse,
    AnalyticsSnapshot,
    BotActionBody,
    BotInstance,
    BotProfileInfo,
    BotStatus,
    DailyReportData,
    IntelSnapshot,
    LivePositionInfo,
    LogEntry,
    MacroEventInfo,
    ModificationSuggestionInfo,
    ModuleStatus,
    NewsItemInfo,
    PatternInsightInfo,
    PositionCloseBody,
    PositionInfo,
    PositionTakeProfitBody,
    PositionTightenStopBody,
    StrategyInfo,
    StrategyScoreInfo,
    TradeQueueItem,
    TradeRecord,
    TrendingCoinInfo,
    WickScalpInfo,
)

if TYPE_CHECKING:
    from bot import TradingBot
    from db.hub_store import HubDB

_bot: TradingBot | None = None
_start_time: float = 0.0
_log_buffer: deque[dict[str, Any]] = deque(maxlen=200)
_background_tasks: list[asyncio.Task[None]] = []
_bot_reports: dict[str, dict[str, Any]] = {}
_BOT_REGISTRY = Path("data/bot_registry.json")
_bot_urls: dict[str, str] = {}  # bot_id -> base URL (e.g. "http://bot-meanrev:9035")
_HUB_DB_PATH = Path("data/hub.db")
_hub_db: HubDB | None = None


def _get_hub_db() -> HubDB:
    """Return the singleton HubDB (creates on first call)."""
    global _hub_db
    if _hub_db is None:
        from db.hub_store import HubDB

        _hub_db = HubDB(path=_HUB_DB_PATH)
        _hub_db.connect()
    return _hub_db


def _load_bot_registry() -> None:
    """Load persisted bot URLs from disk on startup."""
    import contextlib
    import json

    if _BOT_REGISTRY.exists():
        with contextlib.suppress(Exception):
            _bot_urls.update(json.loads(_BOT_REGISTRY.read_text()))


def _save_bot_registry() -> None:
    """Persist bot URLs to disk so they survive restarts."""
    import json

    try:
        _BOT_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
        _BOT_REGISTRY.write_text(json.dumps(_bot_urls, indent=2))
    except Exception:
        pass


_load_bot_registry()


def _log_sink(message: object) -> None:
    record = message.record  # type: ignore[attr-defined]
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
    if bot:
        local_id = bot.settings.bot_id or "default"
        if local_id not in _bot_urls:
            _bot_urls[local_id] = f"http://bot-{local_id}:9035"
            _save_bot_registry()


FRONTEND_DIR = Path(__file__).parent / "frontend" / "dist"
DOCS_DIR = Path(__file__).parent.parent / "docs"

app = FastAPI(title="Trade Borg Dashboard", version="1.0.0")
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
    total_balance = _bot.target._current_balance
    margin_used = 0.0
    for pos in _bot.orders.scaler.active_positions.values():
        margin_used += pos.avg_entry_price * pos.current_size / max(pos.current_leverage, 1)
    return BotStatus(
        bot_id=_bot.settings.bot_id or "default",
        running=_bot._running,
        trading_mode=_bot.settings.trading_mode,
        exchange_name=_bot.settings.exchange.upper(),
        exchange_url=_bot.settings.platform_url,
        balance=total_balance,
        available_margin=max(0.0, total_balance - margin_used),
        daily_pnl=_bot.target.todays_pnl,
        daily_pnl_pct=_bot.target.todays_pnl_pct,
        tier=_bot.target.tier.value,
        tier_progress_pct=_bot.target.progress_pct,
        daily_target_pct=_bot.target.daily_target_pct,
        total_growth_pct=_bot.target.total_growth_pct,
        total_growth_usd=_bot.target._current_balance - _bot.target._initial_capital,
        uptime_seconds=time.time() - _start_time if _start_time else 0,
        manual_stop_active=_bot.target.manual_stop,
        strategies_count=len(_bot._strategies),
        dynamic_strategies_count=len(_bot._dynamic_strategies),
        profit_buffer_pct=_bot.target.profit_buffer_pct,
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
                age_minutes=(time.time() - pos.opened_at.timestamp()) / 60 if pos.opened_at else 0,
                breakeven_locked=ts.breakeven_locked if ts else False,
                scale_mode=sp.mode.value if sp else "",
                scale_phase=sp.phase.value if sp else "",
                dca_count=sp.adds if sp else 0,
                trade_url=_bot.settings.symbol_platform_url(pos.symbol, pos.market_type),
            )
        )
    return result


def _intel_snapshot() -> IntelSnapshot | None:
    if not _bot:
        return None

    # Multibot: read from shared state written by the monitor service
    if not _bot.intel:
        try:
            _hub_state = SharedState(data_dir=Path("data"))
            snap = _hub_state.read_intel()
            if not snap.sources_active:
                return None
            return IntelSnapshot(
                regime=snap.regime,
                fear_greed=snap.fear_greed,
                fear_greed_bias=snap.fear_greed_bias,
                liquidation_24h=snap.liquidation_24h,
                mass_liquidation=snap.mass_liquidation,
                liquidation_bias=snap.liquidation_bias,
                macro_event_imminent=snap.macro_event_imminent,
                macro_exposure_mult=snap.macro_exposure_mult,
                macro_spike_opportunity=snap.macro_spike_opportunity,
                next_macro_event=snap.next_macro_event,
                whale_bias=snap.whale_bias,
                overleveraged_side=snap.overleveraged_side,
                position_size_multiplier=snap.position_size_multiplier,
                should_reduce_exposure=snap.should_reduce_exposure,
                preferred_direction=snap.preferred_direction,
            )
        except Exception:
            return None

    c = _bot.intel.condition
    if c is None:
        return None
    macro_events_raw = []
    try:
        upcoming = _bot.intel.macro.upcoming_high_impact
        macro_events_raw = [
            MacroEventInfo(
                title=ev.title,
                impact=ev.impact.value,
                hours_until=round(ev.hours_until, 1),
                date_iso=ev.date.isoformat(),
            )
            for ev in upcoming[:15]
        ]
    except Exception:
        pass

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
        macro_events=macro_events_raw,
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


# --------------- health (no auth) ---------------


@app.get("/health", response_model=None)
async def health() -> dict[str, Any]:
    return {"status": "ok", "bot_running": _bot._running if _bot else False}


@app.get("/api/grafana-url", response_model=None)
async def grafana_url(_: str = Depends(verify_token)) -> dict[str, Any]:
    port = _bot.settings.grafana_port if _bot else 3001
    return {"port": port, "dashboard_uid": "trading-bot"}


@app.get("/api/system-metrics", response_model=None)
async def system_metrics(_: str = Depends(verify_token)) -> dict[str, Any]:
    from web.metrics import get_metrics_json

    uptime = time.time() - _start_time if _start_time else 0
    return get_metrics_json(_bot, uptime)


@app.get("/metrics", response_model=None)
async def metrics() -> Response:
    from web.metrics import collect_metrics

    uptime = time.time() - _start_time if _start_time else 0
    body = collect_metrics(_bot, uptime)
    return Response(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")


# --------------- REST endpoints ---------------


@app.get("/api/status", response_model=BotStatus)
async def get_status(_: str = Depends(verify_token)) -> BotStatus:
    return _bot_status()


@app.get("/api/bots", response_model=list[BotInstance])
async def get_bots(_: str = Depends(verify_token)) -> list[BotInstance]:
    """List only enabled (non-hub) bot instances from in-memory reports."""
    from config.bot_profiles import PROFILES_BY_ID

    bots: list[BotInstance] = []
    label_map = {"momentum": "Momentum", "meanrev": "Mean Reversion", "swing": "Swing"}
    hub = _get_hub_db()
    enabled_map = hub.get_all_bot_enabled()

    for rpt in _bot_reports.values():
        bid = rpt.get("bot_id", "")
        if bid == "hub":
            continue
        profile = PROFILES_BY_ID.get(bid)
        if not enabled_map.get(bid, profile.is_default if profile else False):
            continue
        strat_names = [s.get("name", "") for s in rpt.get("strategies", [])]
        bots.append(
            BotInstance(
                bot_id=bid,
                label=label_map.get(bid, bid.title()),
                port=0,
                exchange=rpt.get("exchange", ""),
                strategies=strat_names,
            )
        )
    if not bots and _bot:
        bots.append(
            BotInstance(
                bot_id=_bot.settings.bot_id or "default",
                label="Default",
                port=0,
                exchange=_bot.settings.exchange.upper(),
                strategies=_bot.settings.bot_strategy_list,
            )
        )
    return bots


@app.get("/api/positions", response_model=list[PositionInfo])
async def get_positions(_: str = Depends(verify_token)) -> list[PositionInfo]:
    return await _positions()


@app.get("/api/trades", response_model=list[TradeRecord])
async def get_trades(_: str = Depends(verify_token)) -> list[TradeRecord]:
    if not _bot:
        return []
    records = []
    # Aggregate trade logs from all bot reports
    for rpt in _bot_reports.values():
        for t in rpt.get("trade_log", []):
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
    if not records:
        # Fallback: local bot's in-memory log
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
    records.sort(key=lambda r: r.timestamp, reverse=True)
    return records[:200]


@app.get("/api/intel", response_model=IntelSnapshot | None)
async def get_intel(_: str = Depends(verify_token)) -> IntelSnapshot | None:
    return _intel_snapshot()


@app.get("/api/news", response_model=list[NewsItemInfo])
async def get_news(_: str = Depends(verify_token)) -> list[NewsItemInfo]:
    if not _bot:
        return []
    return [
        NewsItemInfo(
            headline=n.headline,
            source=n.source,
            url=n.url,
            published=n.published.isoformat() if n.published else "",
            matched_symbols=n.matched_symbols,
            sentiment=n.sentiment,
            sentiment_score=n.sentiment_score,
        )
        for n in reversed(_bot._recent_news[-50:])
    ]


@app.get("/api/trade-queue", response_model=list[TradeQueueItem])
async def get_trade_queue(_: str = Depends(verify_token)) -> list[TradeQueueItem]:
    """Return all recent trade proposals across all bot queues with lifecycle status."""
    from shared.models import TradeQueue as TQ

    state = SharedState()
    all_proposals: list[Any] = []

    data_dir = state._data_dir
    found_bot_dirs = False
    for child in sorted(data_dir.iterdir()):
        if not child.is_dir():
            continue
        qf = child / "trade_queue.json"
        if not qf.exists():
            continue
        found_bot_dirs = True
        try:
            q = TQ.model_validate_json(qf.read_text())
            for bucket in (q.critical, q.daily, q.swing):
                for p in bucket:
                    all_proposals.append(p)
        except Exception:
            continue

    if not found_bot_dirs:
        queue = state.read_trade_queue()
        for bucket in (queue.critical, queue.daily, queue.swing):
            for p in bucket:
                all_proposals.append(p)

    pending = [p for p in all_proposals if not p.consumed and not p.rejected and not p.is_expired]

    seen: set[tuple[str, ...]] = set()
    unique = []
    for p in pending:
        key = (p.symbol, p.side, p.strategy or "")
        if key not in seen:
            seen.add(key)
            unique.append(p)

    unique.sort(key=lambda p: p.created_at, reverse=True)

    return [
        TradeQueueItem(
            symbol=p.symbol,
            side=p.side,
            strategy=p.strategy or "",
            strength=p.strength,
            age_seconds=p.age_seconds,
            status="pending",
            reason=p.reason or "",
        )
        for p in unique
    ]


@app.get("/api/trending", response_model=list[TrendingCoinInfo])
async def get_trending(_: str = Depends(verify_token)) -> list[TrendingCoinInfo]:
    if not _bot:
        return []

    if _bot.scanner:
        return [
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
            for coin in _bot.scanner.hot_movers
        ]

    # Multi-bot: read from shared intel state (hub-local, not bot's)
    _hub_state = SharedState(data_dir=Path("data"))
    snap = _hub_state.read_intel()
    return [
        TrendingCoinInfo(
            symbol=m.symbol,
            name=m.name,
            price=m.price,
            volume_24h=m.volume_24h,
            market_cap=m.market_cap,
            change_1h=m.change_1h,
            change_24h=m.change_24h,
            is_low_liquidity=m.is_low_liquidity,
            has_dynamic_strategy=False,
        )
        for m in snap.hot_movers
    ]


@app.get("/api/strategies", response_model=list[StrategyInfo])
async def get_strategies(_: str = Depends(verify_token)) -> list[StrategyInfo]:
    grouped: dict[tuple[str, bool], StrategyInfo] = {}
    for rpt in _bot_reports.values():
        for s in rpt.get("strategies", []):
            name = s.get("name", "")
            is_dyn = s.get("is_dynamic", False)
            key = (name, is_dyn)
            if key not in grouped:
                grouped[key] = StrategyInfo(
                    name=name,
                    symbol="",
                    market_type=s.get("market_type", "futures"),
                    leverage=s.get("leverage", 1),
                    mode=s.get("mode", "pyramid"),
                    is_dynamic=is_dyn,
                )
            g = grouped[key]
            g.open_now += s.get("open_now", 0)
            g.applied_count += s.get("applied_count", 0)
            g.success_count += s.get("success_count", 0)
            g.fail_count += s.get("fail_count", 0)
            sym = s.get("symbol", "")
            if sym:
                existing = {x.strip() for x in g.symbol.split(",") if x.strip()}
                if sym not in existing:
                    g.symbol = ", ".join(sorted(existing | {sym})) if existing else sym
    return list(grouped.values())


@app.get("/api/modules", response_model=list[ModuleStatus])
async def get_modules(_: str = Depends(verify_token)) -> list[ModuleStatus]:
    if not _bot:
        return []
    shared = _bot._multibot
    snap = SharedState(data_dir=Path("data")).read_intel() if shared else None
    return [
        ModuleStatus(
            name="intel",
            display_name="Market Intelligence",
            enabled=shared or (_bot.intel is not None and _bot.settings.intel_enabled),
            description="Fear & Greed, liquidations, macro calendar, whale sentiment"
            + (" (via monitor service)" if shared else ""),
            stats={
                "regime": snap.regime
                if snap
                else (_bot.intel.condition.regime.value if _bot.intel and _bot.intel.condition else "off")
            },
        ),
        ModuleStatus(
            name="scanner",
            display_name="Trending Scanner",
            enabled=True,
            description="CryptoBubbles-style trending coin scanner" + (" (via monitor service)" if shared else ""),
            stats={
                "trending_count": len(snap.hot_movers)
                if snap
                else (len(_bot.scanner.hot_movers) if _bot.scanner else 0)
            },
        ),
        ModuleStatus(
            name="news",
            display_name="News Monitor",
            enabled=shared or _bot.settings.news_enabled,
            description="RSS feed monitoring for spike correlation" + (" (via monitor service)" if shared else ""),
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


def _compound_projection(target: Any) -> str:
    """Single consolidated projection block for the dashboard."""
    p = target.projected_balance
    pct = target.daily_target_pct
    bal = target._current_balance
    growth = target.total_growth_pct
    day = target._day_number
    lines = [
        f"  Balance: {bal:,.2f} USDT  |  Growth: {growth:+.1f}%  |  Day {day}",
        f"  Daily target: {pct:.1f}%",
        "",
        "  Compound projections (if target hit daily):",
        f"    1 week:   {p['1_week']:>12,.2f} USDT",
        f"    1 month:  {p['1_month']:>12,.2f} USDT",
        f"    3 months: {p['3_months']:>12,.2f} USDT",
    ]
    return "\n".join(lines)


@app.get("/api/daily-report", response_model=DailyReportData)
async def get_daily_report(_: str = Depends(verify_token)) -> DailyReportData:
    if not _bot:
        return DailyReportData()
    # Aggregate daily report data across all bots
    total_winning = 0
    total_losing = 0
    total_target_hit = 0
    all_pnl_pcts: list[float] = []
    best_day: dict[str, Any] | None = None
    worst_day: dict[str, Any] | None = None
    all_history: list[dict[str, Any]] = []

    for rpt in _bot_reports.values():
        daily = rpt.get("daily_report", {})
        if not daily:
            continue
        total_winning += daily.get("winning_days", 0)
        total_losing += daily.get("losing_days", 0)
        total_target_hit += daily.get("target_hit_days", 0)
        avg = daily.get("avg_daily_pnl_pct", 0)
        if avg:
            all_pnl_pcts.append(avg)
        for h in daily.get("history", []):
            all_history.append(h)
        bd = daily.get("best_day")
        if bd and (not best_day or bd.get("pnl_pct", 0) > best_day.get("pnl_pct", 0)):
            best_day = bd
        wd = daily.get("worst_day")
        if wd and (not worst_day or wd.get("pnl_pct", 0) < worst_day.get("pnl_pct", 0)):
            worst_day = wd
    if not all_pnl_pcts:
        # Fallback: local bot only
        t = _bot.target
        history = [r.model_dump() for r in t.history]
        best = t.best_day
        worst = t.worst_day
        return DailyReportData(
            compound_report=_compound_projection(_bot.target),
            history=history,
            winning_days=t.winning_days,
            losing_days=t.losing_days,
            target_hit_days=t.target_hit_days,
            avg_daily_pnl_pct=t.avg_daily_pnl_pct,
            best_day=best.model_dump() if best else None,
            worst_day=worst.model_dump() if worst else None,
            projected=t.projected_balance,
        )

    return DailyReportData(
        compound_report=_compound_projection(_bot.target),
        history=all_history,
        winning_days=total_winning,
        losing_days=total_losing,
        target_hit_days=total_target_hit,
        avg_daily_pnl_pct=sum(all_pnl_pcts) / len(all_pnl_pcts) if all_pnl_pcts else 0,
        best_day=best_day,
        worst_day=worst_day,
        projected=_bot.target.projected_balance,
    )


# --------------- analytics endpoints ---------------


@app.get("/api/analytics", response_model=AnalyticsSnapshot)
async def get_analytics(_: str = Depends(verify_token)) -> AnalyticsSnapshot:
    if not _bot:
        return AnalyticsSnapshot()

    hub = _get_hub_db()
    if hub.trade_count() > 0:
        from analytics.engine import AnalyticsEngine as AE

        hub_analytics = AE(hub)
        hub_analytics.refresh()
        scores = [
            StrategyScoreInfo(**s.model_dump())
            for s in sorted(hub_analytics.scores.values(), key=lambda x: x.total_pnl, reverse=True)
        ]
        patterns = [PatternInsightInfo(**p.model_dump()) for p in hub_analytics.patterns]
        suggestions = [ModificationSuggestionInfo(**s.model_dump()) for s in hub_analytics.suggestions]
        hourly = hub.get_hourly_performance()
        regime = hub.get_regime_performance()
    else:
        scores = []
        patterns = []
        suggestions = []
        hourly = []
        regime = []

    live = []
    # Pull live positions from all bot reports (multibot)
    for rpt in _bot_reports.values():
        for p in rpt.get("positions", []):
            entry = p.get("entry_price", 0)
            current = p.get("current_price", 0)
            side = p.get("side", "buy")
            is_long = side in ("long", "buy")
            pnl_pct = p.get("pnl_pct", 0.0)
            leverage = p.get("leverage", 1)
            notional = p.get("notional_value", 0)
            pnl_usd = p.get("pnl_usd", 0.0)
            live.append(
                LivePositionInfo(
                    symbol=p.get("symbol", ""),
                    side="long" if is_long else "short",
                    strategy=p.get("strategy", "unknown"),
                    entry_price=entry,
                    current_price=current,
                    pnl_pct=pnl_pct,
                    pnl_usd=pnl_usd,
                    notional=notional,
                    leverage=leverage,
                    age_minutes=p.get("age_minutes", 0),
                    dca_count=p.get("dca_count", 0),
                )
            )
    # Fallback: local bot positions if no reports
    if not live:
        positions = await _bot.exchange.fetch_positions()
        price_map = {p.symbol: p.current_price for p in positions if p.amount > 0}
        for sym, sp in _bot.orders.scaler.active_positions.items():
            current_price = price_map.get(sym, sp.last_add_price or sp.avg_entry_price)
            if sp.avg_entry_price > 0:
                if sp.side == "long":
                    pnl_pct = (current_price - sp.avg_entry_price) / sp.avg_entry_price * 100
                else:
                    pnl_pct = (sp.avg_entry_price - current_price) / sp.avg_entry_price * 100
            else:
                pnl_pct = 0.0
            notional = sp.current_size * current_price * sp.current_leverage
            pnl_usd = notional * pnl_pct / 100 if sp.current_leverage > 0 else 0
            _opened = getattr(sp, "opened_at", 0)
            age = (time.time() - _opened) / 60 if _opened else 0
            live.append(
                LivePositionInfo(
                    symbol=sym,
                    side=sp.side,
                    strategy=sp.strategy or "unknown",
                    entry_price=sp.avg_entry_price,
                    current_price=current_price,
                    pnl_pct=pnl_pct,
                    pnl_usd=pnl_usd,
                    notional=notional,
                    leverage=sp.current_leverage,
                    age_minutes=age,
                    dca_count=sp.adds,
                )
            )

    total_logged = hub.trade_count()

    return AnalyticsSnapshot(
        strategy_scores=scores,
        patterns=patterns,
        suggestions=suggestions,
        total_trades_logged=total_logged,
        hourly_performance=hourly,
        regime_performance=regime,
        live_positions=live,
    )


@app.get("/api/closed-trades")
async def get_closed_trades(limit: int = 100, _: str = Depends(verify_token)) -> list[dict[str, Any]]:
    if not _bot:
        return []
    hub = _get_hub_db()
    rows = hub.get_all_trades(limit=limit)
    return [r.model_dump() for r in rows if r.closed_at]


@app.get("/api/deposits")
async def get_deposits(limit: int = 100, _: str = Depends(verify_token)) -> list[dict[str, Any]]:
    hub = _get_hub_db()
    return hub.get_deposits(limit=limit)


@app.post("/api/analytics/refresh", response_model=ActionResponse)
async def refresh_analytics(_: str = Depends(verify_token)) -> ActionResponse:
    hub = _get_hub_db()
    if hub.trade_count() == 0:
        return ActionResponse(success=False, message="No trades in hub DB")
    from analytics.engine import AnalyticsEngine as AE

    ae = AE(hub)
    ae.refresh()
    return ActionResponse(success=True, message=f"Analytics refreshed ({len(ae.scores)} strategies)")


# --------------- action endpoints ---------------


@app.post("/api/bot/start", response_model=ActionResponse)
async def bot_start(body: BotActionBody | None = None, _: str = Depends(verify_token)) -> ActionResponse:
    bid = (body.bot_id if body else "") or ""
    if not _bot:
        return ActionResponse(success=False, message="Bot instance not initialized")
    if bid and not _is_local_bot(bid):
        return await _forward_to_bot(bid, "/api/bot/start", {})
    if bid == "all" or not bid:
        results = []
        if not _bot._running:
            _background_tasks[:] = [t for t in _background_tasks if not t.done()]
            _background_tasks.append(asyncio.create_task(_bot.start()))
            results.append("local: starting")
        else:
            results.append("local: already running")
        if not bid or bid == "all":
            remote = await _broadcast_to_remote_bots("/api/bot/start", {})
            if remote:
                results.append(remote)
        return ActionResponse(success=True, message="; ".join(results))
    if _bot._running:
        return ActionResponse(success=False, message="Bot is already running")
    _background_tasks[:] = [t for t in _background_tasks if not t.done()]
    _background_tasks.append(asyncio.create_task(_bot.start()))
    return ActionResponse(success=True, message="Bot starting")


@app.post("/api/bot/stop", response_model=ActionResponse)
async def bot_stop(body: BotActionBody | None = None, _: str = Depends(verify_token)) -> ActionResponse:
    bid = (body.bot_id if body else "") or ""
    if not _bot:
        return ActionResponse(success=False, message="Bot instance not initialized")
    if bid and not _is_local_bot(bid) and bid != "all":
        return await _forward_to_bot(bid, "/api/bot/stop", {})
    results = []
    if _bot._running:
        await _bot.stop()
        results.append("local: stopped")
    else:
        results.append("local: already stopped")
    if not bid or bid == "all":
        remote = await _broadcast_to_remote_bots("/api/bot/stop", {})
        if remote:
            results.append(remote)
    return ActionResponse(success=True, message="; ".join(results))


@app.post("/api/position/close", response_model=ActionResponse)
async def close_position(body: PositionCloseBody, _: str = Depends(verify_token)) -> ActionResponse:
    if not _bot:
        return ActionResponse(success=False, message="Bot not initialized")
    if not _is_local_bot(body.bot_id):
        return await _forward_to_bot(body.bot_id, "/api/position/close", {"symbol": body.symbol})
    from core.models import Signal, SignalAction

    symbol = body.symbol
    sig = Signal(
        symbol=symbol,
        action=SignalAction.CLOSE,
        strategy="dashboard",
        reason="Manual close from dashboard",
    )
    try:
        await _bot.orders.execute_signal(sig)
        nudge_ws()
        return ActionResponse(success=True, message=f"Closed {symbol}")
    except Exception as e:
        return ActionResponse(success=False, message=str(e))


@app.post("/api/position/take-profit", response_model=ActionResponse)
async def take_profit(body: PositionTakeProfitBody, _: str = Depends(verify_token)) -> ActionResponse:
    if not _bot:
        return ActionResponse(success=False, message="Bot not initialized")
    if not _is_local_bot(body.bot_id):
        return await _forward_to_bot(body.bot_id, "/api/position/take-profit", {"symbol": body.symbol, "pct": body.pct})
    symbol = body.symbol
    pct = max(1, min(100, body.pct))
    try:
        positions = await _bot.exchange.fetch_positions()
        pos = next((p for p in positions if p.symbol == symbol and p.amount > 0), None)
        if not pos:
            return ActionResponse(success=False, message=f"No open position for {symbol}")
        close_amount = pos.amount * (pct / 100)
        from core.models import MarketType, OrderSide, OrderType

        close_side = OrderSide.SELL if pos.side.value == "buy" else OrderSide.BUY
        mkt = MarketType(pos.market_type) if pos.market_type in ("spot", "futures") else MarketType.SPOT
        _result = await _bot.exchange.place_order(
            symbol=symbol,
            side=close_side,
            order_type=OrderType.MARKET,
            amount=close_amount,
            leverage=pos.leverage,
            market_type=mkt,
        )
        nudge_ws()
        return ActionResponse(success=True, message=f"Took {pct}% profit on {symbol} ({close_amount:.6f})")
    except Exception as e:
        return ActionResponse(success=False, message=str(e))


@app.post("/api/position/tighten-stop", response_model=ActionResponse)
async def tighten_stop(body: PositionTightenStopBody, _: str = Depends(verify_token)) -> ActionResponse:
    if not _bot:
        return ActionResponse(success=False, message="Bot not initialized")
    if not _is_local_bot(body.bot_id):
        return await _forward_to_bot(
            body.bot_id, "/api/position/tighten-stop", {"symbol": body.symbol, "pct": body.pct}
        )
    symbol = body.symbol
    pct = max(0.1, min(50, body.pct))
    ts = _bot.orders.trailing.active_stops.get(symbol)
    if not ts:
        return ActionResponse(success=False, message=f"No trailing stop for {symbol}")
    positions = await _bot.exchange.fetch_positions()
    pos = next((p for p in positions if p.symbol == symbol), None)
    if not pos:
        return ActionResponse(success=False, message=f"No position for {symbol}")
    if not pos.current_price:
        return ActionResponse(success=False, message="No current price available")
    new_stop = pos.current_price * (1 - pct / 100) if pos.side.value == "buy" else pos.current_price * (1 + pct / 100)
    ts.current_stop = new_stop
    nudge_ws()
    return ActionResponse(success=True, message=f"Stop tightened to {new_stop:.6f} ({pct}% from current)")


@app.post("/api/close-all", response_model=ActionResponse)
async def close_all(body: BotActionBody | None = None, _: str = Depends(verify_token)) -> ActionResponse:
    bid = (body.bot_id if body else "") or ""
    if not _bot:
        return ActionResponse(success=False, message="Bot not initialized")
    if bid and not _is_local_bot(bid) and bid != "all":
        return await _forward_to_bot(bid, "/api/close-all", {})
    results = []
    await _bot._close_all_positions("Dashboard: close all")
    results.append("local: closed")
    if not bid or bid == "all":
        remote = await _broadcast_to_remote_bots("/api/close-all", {})
        if remote:
            results.append(remote)
    nudge_ws()
    return ActionResponse(success=True, message="; ".join(results))


@app.post("/api/stop-trading", response_model=ActionResponse)
async def stop_trading(body: BotActionBody | None = None, _: str = Depends(verify_token)) -> ActionResponse:
    bid = (body.bot_id if body else "") or ""
    if not _bot:
        return ActionResponse(success=False, message="Bot not initialized")
    if bid and not _is_local_bot(bid) and bid != "all":
        return await _forward_to_bot(bid, "/api/stop-trading", {})
    _bot.target.STOP_FILE.touch()
    results = ["local: halted"]
    if not bid or bid == "all":
        remote = await _broadcast_to_remote_bots("/api/stop-trading", {})
        if remote:
            results.append(remote)
    nudge_ws()
    return ActionResponse(success=True, message="; ".join(results))


@app.post("/api/resume-trading", response_model=ActionResponse)
async def resume_trading(body: BotActionBody | None = None, _: str = Depends(verify_token)) -> ActionResponse:
    bid = (body.bot_id if body else "") or ""
    if not _bot:
        return ActionResponse(success=False, message="Bot not initialized")
    if bid and not _is_local_bot(bid) and bid != "all":
        return await _forward_to_bot(bid, "/api/resume-trading", {})
    _bot.target.STOP_FILE.unlink(missing_ok=True)
    results = ["local: resumed"]
    if not bid or bid == "all":
        remote = await _broadcast_to_remote_bots("/api/resume-trading", {})
        if remote:
            results.append(remote)
    nudge_ws()
    return ActionResponse(success=True, message="; ".join(results))


@app.post("/api/reset-profit-buffer", response_model=ActionResponse)
async def reset_profit_buffer(_: str = Depends(verify_token)) -> ActionResponse:
    if not _bot:
        return ActionResponse(success=False, message="Bot not initialized")
    old = _bot.target.profit_buffer_pct
    _bot.target._profit_buffer_pct = 0.0
    _bot.risk.max_daily_loss_pct = _bot.risk._base_max_daily_loss_pct
    return ActionResponse(success=True, message=f"Profit buffer reset (was {old:.1f}%). Daily loss limit back to base.")


@app.post("/api/module/{name}/toggle", response_model=ActionResponse)
async def toggle_module(name: str, _: str = Depends(verify_token)) -> ActionResponse:
    if not _bot:
        return ActionResponse(success=False, message="Bot not initialized")
    if name == "intel":
        if _bot._multibot:
            return ActionResponse(success=False, message="Intel is managed by the monitor service")
        if _bot.intel:
            await _bot.intel.stop()
            _bot.intel = None
            return ActionResponse(success=True, message="Intel disabled")
        else:
            from intel import MarketIntel

            _bot.intel = MarketIntel(
                coinglass_key=_bot.settings.coinglass_api_key,
                symbols=_bot.settings.intel_symbol_list,
                tv_exchange=_bot.settings.tv_exchange,
                cmc_api_key=_bot.settings.cmc_api_key,
                coingecko_api_key=_bot.settings.coingecko_api_key,
            )
            await _bot.intel.start()
            return ActionResponse(success=True, message="Intel enabled")
    elif name == "news":
        if _bot._multibot:
            return ActionResponse(success=False, message="News is managed by the monitor service")
        if not _bot.news:
            return ActionResponse(success=False, message="News monitor not available")
        _bot.settings.news_enabled = not _bot.settings.news_enabled
        _bot.news.enabled = _bot.settings.news_enabled
        if _bot.settings.news_enabled:
            if not _bot.news._running:
                await _bot.news.start()
            state = "enabled"
        else:
            await _bot.news.stop()
            state = "disabled"
        return ActionResponse(success=True, message=f"News {state}")
    return ActionResponse(success=False, message=f"Unknown module: {name}")


# --------------- Bot Profiles (dynamic container management) ---------------


@app.get("/api/bot-profiles", response_model=list[BotProfileInfo])
async def get_bot_profiles(_: str = Depends(verify_token)) -> list[BotProfileInfo]:
    """List all bot profiles with their hub-controlled status."""
    from config.bot_profiles import ALL_PROFILES

    hub = _get_hub_db()
    enabled_map = hub.get_all_bot_enabled()

    result: list[BotProfileInfo] = []
    for p in ALL_PROFILES:
        enabled = enabled_map.get(p.id, p.is_default)
        rpt = _bot_reports.get(p.id, {})
        if p.is_hub:
            container_status = "running" if (_bot is not None and _bot._running) else "idle"
        elif rpt:
            container_status = "running" if enabled else "idle"
        else:
            container_status = "idle"

        result.append(
            BotProfileInfo(
                id=p.id,
                display_name=p.display_name,
                description=p.description,
                style=p.style,
                strategies=p.strategies,
                env_overrides=p.env_overrides,
                is_hub=p.is_hub,
                enabled=enabled,
                container_status=container_status,
            )
        )
    return result


@app.post("/api/bot-profile/{profile_id}/toggle", response_model=ActionResponse)
async def toggle_bot_profile(profile_id: str, _: str = Depends(verify_token)) -> ActionResponse:
    """Enable or disable a bot via hub DB config.

    The bot learns its enabled state from the /internal/report response
    every tick. No file polling, no Docker API — pure HTTP.
    """
    from config.bot_profiles import PROFILES_BY_ID

    profile = PROFILES_BY_ID.get(profile_id)
    if not profile:
        return ActionResponse(success=False, message=f"Unknown profile: {profile_id}")

    if profile.is_hub:
        return ActionResponse(success=False, message="Hub bot cannot be toggled — it runs the dashboard")

    hub = _get_hub_db()
    currently_enabled = hub.is_bot_enabled(profile_id)
    new_enabled = not currently_enabled
    hub.set_bot_enabled(profile_id, new_enabled)

    action = "Enabled" if new_enabled else "Disabled"
    nudge_ws()
    return ActionResponse(success=True, message=f"{action} {profile.display_name}")


# --------------- DB Explorer (read-only) ---------------

_ALLOWED_TABLES: set[str] = set()


def _get_db_conn() -> Any:
    """Get the best available DB connection — hub first, local fallback."""
    hub = _get_hub_db()
    return hub.conn


def _get_db_tables() -> list[dict[str, Any]]:
    conn = _get_db_conn()
    if not conn:
        return []
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    result = []
    for r in rows:
        name = r["name"]
        count = conn.execute(f"SELECT COUNT(*) as c FROM [{name}]").fetchone()["c"]
        result.append({"name": name, "row_count": count})
    _ALLOWED_TABLES.update(t["name"] for t in result)
    return result


@app.get("/api/db/tables", response_model=None)
async def db_tables(_: str = Depends(verify_token)) -> list[dict[str, Any]]:
    return _get_db_tables()


@app.get("/api/db/table/{table_name}", response_model=None)
async def db_table_rows(
    table_name: str,
    page: int = 1,
    page_size: int = 100,
    _: str = Depends(verify_token),
) -> dict[str, Any]:
    conn = _get_db_conn()
    if not conn:
        return {"columns": [], "rows": [], "total": 0, "page": page, "page_size": page_size}

    if not _ALLOWED_TABLES:
        _get_db_tables()
    if table_name not in _ALLOWED_TABLES:
        raise HTTPException(status_code=404, detail=f"Table '{table_name}' not found")
    page_size = min(max(page_size, 10), 500)
    offset = (max(page, 1) - 1) * page_size

    total = conn.execute(f"SELECT COUNT(*) as c FROM [{table_name}]").fetchone()["c"]
    cursor = conn.execute(
        f"SELECT * FROM [{table_name}] ORDER BY rowid DESC LIMIT ? OFFSET ?",
        (page_size, offset),
    )
    columns = [desc[0] for desc in cursor.description] if cursor.description else []
    rows = [dict(r) for r in cursor.fetchall()]

    return {
        "columns": columns,
        "rows": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size,
    }


# --------------- Bot report ingestion (API-based) ---------------


def report_bot_snapshot(data: dict[str, Any]) -> None:
    """Store a bot's dashboard snapshot in memory (called via POST or locally)."""
    bot_id = data.get("bot_id", "")
    if bot_id:
        _bot_reports[bot_id] = data


async def _forward_to_bot(bot_id: str, path: str, body: dict[str, Any]) -> ActionResponse:
    """Forward an action to a remote bot's API and return its response."""
    import aiohttp

    base = _bot_urls.get(bot_id)
    if not base:
        return ActionResponse(success=False, message=f"Bot '{bot_id}' not registered")
    url = f"{base}{path}"
    token = get_settings().dashboard_token
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as sess,
            sess.post(url, json=body, headers=headers) as resp,
        ):
            data = await resp.json()
            return ActionResponse(success=data.get("success", False), message=data.get("message", ""))
    except Exception as e:
        return ActionResponse(success=False, message=f"Forward to {bot_id} failed: {e}")


async def _broadcast_to_remote_bots(path: str, body: dict[str, Any]) -> str:
    """Send an action to all registered remote bots. Returns summary."""
    local_id = (_bot.settings.bot_id or "default") if _bot else ""
    results = []
    for bid in _bot_urls:
        if bid == local_id:
            continue
        resp = await _forward_to_bot(bid, path, body)
        results.append(f"{bid}: {'ok' if resp.success else resp.message}")
    return "; ".join(results)


def _is_local_bot(bot_id: str) -> bool:
    """True when bot_id refers to the bot running on this process."""
    if not _bot:
        return False
    local_id = _bot.settings.bot_id or "default"
    return not bot_id or bot_id == local_id


def _build_merged_snapshot() -> dict[str, Any]:
    """Merge all bot reports into a single dashboard payload.

    Only includes enabled trading bots (skips hub and disabled/idle bots).
    """
    from config.bot_profiles import PROFILES_BY_ID

    reports = list(_bot_reports.values())
    hub = _get_hub_db()
    enabled_map = hub.get_all_bot_enabled()

    all_positions: list[dict[str, Any]] = []
    all_wicks: list[dict[str, Any]] = []
    bot_snapshots: list[dict[str, Any]] = []

    total_balance = 0.0
    total_available = 0.0
    total_daily_pnl = 0.0
    total_daily_pnl_pct = 0.0
    total_growth_usd = 0.0
    total_growth_pct = 0.0
    total_profit_buffer = 0.0
    total_uptime = 0.0
    any_running = False
    any_halted = False
    strategies_count = 0
    dynamic_count = 0
    bot_count = 0
    first_status: dict[str, Any] = {}

    for rpt in reports:
        s = rpt.get("status", {})
        bid = rpt.get("bot_id", "")
        ex_name = rpt.get("exchange", "")

        if bid == "hub":
            continue
        profile = PROFILES_BY_ID.get(bid)
        if not enabled_map.get(bid, profile.is_default if profile else False):
            continue

        if not first_status:
            first_status = s

        total_balance += s.get("balance", 0)
        total_available += s.get("available_margin", 0)
        total_daily_pnl += s.get("daily_pnl", 0)
        total_daily_pnl_pct += s.get("daily_pnl_pct", 0)
        total_growth_usd += s.get("total_growth_usd", 0)
        total_growth_pct += s.get("total_growth_pct", 0)
        total_profit_buffer += s.get("profit_buffer_pct", 0)
        total_uptime = max(total_uptime, s.get("uptime_seconds", 0))
        if s.get("running"):
            any_running = True
        if s.get("manual_stop_active"):
            any_halted = True
        strategies_count += s.get("strategies_count", 0)
        dynamic_count += s.get("dynamic_strategies_count", 0)
        bot_count += 1

        for p in rpt.get("positions", []):
            p["bot_id"] = bid
            p["exchange_name"] = ex_name
            all_positions.append(p)
        for w in rpt.get("wick_scalps", []):
            w["bot_id"] = bid
            w["exchange_name"] = ex_name
            all_wicks.append(w)

        bot_snapshots.append(
            {
                "bot_id": bid,
                "exchange": ex_name,
                "connected": True,
                "data": {
                    "status": s,
                    "positions": rpt.get("positions", []),
                    "wick_scalps": rpt.get("wick_scalps", []),
                    "intel": None,
                    "logs": [],
                },
            }
        )

    intel = _intel_snapshot()

    merged_status = {
        "bot_id": "all",
        "running": any_running,
        "trading_mode": first_status.get("trading_mode", "paper_local"),
        "exchange_name": first_status.get("exchange_name", ""),
        "exchange_url": first_status.get("exchange_url", ""),
        "balance": total_balance,
        "available_margin": total_available,
        "daily_pnl": total_daily_pnl,
        "daily_pnl_pct": total_daily_pnl_pct / bot_count if bot_count else 0,
        "tier": first_status.get("tier", "building"),
        "tier_progress_pct": first_status.get("tier_progress_pct", 0),
        "daily_target_pct": first_status.get("daily_target_pct", 10),
        "total_growth_usd": total_growth_usd,
        "total_growth_pct": total_growth_pct / bot_count if bot_count else 0,
        "uptime_seconds": total_uptime,
        "manual_stop_active": any_halted,
        "strategies_count": strategies_count,
        "dynamic_strategies_count": dynamic_count,
        "profit_buffer_pct": total_profit_buffer / bot_count if bot_count else 0,
    }

    return {
        "status": merged_status,
        "positions": all_positions,
        "wick_scalps": all_wicks,
        "intel": intel.model_dump() if intel else None,
        "logs": list(_log_buffer),
        "bots": bot_snapshots,
    }


@app.post("/internal/report")
async def receive_bot_report(request: Request) -> dict[str, Any]:
    """Bots POST snapshots here; hub returns all data bots need.

    Bots never touch the shared data volume — the hub acts as a proxy:
    - Reads intel, analytics, trade_queue, extreme_watchlist on their behalf
    - Writes bot_status and exchange_symbols on their behalf
    - Returns enabled flag, confirmed ack keys, and all shared data
    """
    data = await request.json()
    bot_id = data.get("bot_id", "")
    if bot_id:
        url = f"http://bot-{bot_id}:9035"
        if _bot_urls.get(bot_id) != url:
            _bot_urls[bot_id] = url
            _save_bot_registry()

    # --- Proxy writes on behalf of the bot ---
    state = SharedState(data_dir=Path("data"))
    bot_state = SharedState(data_dir=Path(f"data/{bot_id}")) if bot_id else None

    if bot_id and bot_state:
        from shared.models import BotDeploymentStatus

        bot_status_data = data.get("bot_status")
        if bot_status_data:
            bot_state.write_bot_status(BotDeploymentStatus(**bot_status_data))

        exchange_symbols = data.get("exchange_symbols")
        if exchange_symbols:
            bot_state.write_exchange_symbols(
                bot_id, exchange_symbols.get("exchange", ""), exchange_symbols.get("symbols", [])
            )

        queue_updates = data.get("queue_updates")
        if queue_updates and bot_state:
            consumed = queue_updates.get("consumed", [])
            rejected = queue_updates.get("rejected", {})
            if consumed or rejected:
                bot_state.apply_trade_queue_updates(consumed, rejected)

    report_bot_snapshot(data)
    hub = _get_hub_db()
    confirmed = hub.drain_confirmed_keys(bot_id) if bot_id else []
    if bot_id:
        from config.bot_profiles import PROFILES_BY_ID

        profile = PROFILES_BY_ID.get(bot_id)
        default_enabled = profile.is_default if profile else False
        enabled = hub.is_bot_enabled(bot_id, default=default_enabled)
    else:
        enabled = True

    # --- Proxy reads: return shared data the bot needs ---
    response: dict[str, Any] = {
        "status": "ok",
        "confirmed_keys": confirmed,
        "enabled": enabled,
    }
    if bot_id and bot_state:
        with contextlib.suppress(Exception):
            response["intel"] = state.read_intel().model_dump()
        with contextlib.suppress(Exception):
            response["analytics"] = state.read_analytics().model_dump()
        with contextlib.suppress(Exception):
            response["trade_queue"] = bot_state.read_trade_queue().model_dump()
        with contextlib.suppress(Exception):
            response["extreme_watchlist"] = state.read_extreme_watchlist().model_dump()
        response["intel_age"] = state.intel_age_seconds()
    return response


@app.post("/internal/trade")
async def receive_trade(request: Request) -> dict[str, Any]:
    """Bots push trade open/close events here. Hub writes to its own DB.

    Accepts ``request_key`` for idempotent writes and deferred ack.
    """
    data = await request.json()
    bot_id = data.get("bot_id", "")
    action = data.get("action", "")
    trade = data.get("trade", {})
    request_key = data.get("request_key", "")
    if not bot_id or not trade:
        return {"status": "error", "detail": "missing bot_id or trade"}

    hub = _get_hub_db()
    if action == "close" and trade.get("opened_at"):
        updated = hub.update_trade_close(bot_id, trade["opened_at"], trade, request_key=request_key)
        if not updated:
            hub.insert_trade(bot_id, trade, request_key=request_key)
    else:
        hub.insert_trade(bot_id, trade, request_key=request_key)

    return {"status": "ok", "action": action, "request_key": request_key}


@app.get("/internal/trades/{bot_id}/open")
async def get_bot_open_trades(bot_id: str) -> list[dict[str, Any]]:
    """Return open (unclosed) trades for a bot — used on bot startup to recover state."""
    hub = _get_hub_db()
    trades = hub.get_open_trades_for_bot(bot_id)
    return [t.model_dump() for t in trades]


@app.get("/internal/trades/{bot_id}/stats")
async def get_bot_strategy_stats(bot_id: str) -> dict[str, dict[str, Any]]:
    """Return per-strategy stats for a bot, keyed by 'strategy:symbol'."""
    hub = _get_hub_db()
    return hub.get_all_strategy_stats_for_bot(bot_id)


@app.post("/internal/recovery-close")
async def recovery_close_trade(request: Request) -> dict[str, Any]:
    """Bot reports a trade that died while it was down. No exit stats."""
    data = await request.json()
    bot_id = data.get("bot_id", "")
    opened_at = data.get("opened_at", "")
    if not bot_id or not opened_at:
        return {"status": "error", "detail": "missing bot_id or opened_at"}
    hub = _get_hub_db()
    updated = hub.mark_recovery_close(bot_id, opened_at)
    return {"status": "ok", "updated": updated}


@app.post("/internal/deposit")
async def receive_deposit(request: Request) -> dict[str, Any]:
    """Bots push deposit/withdrawal detection events here."""
    data = await request.json()
    bot_id = data.get("bot_id", "")
    amount = data.get("amount", 0)
    if not bot_id:
        return {"status": "error", "detail": "missing bot_id"}

    hub = _get_hub_db()
    row_id = hub.insert_deposit(
        bot_id=bot_id,
        amount=amount,
        exchange=data.get("exchange", ""),
        detected_at=data.get("detected_at", ""),
        balance_before=data.get("balance_before", 0),
        balance_after=data.get("balance_after", 0),
        notes=data.get("notes", ""),
    )
    return {"status": "ok", "deposit_id": row_id}


# --------------- WebSocket ---------------

_ws_nudge: asyncio.Event | None = None


def _get_nudge() -> asyncio.Event:
    global _ws_nudge
    if _ws_nudge is None:
        _ws_nudge = asyncio.Event()
    return _ws_nudge


def nudge_ws() -> None:
    """Signal all WebSocket loops to push an update immediately."""
    evt = _get_nudge()
    evt.set()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    if not await verify_ws_token(websocket):
        return
    await websocket.accept()
    nudge = _get_nudge()
    try:
        while True:
            snapshot = _build_merged_snapshot()
            await websocket.send_json(snapshot)
            nudge.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(nudge.wait(), timeout=2)
    except WebSocketDisconnect:
        logger.debug("Dashboard WebSocket disconnected")
    except Exception as e:
        logger.debug("Dashboard WebSocket error: {}", e)


# --------------- static files ---------------


@app.get("/api/summary-html", response_model=None)
async def serve_summary() -> HTMLResponse:
    summary_path = DOCS_DIR / "summary.html"
    if summary_path.exists():
        return HTMLResponse(summary_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Summary not found</h1>", status_code=404)


if FRONTEND_DIR.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}", response_model=None)
    async def serve_spa(full_path: str) -> FileResponse:
        file_path = (FRONTEND_DIR / full_path).resolve()
        if file_path.is_relative_to(FRONTEND_DIR) and file_path.is_file():
            return FileResponse(file_path)
        return FileResponse(FRONTEND_DIR / "index.html")
