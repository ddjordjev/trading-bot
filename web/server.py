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
    from db.hub_store import HubDB
    from hub.state import HubState

_hub_state_ref: HubState | None = None
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


def set_bot(bot: Any) -> None:
    """No-op kept for test compatibility. Hub mode has no local bot."""
    pass


def set_hub_state(state: HubState) -> None:
    """Called by hub_main.py to inject the in-memory state shared with monitor/analytics."""
    global _hub_state_ref, _start_time
    _hub_state_ref = state
    if not _start_time:
        _start_time = time.time()


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
    if _hub_state_ref is None:
        return BotStatus()

    merged = _build_merged_snapshot()
    s = merged.get("status", {})
    return BotStatus(
        bot_id="hub",
        running=True,
        trading_mode=get_settings().trading_mode,
        exchange_name=s.get("exchange_name", ""),
        balance=s.get("balance", 0),
        available_margin=s.get("available_margin", 0),
        daily_pnl=s.get("daily_pnl", 0),
        daily_pnl_pct=s.get("daily_pnl_pct", 0),
        tier=s.get("tier", "building"),
        total_growth_pct=s.get("total_growth_pct", 0),
        total_growth_usd=s.get("total_growth_usd", 0),
        uptime_seconds=time.time() - _start_time if _start_time else 0,
        strategies_count=s.get("strategies_count", 0),
        dynamic_strategies_count=0,
        profit_buffer_pct=s.get("profit_buffer_pct", 0),
    )


async def _positions() -> list[PositionInfo]:
    merged = _build_merged_snapshot()
    out: list[PositionInfo] = []
    for p in merged.get("positions", []):
        filtered = {k: v for k, v in p.items() if k in PositionInfo.model_fields}
        with contextlib.suppress(Exception):
            out.append(PositionInfo(**filtered))
    return out


def _intel_snapshot() -> IntelSnapshot | None:
    if _hub_state_ref is None:
        return None
    snap = _hub_state_ref.read_intel()
    if not snap.sources_active:
        return None
    return snap


def _wick_scalps() -> list[WickScalpInfo]:
    merged = _build_merged_snapshot()
    out: list[WickScalpInfo] = []
    for w in merged.get("wick_scalps", []):
        filtered = {k: v for k, v in w.items() if k in WickScalpInfo.model_fields}
        with contextlib.suppress(Exception):
            out.append(WickScalpInfo(**filtered))
    return out


def _recent_logs() -> list[LogEntry]:
    return [LogEntry(**e) for e in _log_buffer]


# --------------- health (no auth) ---------------


@app.get("/health", response_model=None)
async def health() -> dict[str, Any]:
    return {"status": "ok", "bot_running": True, "mode": "hub"}


@app.get("/api/grafana-url", response_model=None)
async def grafana_url(_: str = Depends(verify_token)) -> dict[str, Any]:
    return {"port": get_settings().grafana_port, "dashboard_uid": "trading-bot"}


@app.get("/api/system-metrics", response_model=None)
async def system_metrics(_: str = Depends(verify_token)) -> dict[str, Any]:
    from web.metrics import get_metrics_json

    uptime = time.time() - _start_time if _start_time else 0
    return get_metrics_json(None, uptime)


@app.get("/metrics", response_model=None)
async def metrics() -> Response:
    from web.metrics import collect_metrics

    uptime = time.time() - _start_time if _start_time else 0
    body = collect_metrics(None, uptime)
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
        if not enabled_map.get(bid, profile.is_default if profile else True):
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
    return bots


@app.get("/api/positions", response_model=list[PositionInfo])
async def get_positions(_: str = Depends(verify_token)) -> list[PositionInfo]:
    return await _positions()


@app.get("/api/trades", response_model=list[TradeRecord])
async def get_trades(_: str = Depends(verify_token)) -> list[TradeRecord]:
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
    records.sort(key=lambda r: r.timestamp, reverse=True)
    return records[:200]


@app.get("/api/intel", response_model=IntelSnapshot | None)
async def get_intel(_: str = Depends(verify_token)) -> IntelSnapshot | None:
    return _intel_snapshot()


@app.get("/api/news", response_model=list[NewsItemInfo])
async def get_news(_: str = Depends(verify_token)) -> list[NewsItemInfo]:
    # Hub mode: read news from in-memory intel snapshot
    if _hub_state_ref is None:
        return []
    snap = _hub_state_ref.read_intel()
    return [
        NewsItemInfo(
            headline=n.get("headline", ""),
            source=n.get("source", ""),
            url=n.get("url", ""),
            published=n.get("published", ""),
            matched_symbols=n.get("matched_symbols", []),
            sentiment=n.get("sentiment", "neutral"),
            sentiment_score=n.get("sentiment_score", 0.0),
        )
        for n in reversed(snap.news_items[-50:])
    ]


@app.get("/api/trade-queue", response_model=list[TradeQueueItem])
async def get_trade_queue(_: str = Depends(verify_token)) -> list[TradeQueueItem]:
    """Return all recent trade proposals across all bot queues with lifecycle status."""
    all_proposals: list[Any] = []

    if _hub_state_ref is None:
        return []

    # Shared queue (not yet dispatched) + recently dispatched to bots
    q = _hub_state_ref.read_trade_queue()
    for bucket in (q.critical, q.daily, q.swing):
        all_proposals.extend(bucket)
    all_proposals.extend(_hub_state_ref.read_dispatched_proposals())

    active = [p for p in all_proposals if not p.is_expired]

    seen: set[str] = set()
    unique = []
    for p in active:
        if p.id not in seen:
            seen.add(p.id)
            unique.append(p)

    unique.sort(key=lambda p: p.created_at, reverse=True)

    def _status(p: Any) -> str:
        if p.consumed:
            return "consumed"
        if p.rejected:
            return "rejected"
        return "pending"

    return [
        TradeQueueItem(
            symbol=p.symbol,
            side=p.side,
            strategy=p.strategy or "",
            strength=p.strength,
            age_seconds=p.age_seconds,
            status=_status(p),
            reason=p.reason or "",
        )
        for p in unique
    ]


@app.get("/api/trending", response_model=list[TrendingCoinInfo])
async def get_trending(_: str = Depends(verify_token)) -> list[TrendingCoinInfo]:
    if _hub_state_ref is None:
        return []
    snap = _hub_state_ref.read_intel()
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

    # Source 1: live bot reports (strategies currently running on connected bots)
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

    # Source 2: analytics weights (hub-side strategy performance from trade history)
    if _hub_state_ref is not None:
        analytics = _hub_state_ref.read_analytics()
        for w in analytics.weights:
            key = (w.strategy, False)
            if key not in grouped:
                grouped[key] = StrategyInfo(
                    name=w.strategy,
                    symbol="",
                    market_type="futures",
                    leverage=10,
                    mode="pyramid",
                    applied_count=w.total_trades,
                    success_count=round(w.win_rate * w.total_trades) if w.total_trades else 0,
                    fail_count=w.total_trades - round(w.win_rate * w.total_trades) if w.total_trades else 0,
                )
            elif not grouped[key].applied_count and w.total_trades:
                g = grouped[key]
                g.applied_count = w.total_trades
                g.success_count = round(w.win_rate * w.total_trades)
                g.fail_count = w.total_trades - g.success_count

    return list(grouped.values())


@app.get("/api/modules", response_model=list[ModuleStatus])
async def get_modules(_: str = Depends(verify_token)) -> list[ModuleStatus]:
    if _hub_state_ref is None:
        return []
    snap = _hub_state_ref.read_intel()
    return [
        ModuleStatus(
            name="intel",
            display_name="Market Intelligence",
            enabled=True,
            description="Fear & Greed, liquidations, macro calendar, whale sentiment (in-process)",
            stats={"regime": snap.regime},
        ),
        ModuleStatus(
            name="scanner",
            display_name="Trending Scanner",
            enabled=True,
            description="CryptoBubbles-style trending coin scanner (in-process)",
            stats={"trending_count": len(snap.hot_movers)},
        ),
        ModuleStatus(
            name="news",
            display_name="News Monitor",
            enabled=True,
            description="RSS feed monitoring for spike correlation (in-process)",
            stats={"recent_count": len(snap.news_items)},
        ),
        ModuleStatus(
            name="analytics",
            display_name="Analytics Engine",
            enabled=True,
            description="Strategy scoring, pattern detection, suggestions (in-process)",
            stats={"strategies_scored": len(_hub_state_ref.read_analytics().weights)},
        ),
    ]


@app.get("/api/daily-report", response_model=DailyReportData)
async def get_daily_report(_: str = Depends(verify_token)) -> DailyReportData:
    if _hub_state_ref is None:
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
        return DailyReportData()

    return DailyReportData(
        compound_report="",
        history=all_history,
        winning_days=total_winning,
        losing_days=total_losing,
        target_hit_days=total_target_hit,
        avg_daily_pnl_pct=sum(all_pnl_pcts) / len(all_pnl_pcts) if all_pnl_pcts else 0,
        best_day=best_day,
        worst_day=worst_day,
        projected={},
    )


# --------------- analytics endpoints ---------------


@app.get("/api/analytics", response_model=AnalyticsSnapshot)
async def get_analytics(_: str = Depends(verify_token)) -> AnalyticsSnapshot:
    if _hub_state_ref is None:
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
    hub = _get_hub_db()
    rows = hub.get_all_trades(limit=limit)
    return [r.model_dump() for r in rows if r.closed_at]


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
    if bid and bid != "all":
        return await _forward_to_bot(bid, "/api/bot/start", {})
    result = await _broadcast_to_remote_bots("/api/bot/start", {})
    return ActionResponse(success=True, message=result or "broadcast sent")


@app.post("/api/bot/stop", response_model=ActionResponse)
async def bot_stop(body: BotActionBody | None = None, _: str = Depends(verify_token)) -> ActionResponse:
    bid = (body.bot_id if body else "") or ""
    if bid and bid != "all":
        return await _forward_to_bot(bid, "/api/bot/stop", {})
    result = await _broadcast_to_remote_bots("/api/bot/stop", {})
    return ActionResponse(success=True, message=result or "broadcast sent")


@app.post("/api/position/close", response_model=ActionResponse)
async def close_position(body: PositionCloseBody, _: str = Depends(verify_token)) -> ActionResponse:
    return await _forward_to_bot(body.bot_id, "/api/position/close", {"symbol": body.symbol})


@app.post("/api/position/take-profit", response_model=ActionResponse)
async def take_profit(body: PositionTakeProfitBody, _: str = Depends(verify_token)) -> ActionResponse:
    return await _forward_to_bot(body.bot_id, "/api/position/take-profit", {"symbol": body.symbol, "pct": body.pct})


@app.post("/api/position/tighten-stop", response_model=ActionResponse)
async def tighten_stop(body: PositionTightenStopBody, _: str = Depends(verify_token)) -> ActionResponse:
    return await _forward_to_bot(body.bot_id, "/api/position/tighten-stop", {"symbol": body.symbol, "pct": body.pct})


@app.post("/api/close-all", response_model=ActionResponse)
async def close_all(body: BotActionBody | None = None, _: str = Depends(verify_token)) -> ActionResponse:
    bid = (body.bot_id if body else "") or ""
    if bid and bid != "all":
        return await _forward_to_bot(bid, "/api/close-all", {})
    result = await _broadcast_to_remote_bots("/api/close-all", {})
    nudge_ws()
    return ActionResponse(success=True, message=result or "broadcast sent")


@app.post("/api/stop-trading", response_model=ActionResponse)
async def stop_trading(body: BotActionBody | None = None, _: str = Depends(verify_token)) -> ActionResponse:
    bid = (body.bot_id if body else "") or ""
    if bid and bid != "all":
        return await _forward_to_bot(bid, "/api/stop-trading", {})
    result = await _broadcast_to_remote_bots("/api/stop-trading", {})
    nudge_ws()
    return ActionResponse(success=True, message=result or "broadcast sent")


@app.post("/api/resume-trading", response_model=ActionResponse)
async def resume_trading(body: BotActionBody | None = None, _: str = Depends(verify_token)) -> ActionResponse:
    bid = (body.bot_id if body else "") or ""
    if bid and bid != "all":
        return await _forward_to_bot(bid, "/api/resume-trading", {})
    result = await _broadcast_to_remote_bots("/api/resume-trading", {})
    nudge_ws()
    return ActionResponse(success=True, message=result or "broadcast sent")


@app.post("/api/reset-profit-buffer", response_model=ActionResponse)
async def reset_profit_buffer(_: str = Depends(verify_token)) -> ActionResponse:
    result = await _broadcast_to_remote_bots("/api/reset-profit-buffer", {})
    return ActionResponse(success=True, message=result or "broadcast sent")


@app.post("/api/module/{name}/toggle", response_model=ActionResponse)
async def toggle_module(name: str, _: str = Depends(verify_token)) -> ActionResponse:
    if name not in ("intel", "news", "scanner", "analytics"):
        return ActionResponse(success=False, message=f"Unknown module: {name}")
    return ActionResponse(success=False, message=f"{name} is managed by the hub — toggle not supported")


# --------------- Bot Profiles (dynamic container management) ---------------


@app.get("/api/bot-profiles", response_model=list[BotProfileInfo])
async def get_bot_profiles(_: str = Depends(verify_token)) -> list[BotProfileInfo]:
    """List all bot profiles with their hub-controlled status."""
    from config.bot_profiles import ALL_PROFILES

    hub = _get_hub_db()
    enabled_map = hub.get_all_bot_enabled()

    result: list[BotProfileInfo] = []
    for p in ALL_PROFILES:
        if p.is_hub:
            continue
        enabled = enabled_map.get(p.id, p.is_default)
        rpt = _bot_reports.get(p.id, {})
        container_status = ("running" if enabled else "idle") if rpt else "idle"

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

    When enabling an idle bot, writes an activation file to the shared data
    volume so the bot can detect activation without hub communication.
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

    if new_enabled:
        _write_activation_file(profile_id)

    action = "Enabled" if new_enabled else "Disabled"
    nudge_ws()
    return ActionResponse(success=True, message=f"{action} {profile.display_name}")


def _write_activation_file(bot_id: str) -> None:
    """Write an activation marker file so an idle bot can detect activation locally."""
    activate_dir = Path("data") / bot_id
    activate_dir.mkdir(parents=True, exist_ok=True)
    (activate_dir / "activate").write_text("1")


# --------------- DB Explorer (read-only) ---------------

_ALLOWED_TABLES: set[str] = set()


def _get_db_conn() -> Any:
    """Get hub DB connection for the database explorer."""
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
    """Merge a bot's dashboard snapshot into memory (called via POST or locally).

    Quick hub checks only send bot_id + bot_style (no status/positions).
    We merge into the existing report so lightweight polls don't erase
    the last full snapshot.
    """
    bot_id = data.get("bot_id", "")
    if not bot_id:
        return
    existing = _bot_reports.get(bot_id)
    if existing is None:
        _bot_reports[bot_id] = data
    else:
        for key, value in data.items():
            existing[key] = value


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
    results = []
    for bid in _bot_urls:
        resp = await _forward_to_bot(bid, path, body)
        results.append(f"{bid}: {'ok' if resp.success else resp.message}")
    return "; ".join(results)


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
        if not enabled_map.get(bid, profile.is_default if profile else True):
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
    - Writes bot_status on their behalf
    - Returns enabled flag, confirmed ack keys, and all shared data
    """
    data = await request.json()
    bot_id = data.get("bot_id", "")
    if bot_id:
        url = f"http://bot-{bot_id}:9035"
        if _bot_urls.get(bot_id) != url:
            _bot_urls[bot_id] = url
            _save_bot_registry()

    if bot_id and _hub_state_ref is not None:
        from shared.models import BotDeploymentStatus

        bot_status_data = data.get("bot_status")
        if bot_status_data:
            try:
                _hub_state_ref.write_bot_status(BotDeploymentStatus(**bot_status_data))
            except Exception:
                logger.warning("Ignoring malformed bot_status from {}", bot_id)

        queue_updates = data.get("queue_updates")
        if queue_updates:
            consumed = queue_updates.get("consumed", [])
            rejected = queue_updates.get("rejected", {})
            if consumed or rejected:
                _hub_state_ref.apply_bot_queue_updates(bot_id, consumed, rejected)

    report_bot_snapshot(data)
    hub = _get_hub_db()
    confirmed = hub.drain_confirmed_keys(bot_id) if bot_id else []
    if bot_id:
        from config.bot_profiles import PROFILES_BY_ID

        profile = PROFILES_BY_ID.get(bot_id)
        default_enabled = profile.is_default if profile else True
        enabled = hub.is_bot_enabled(bot_id, default=default_enabled)
    else:
        enabled = True

    response: dict[str, Any] = {
        "status": "ok",
        "confirmed_keys": confirmed,
        "enabled": enabled,
    }
    if bot_id and _hub_state_ref is not None:
        with contextlib.suppress(Exception):
            bot_style = data.get("bot_style", bot_id)
            response["trade_queue"] = _hub_state_ref.read_queue_for_bot_style(bot_style).model_dump()
    return response


@app.get("/internal/intel")
async def get_bot_intel() -> dict[str, Any]:
    """Bots fetch the cached intel snapshot, analytics, and extreme watchlist.

    Returns the full snapshot as-is — bots decide what applies to them.
    Separate from /internal/report (which handles status + queue).
    """
    result: dict[str, Any] = {}
    if _hub_state_ref is not None:
        with contextlib.suppress(Exception):
            result["intel"] = _hub_state_ref.read_intel().model_dump()
        with contextlib.suppress(Exception):
            result["analytics"] = _hub_state_ref.read_analytics().model_dump()
        with contextlib.suppress(Exception):
            result["extreme_watchlist"] = _hub_state_ref.read_extreme_watchlist().model_dump()
        result["intel_age"] = _hub_state_ref.intel_age_seconds()
    else:
        result["intel_age"] = 999999.0
    return result


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
        logger.info("Dashboard WebSocket disconnected")
    except Exception as e:
        logger.warning("Dashboard WebSocket error: {}", e)


# --------------- static files ---------------


@app.get("/api/summary-html", response_model=None)
async def serve_summary() -> HTMLResponse:
    summary_path = DOCS_DIR / "summary.html"
    if summary_path.exists():
        return HTMLResponse(summary_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Summary not found</h1>", status_code=404)


if DOCS_DIR.exists():
    app.mount("/docs-static", StaticFiles(directory=DOCS_DIR), name="docs-static")

if FRONTEND_DIR.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}", response_model=None)
    async def serve_spa(full_path: str) -> FileResponse:
        file_path = (FRONTEND_DIR / full_path).resolve()
        if file_path.is_relative_to(FRONTEND_DIR) and file_path.is_file():
            return FileResponse(file_path)
        return FileResponse(FRONTEND_DIR / "index.html")
