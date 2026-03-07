from __future__ import annotations

import asyncio
import contextlib
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
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
    PositionClaimBody,
    PositionCloseBody,
    PositionInfo,
    PositionTakeProfitBody,
    PositionTightenStopBody,
    StrategyInfo,
    StrategyScoreInfo,
    SuggestionStatusBody,
    TradeQueueItem,
    TradeRecord,
    TrendingCoinInfo,
    WickScalpInfo,
)

if TYPE_CHECKING:
    from db.hub_store import HubDB
    from db.hub_store_postgres import PostgresHubDB
    from hub.state import HubState

_hub_state_ref: HubState | None = None
_monitor_ref: Any | None = None
_openclaw_advisor_ref: Any | None = None
_start_time: float = 0.0
_log_buffer: deque[dict[str, Any]] = deque(maxlen=200)
_bot_reports: dict[str, dict[str, Any]] = {}
_BOT_REGISTRY = Path("data/bot_registry.json")
_bot_urls: dict[str, str] = {}  # bot_id -> base URL (e.g. "http://bot-meanrev:9035")
_HUB_DB_PATH = Path("data/hub.db")
_hub_db: HubDB | PostgresHubDB | None = None
_exchange_snapshot_last_ts: dict[str, float] = {}
_last_stable_snapshot: dict[str, Any] | None = None
_last_stable_snapshot_ts: float = 0.0
_pending_profile_enable_since: dict[str, float] = {}
_PENDING_ENABLE_GRACE_SECS = 45.0
_activation_nudge_since: dict[str, float] = {}
_ACTIVATION_NUDGE_SECS = 15.0


@dataclass(slots=True)
class _EndpointCacheEntry:
    payload: Any
    expires_at: float
    state_token: tuple[Any, ...]


_endpoint_cache: dict[str, _EndpointCacheEntry] = {}
_TRENDING_CACHE_TTL_SECS = 2.0
_STRATEGIES_CACHE_TTL_SECS = 1.5


def _cache_get(name: str, state_token: tuple[Any, ...]) -> Any | None:
    entry = _endpoint_cache.get(name)
    if entry is None:
        return None
    if time.monotonic() >= entry.expires_at:
        return None
    if entry.state_token != state_token:
        return None
    return entry.payload


def _cache_set(name: str, state_token: tuple[Any, ...], payload: Any, ttl_secs: float) -> None:
    _endpoint_cache[name] = _EndpointCacheEntry(
        payload=payload,
        expires_at=time.monotonic() + max(0.1, ttl_secs),
        state_token=state_token,
    )


def _trending_state_token() -> tuple[Any, ...]:
    if _hub_state_ref is None:
        return ("none",)
    snap = _hub_state_ref.read_intel()
    movers = list(getattr(snap, "hot_movers", []) or [])
    first_symbol = str(getattr(movers[0], "symbol", "")) if movers else ""
    first_ts = str(getattr(movers[0], "timestamp", "")) if movers else ""
    return (id(_hub_state_ref), str(getattr(snap, "updated_at", "")), len(movers), first_symbol, first_ts)


def _strategies_state_token() -> tuple[Any, ...]:
    report_sig_rows: list[tuple[Any, ...]] = []
    for bid, rpt in _bot_reports.items():
        positions = list(rpt.get("positions", []) or [])
        strategies = list(rpt.get("strategies", []) or [])
        strategy_sig = tuple(
            sorted(
                (
                    str(s.get("name", "") or ""),
                    bool(s.get("is_dynamic", False)),
                    int(s.get("applied_count", 0) or 0),
                    int(s.get("success_count", 0) or 0),
                    int(s.get("fail_count", 0) or 0),
                    str(s.get("symbol", "") or ""),
                )
                for s in strategies
                if isinstance(s, dict)
            )
        )
        position_strategy_sig = tuple(
            sorted(
                str(p.get("strategy", "") or "").strip()
                for p in positions
                if isinstance(p, dict) and str(p.get("strategy", "") or "").strip()
            )
        )
        report_sig_rows.append(
            (
                str(bid),
                str(rpt.get("updated_at", "") or ""),
                len(positions),
                len(strategies),
                strategy_sig,
                position_strategy_sig,
            )
        )
    report_sig = tuple(sorted(report_sig_rows))
    if _hub_state_ref is None:
        return ("none", report_sig)
    analytics = _hub_state_ref.read_analytics()
    return (
        id(_hub_state_ref),
        report_sig,
        str(getattr(analytics, "updated_at", "")),
        len(getattr(analytics, "weights", []) or []),
    )


def _get_hub_db() -> HubDB | PostgresHubDB:
    """Return the singleton HubDB (creates on first call)."""
    global _hub_db
    if _hub_db is None:
        from db.hub_repository import make_hub_repository

        _hub_db = make_hub_repository(path=_HUB_DB_PATH)
        _hub_db.connect()
    return _hub_db


def _normalize_bot_id(bot_id: Any) -> str:
    """Canonical bot id shape used across hub ingress/state/queries."""
    return str(bot_id or "").strip().lower()


def _pair_symbol(symbol: Any) -> str:
    raw = str(symbol or "").strip().upper()
    if not raw:
        return ""
    return raw.split(":")[0]


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
    except Exception as e:
        logger.warning("Could not save bot registry: {}", e)


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
    logger.add(_log_sink, level=get_settings().log_level, format="{message}")


def set_bot(bot: Any) -> None:
    """No-op kept for test compatibility. Hub mode has no local bot."""
    pass


def set_hub_state(state: HubState) -> None:
    """Called by hub_main.py to inject the in-memory state shared with monitor/analytics."""
    global _hub_state_ref, _start_time
    _hub_state_ref = state
    if not _start_time:
        _start_time = time.time()


def set_monitor_service(monitor: Any) -> None:
    """Called by hub_main.py to expose monitor runtime controls to API handlers."""
    global _monitor_ref
    _monitor_ref = monitor


def set_openclaw_advisor_service(service: Any) -> None:
    """Called by hub_main.py to expose OpenClaw daily advisor controls."""
    global _openclaw_advisor_ref
    _openclaw_advisor_ref = service


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
    from config.bot_profiles import PROFILES_BY_ID, is_default_enabled

    bots: list[BotInstance] = []
    label_map = {"momentum": "Momentum", "meanrev": "Mean Reversion", "swing": "Swing"}
    hub = _get_hub_db()
    enabled_map = hub.get_all_bot_enabled()

    for rpt in _bot_reports.values():
        bid = _normalize_bot_id(rpt.get("bot_id", ""))
        if bid == "hub":
            continue
        default_enabled = is_default_enabled(bid) if bid in PROFILES_BY_ID else True
        # Treat live-reported bots as visible unless hub explicitly disabled them.
        enabled = bool(enabled_map.get(bid, True))
        if bid in PROFILES_BY_ID and bid not in enabled_map:
            enabled = default_enabled or bool(rpt.get("status", {}).get("running", False))
        if not enabled:
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
    """Return all current trade proposals plus recent outcomes."""
    if _hub_state_ref is None:
        return []

    items: list[TradeQueueItem] = []

    q = _hub_state_ref.read_trade_queue()
    for p in q.proposals:
        if p.is_expired or p.is_locked:
            continue
        items.append(
            TradeQueueItem(
                symbol=p.symbol,
                side=p.side,
                strategy=p.strategy or "",
                strength=p.strength,
                age_seconds=p.age_seconds,
                reason=p.reason or "",
                supported_exchanges=p.supported_exchanges,
            )
        )

    items.sort(key=lambda x: x.age_seconds)
    return items


@app.get("/api/trending", response_model=list[TrendingCoinInfo])
async def get_trending(_: str = Depends(verify_token)) -> list[TrendingCoinInfo]:
    if _hub_state_ref is None:
        return []
    state_token = _trending_state_token()
    cached = _cache_get("trending", state_token)
    if cached is not None:
        return list(cached)
    snap = _hub_state_ref.read_intel()
    rows = [
        TrendingCoinInfo(
            symbol=m.symbol,
            name=m.name,
            price=m.price,
            volume_24h=m.volume_24h,
            market_cap=m.market_cap,
            change_5m=float(getattr(m, "change_5m", 0.0) or 0.0),
            change_1h=m.change_1h,
            change_24h=m.change_24h,
            is_low_liquidity=m.is_low_liquidity,
            has_dynamic_strategy=False,
            source=str(getattr(m, "source", "") or ""),
        )
        for m in snap.hot_movers
    ]
    _cache_set("trending", state_token, rows, _TRENDING_CACHE_TTL_SECS)
    return list(rows)


@app.get("/api/strategies", response_model=list[StrategyInfo])
async def get_strategies(_: str = Depends(verify_token)) -> list[StrategyInfo]:
    from config.bot_profiles import PROFILES_BY_ID

    state_token = _strategies_state_token()
    cached = _cache_get("strategies", state_token)
    if cached is not None:
        return list(cached)
    grouped: dict[tuple[str, bool], StrategyInfo] = {}
    open_counts_by_strategy: dict[str, int] = {}
    hub = _get_hub_db()
    enabled_map = hub.get_all_bot_enabled()

    # Source 1: live bot reports (strategies currently running on connected bots)
    for rpt in _bot_reports.values():
        # Open strategy count is derived from live positions, not strategy cache,
        # to avoid stale "open" badges after positions are closed.
        live_counts: dict[str, int] = {}
        for pos in rpt.get("positions", []):
            try:
                amt = float(pos.get("amount", 0) or 0)
            except (TypeError, ValueError):
                amt = 0.0
            if amt <= 0:
                continue
            strategy_name = str(pos.get("strategy", "") or "").strip()
            if not strategy_name:
                continue
            live_counts[strategy_name] = live_counts.get(strategy_name, 0) + 1
        for name, cnt in live_counts.items():
            open_counts_by_strategy[name] = open_counts_by_strategy.get(name, 0) + cnt

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

    # Source 3: enabled/running profile definitions.
    # This keeps Strategies page useful even when bots report empty strategy arrays.
    settings = get_settings()
    default_leverage = int(getattr(settings, "default_leverage", 10) or 10)
    default_mode = str(getattr(settings, "default_scale_mode", "pyramid") or "pyramid")
    for bid, rpt in _bot_reports.items():
        profile = PROFILES_BY_ID.get(str(bid).strip())
        if not profile or profile.is_hub:
            continue
        enabled = bool(enabled_map.get(profile.id, profile.is_default))
        running = bool((rpt.get("status", {}) or {}).get("running", False))
        if not enabled or not running:
            continue
        profile_leverage = int(profile.env_overrides.get("DEFAULT_LEVERAGE", default_leverage) or default_leverage)
        profile_mode = str(profile.env_overrides.get("DEFAULT_SCALE_MODE", default_mode) or default_mode)
        for strategy_name in profile.strategies:
            key = (strategy_name, False)
            if key not in grouped:
                grouped[key] = StrategyInfo(
                    name=strategy_name,
                    symbol="",
                    market_type="futures",
                    leverage=profile_leverage,
                    mode=profile_mode,
                    is_dynamic=False,
                )

    for g in grouped.values():
        g.open_now = open_counts_by_strategy.get(g.name, 0)

    rows = list(grouped.values())
    _cache_set("strategies", state_token, rows, _STRATEGIES_CACHE_TTL_SECS)
    return list(rows)


@app.get("/api/modules", response_model=list[ModuleStatus])
async def get_modules(_: str = Depends(verify_token)) -> list[ModuleStatus]:
    if _hub_state_ref is None:
        return []
    snap = _hub_state_ref.read_intel()
    intel_enabled = (
        bool(_monitor_ref.is_intel_enabled()) if _monitor_ref and hasattr(_monitor_ref, "is_intel_enabled") else True
    )
    scanner_enabled = (
        bool(_monitor_ref.is_scanner_enabled())
        if _monitor_ref and hasattr(_monitor_ref, "is_scanner_enabled")
        else True
    )
    news_enabled = (
        bool(_monitor_ref.is_news_enabled()) if _monitor_ref and hasattr(_monitor_ref, "is_news_enabled") else True
    )
    analytics_enabled = (
        bool(_monitor_ref.is_analytics_enabled())
        if _monitor_ref and hasattr(_monitor_ref, "is_analytics_enabled")
        else True
    )
    openclaw_regime = str(getattr(snap, "openclaw_regime", "unknown") or "unknown")
    openclaw_confidence = float(getattr(snap, "openclaw_regime_confidence", 0.0) or 0.0)
    openclaw_ideas = list(getattr(snap, "openclaw_idea_briefs", []) or [])
    openclaw_triage = list(getattr(snap, "openclaw_failure_triage", []) or [])
    openclaw_experiments = list(getattr(snap, "openclaw_experiments", []) or [])
    openclaw_enabled = (
        bool(_monitor_ref.is_openclaw_enabled())
        if _monitor_ref and hasattr(_monitor_ref, "is_openclaw_enabled")
        else True
    )
    has_openclaw_data = bool(openclaw_ideas or openclaw_triage or openclaw_experiments or openclaw_regime != "unknown")
    return [
        ModuleStatus(
            name="intel",
            display_name="Market Intelligence",
            enabled=intel_enabled,
            description="Fear & Greed, liquidations, macro calendar, whale sentiment (in-process)",
            stats={"regime": snap.regime},
        ),
        ModuleStatus(
            name="scanner",
            display_name="Trending Scanner",
            enabled=scanner_enabled,
            description="CryptoBubbles-style trending coin scanner (in-process)",
            stats={"trending_count": len(snap.hot_movers)},
        ),
        ModuleStatus(
            name="news",
            display_name="News Monitor",
            enabled=news_enabled,
            description="RSS feed monitoring for spike correlation (in-process)",
            stats={"recent_count": len(snap.news_items)},
        ),
        ModuleStatus(
            name="analytics",
            display_name="Analytics Engine",
            enabled=analytics_enabled,
            description="Strategy scoring, pattern detection, suggestions (in-process)",
            stats={"strategies_scored": len(_hub_state_ref.read_analytics().weights)},
        ),
        ModuleStatus(
            name="openclaw",
            display_name="OpenClaw Intelligence",
            enabled=openclaw_enabled,
            description="External advisory intelligence feed (API-only, no execution access)",
            stats={
                "connected": openclaw_enabled and has_openclaw_data,
                "regime": openclaw_regime,
                "confidence": openclaw_confidence,
                "ideas": len(openclaw_ideas),
                "triage": len(openclaw_triage),
            },
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
    reports_seen = 0
    all_pnl_pcts: list[float] = []
    best_day: dict[str, Any] | None = None
    worst_day: dict[str, Any] | None = None
    all_history: list[dict[str, Any]] = []
    projected_totals: dict[str, float] = {"1_week": 0.0, "1_month": 0.0, "3_months": 0.0}
    compound_sections: list[str] = []

    for bot_id, rpt in _bot_reports.items():
        daily = rpt.get("daily_report", {})
        if not daily:
            continue
        reports_seen += 1
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
        projected = daily.get("projected", {})
        if isinstance(projected, dict):
            for key in projected_totals:
                try:
                    projected_totals[key] += float(projected.get(key, 0) or 0)
                except (TypeError, ValueError):
                    continue
        comp = str(daily.get("compound_report", "") or "").strip()
        if comp:
            compound_sections.append(f"[{bot_id}]\n{comp}")

    if reports_seen == 0:
        return DailyReportData()

    compound_report = "\n\n".join(compound_sections)
    projected_out = {k: v for k, v in projected_totals.items() if v > 0}
    return DailyReportData(
        compound_report=compound_report,
        history=all_history,
        winning_days=total_winning,
        losing_days=total_losing,
        target_hit_days=total_target_hit,
        avg_daily_pnl_pct=sum(all_pnl_pcts) / len(all_pnl_pcts) if all_pnl_pcts else 0.0,
        best_day=best_day,
        worst_day=worst_day,
        projected=projected_out,
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
        suggestions = [
            ModificationSuggestionInfo(source="analytics", status="new", **s.model_dump())
            for s in hub_analytics.suggestions
        ]
        hourly = hub.get_hourly_performance()
        regime = hub.get_regime_performance()
    else:
        scores = []
        patterns = []
        suggestions = []
        hourly = []
        regime = []

    oc_rows = hub.list_openclaw_suggestions(include_removed=False, limit=200)
    for row in oc_rows:
        suggestions.append(
            ModificationSuggestionInfo(
                id=int(row.get("id", 0) or 0),
                source=str(row.get("source", "openclaw") or "openclaw"),
                status=str(row.get("status", "new") or "new"),
                strategy=str(row.get("strategy", "") or ""),
                symbol=str(row.get("symbol", "") or ""),
                suggestion_type=str(row.get("suggestion_type", "") or "change_param"),
                title=str(row.get("title", "") or ""),
                description=str(row.get("description", "") or ""),
                confidence=float(row.get("confidence", 0.0) or 0.0),
                current_value=str(row.get("current_value", "") or ""),
                suggested_value=str(row.get("suggested_value", "") or ""),
                expected_improvement=str(row.get("expected_improvement", "") or ""),
                based_on_trades=int(row.get("based_on_trades", 0) or 0),
                notes=str(row.get("notes", "") or ""),
                updated_at=str(row.get("updated_at", "") or ""),
            )
        )

    # Suggestion cleanup:
    # - drop non-actionable analytics pseudo-strategies
    # - merge repeated same-strategy items into one strongest recommendation
    non_actionable_analytics = {"risk_manager", "manual_override", "stop"}
    priority = {
        "disable": 6,
        "regime_filter": 5,
        "time_filter": 4,
        "weight_override": 3,
        "reduce_weight": 3,
        "increase_weight": 3,
        "change_param": 2,
        "process": 1,
    }
    grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for s in suggestions:
        src = str(getattr(s, "source", "") or "").strip().lower()
        strategy = str(getattr(s, "strategy", "") or "").strip().lower()
        if src == "analytics" and strategy in non_actionable_analytics:
            continue
        status = str(getattr(s, "status", "new") or "new").strip().lower()
        status_bucket = "implemented" if status == "implemented" else "active"
        symbol = str(getattr(s, "symbol", "") or "").strip().upper()
        key = (src, status_bucket, strategy, symbol)
        stype = str(getattr(s, "suggestion_type", "") or "").strip().lower()
        rank = (
            priority.get(stype, 0),
            float(getattr(s, "confidence", 0.0) or 0.0),
            int(getattr(s, "based_on_trades", 0) or 0),
        )
        if key not in grouped:
            grouped[key] = {"primary": s, "rank": rank, "types": {stype}, "count": 1}
            continue
        rec = grouped[key]
        rec["types"].add(stype)
        rec["count"] = int(rec["count"]) + 1
        if rank > rec["rank"]:
            rec["primary"] = s
            rec["rank"] = rank

    cleaned: list[ModificationSuggestionInfo] = []
    for rec in grouped.values():
        primary = rec["primary"]
        count = int(rec["count"])
        if count > 1:
            types = ", ".join(t.replace("_", " ") for t in sorted(rec["types"]))
            extra_note = f"merged {count} related suggestions ({types})"
            existing_notes = str(getattr(primary, "notes", "") or "").strip()
            primary.notes = f"{existing_notes} | {extra_note}" if existing_notes else extra_note
        cleaned.append(primary)
    suggestions = sorted(
        cleaned,
        key=lambda s: (
            0 if str(getattr(s, "status", "new") or "new").lower() == "new" else 1,
            -priority.get(str(getattr(s, "suggestion_type", "") or "").lower(), 0),
            -float(getattr(s, "confidence", 0.0) or 0.0),
            -int(getattr(s, "based_on_trades", 0) or 0),
        ),
    )

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


@app.post("/api/openclaw-suggestions/{suggestion_id}/status", response_model=ActionResponse)
async def update_openclaw_suggestion_status(
    suggestion_id: int,
    body: SuggestionStatusBody,
    _: str = Depends(verify_token),
) -> ActionResponse:
    hub = _get_hub_db()
    ok = hub.mark_openclaw_suggestion_status(suggestion_id, body.status, notes=body.notes)
    if not ok:
        return ActionResponse(success=False, message="Suggestion not found or invalid status")
    return ActionResponse(success=True, message=f"Suggestion {suggestion_id} marked as {body.status}")


@app.post("/api/openclaw/daily-review/trigger", response_model=ActionResponse)
async def trigger_openclaw_daily_review(_: str = Depends(verify_token)) -> ActionResponse:
    if _openclaw_advisor_ref is None or not hasattr(_openclaw_advisor_ref, "trigger_now"):
        return ActionResponse(success=False, message="OpenClaw daily advisor unavailable")
    result = await _openclaw_advisor_ref.trigger_now("manual")
    if result.get("ok"):
        return ActionResponse(success=True, message=f"OpenClaw daily review stored (report {result.get('report_id')})")
    return ActionResponse(success=False, message=f"OpenClaw daily review failed: {result.get('error') or 'unknown'}")


@app.get("/api/closed-trades")
async def get_closed_trades(limit: int = 100, _: str = Depends(verify_token)) -> list[dict[str, Any]]:
    hub = _get_hub_db()
    # Closed-trades UI should show only executed/realized closes.
    # Reservation cancels, recovery placeholders, and zero-size stubs pollute
    # win-rate and PnL cards with non-trade rows.
    fetch_limit = max(100, min(limit * 6, 2000))
    rows = hub.get_all_trades(limit=fetch_limit)
    filtered: list[dict[str, Any]] = []
    for r in rows:
        if not r.closed_at or r.action != "close":
            continue
        if r.close_source in {"reservation_cancel", "recovery"}:
            continue
        if float(r.entry_price or 0) <= 0 or float(r.amount or 0) <= 0:
            continue
        if float(r.exit_price or 0) <= 0 and abs(float(r.pnl_usd or 0)) < 1e-12:
            continue
        filtered.append(r.model_dump())
        if len(filtered) >= limit:
            break
    return filtered


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


@app.post("/api/orphan/assign", response_model=ActionResponse)
async def assign_orphan(body: PositionClaimBody, _: str = Depends(verify_token)) -> ActionResponse:
    return await _forward_to_bot(
        body.bot_id,
        "/api/position/claim",
        {"symbol": body.symbol, "strategy": body.strategy},
    )


@app.post("/api/orphan/close", response_model=ActionResponse)
async def close_orphan(body: PositionCloseBody, _: str = Depends(verify_token)) -> ActionResponse:
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
    # Safety-first: hard-stop new entries before force-closing positions.
    _GLOBAL_STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    _GLOBAL_STOP_FILE.touch()
    logger.info("Global STOP file created — close-all will run in halted mode")
    if _hub_state_ref is not None:
        _hub_state_ref.read_trade_queue().proposals.clear()
        logger.info("Trade queue purged (close-all)")
    if bid and bid != "all":
        stop_resp = await _forward_to_bot(bid, "/api/stop-trading", {})
        close_resp = await _forward_to_bot(bid, "/api/close-all", {})
        ok = stop_resp.success and close_resp.success
        msg = f"halt={stop_resp.message or 'ok'}; close={close_resp.message or 'ok'}"
        nudge_ws()
        return ActionResponse(success=ok, message=msg)
    stop_result = await _broadcast_to_remote_bots("/api/stop-trading", {})
    close_result = await _broadcast_to_remote_bots("/api/close-all", {})
    nudge_ws()
    return ActionResponse(success=True, message=f"stop: {stop_result}; close: {close_result}")


_GLOBAL_STOP_FILE = Path("data/STOP")


@app.post("/api/stop-trading", response_model=ActionResponse)
async def stop_trading(body: BotActionBody | None = None, _: str = Depends(verify_token)) -> ActionResponse:
    bid = (body.bot_id if body else "") or ""
    _GLOBAL_STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    _GLOBAL_STOP_FILE.touch()
    logger.info("Global STOP file created — hub will not serve proposals")
    if bid and bid != "all":
        return await _forward_to_bot(bid, "/api/stop-trading", {})
    result = await _broadcast_to_remote_bots("/api/stop-trading", {})
    nudge_ws()
    return ActionResponse(success=True, message=result or "broadcast sent")


@app.post("/api/resume-trading", response_model=ActionResponse)
async def resume_trading(body: BotActionBody | None = None, _: str = Depends(verify_token)) -> ActionResponse:
    bid = (body.bot_id if body else "") or ""
    _GLOBAL_STOP_FILE.unlink(missing_ok=True)
    logger.info("Global STOP file removed — hub will resume serving proposals")
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
    if name not in ("intel", "news", "scanner", "analytics", "openclaw"):
        return ActionResponse(success=False, message=f"Unknown module: {name}")
    if (
        _monitor_ref is None
        or not hasattr(_monitor_ref, "set_module_enabled")
        or not hasattr(_monitor_ref, "is_module_enabled")
    ):
        return ActionResponse(success=False, message=f"{name} runtime toggle unavailable")

    currently_enabled = bool(_monitor_ref.is_module_enabled(name))
    requested_enabled = not currently_enabled
    enabled_now = await _monitor_ref.set_module_enabled(name, requested_enabled)

    if requested_enabled:
        if enabled_now:
            nudge_ws()
            return ActionResponse(success=True, message=f"{name} enabled")
        return ActionResponse(
            success=False,
            message=f"{name} enable failed",
        )

    if enabled_now:
        return ActionResponse(success=False, message=f"{name} disable failed")

    # Immediate hard-isolation for OpenClaw data. Other modules refresh on next monitor tick.
    if name == "openclaw":
        _clear_openclaw_intel_cache()
    nudge_ws()
    return ActionResponse(success=True, message=f"{name} disabled")


def _clear_openclaw_intel_cache() -> None:
    if _hub_state_ref is None:
        return
    snap = _hub_state_ref.read_intel().model_copy(deep=True)
    snap.openclaw_regime = "unknown"
    snap.openclaw_regime_confidence = 0.0
    snap.openclaw_regime_why = []
    snap.openclaw_sentiment_score = 50
    snap.openclaw_long_short_ratio = 0.0
    snap.openclaw_liquidations_24h_usd = 0.0
    snap.openclaw_open_interest_24h_usd = 0.0
    snap.openclaw_idea_briefs = []
    snap.openclaw_failure_triage = []
    snap.openclaw_experiments = []
    snap.sources_active = [s for s in snap.sources_active if s != "openclaw"]
    source_ts = dict(snap.source_timestamps or {})
    source_ts.pop("openclaw", None)
    snap.source_timestamps = source_ts
    _hub_state_ref.write_intel(snap)


# --------------- Bot Profiles (dynamic container management) ---------------


@app.get("/api/bot-profiles", response_model=list[BotProfileInfo])
async def get_bot_profiles(_: str = Depends(verify_token)) -> list[BotProfileInfo]:
    """List all bot profiles with their hub-controlled status."""
    from config.bot_profiles import ALL_PROFILES, is_default_enabled

    hub = _get_hub_db()
    enabled_map = hub.get_all_bot_enabled()
    visible_ids_raw = str(os.getenv("BOT_PROFILES_VISIBLE_IDS", "") or "").strip()
    visible_ids = {part.strip() for part in visible_ids_raw.split(",") if part.strip()} if visible_ids_raw else None

    result: list[BotProfileInfo] = []
    now_ts = time.time()
    for p in ALL_PROFILES:
        if p.is_hub:
            continue
        if visible_ids is not None and p.id not in visible_ids:
            continue
        default_enabled = is_default_enabled(p.id)
        enabled = bool(enabled_map.get(p.id, default_enabled))
        rpt = _bot_reports.get(p.id, {})
        s = rpt.get("status", {})
        positions = rpt.get("positions", [])
        if not rpt:
            container_status = "idle"
        else:
            # Disabled profiles should never display as "running" in the UI.
            # If they still have positions, show winding_down; otherwise idle.
            if not enabled and positions:
                container_status = "winding_down"
            elif not enabled:
                container_status = "idle"
            else:
                running = bool(s.get("running"))
                container_status = "running" if running else "idle"
        # Keep profile toggle state aligned with runtime reality:
        # if a profile stays idle beyond activation grace, force-disable it.
        pending_since = _pending_profile_enable_since.get(p.id)
        if container_status == "running":
            _pending_profile_enable_since.pop(p.id, None)
            _activation_nudge_since.pop(p.id, None)
        elif enabled:
            pending_fresh = bool(pending_since and (now_ts - pending_since) < _PENDING_ENABLE_GRACE_SECS)
            # Auto-disable only for stale enables initiated via this API session.
            # We intentionally avoid forcing OFF historical persisted states here,
            # because startup/warmup/report timing can otherwise disable healthy bots.
            should_force_disable = pending_since is not None
            if should_force_disable and not pending_fresh:
                hub.set_bot_enabled(p.id, False)
                enabled = False
                enabled_map[p.id] = False
                _pending_profile_enable_since.pop(p.id, None)
            # Persisted enabled profiles can survive rebuilds without a fresh toggle.
            # Re-emit activation marker while idle so bots can leave lean-idle mode.
            if enabled and container_status == "idle":
                last_nudge = _activation_nudge_since.get(p.id, 0.0)
                if (now_ts - last_nudge) >= _ACTIVATION_NUDGE_SECS:
                    _write_activation_file(p.id)
                    _activation_nudge_since[p.id] = now_ts
        summary = hub.get_bot_summary(p.id)
        exchange_name = str(rpt.get("exchange", "") or p.env_overrides.get("EXCHANGE", "") or "").strip().upper()
        balance_now: float | None
        if s:
            available = float(s.get("available_margin", 0.0) or 0.0)
            margin_used = sum(
                float(pos.get("notional_value", 0.0) or 0.0) / max(float(pos.get("leverage", 1) or 1), 1.0)
                for pos in positions
            )
            unrealized = sum(float(pos.get("pnl_usd", pos.get("pnl", 0.0)) or 0.0) for pos in positions)
            balance_now = max(0.0, available + margin_used + unrealized)
        else:
            balance_now = None

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
                exchange=exchange_name,
                balance=balance_now,
                daily_pnl=s.get("daily_pnl") if s else None,
                lifetime_pnl=float(summary.get("total_pnl", 0.0) or 0.0),
                wins=summary.get("wins", 0),
                losses=summary.get("losses", 0),
                open_positions=len(positions),
            )
        )
    return result


@app.post("/api/bot-profile/{profile_id}/toggle", response_model=ActionResponse)
async def toggle_bot_profile(profile_id: str, _: str = Depends(verify_token)) -> ActionResponse:
    """Enable or disable a bot via hub DB config.

    When enabling an idle bot, writes an activation file to the shared data
    volume so the bot can detect activation without hub communication.
    """
    from config.bot_profiles import PROFILES_BY_ID, is_default_enabled

    profile = PROFILES_BY_ID.get(profile_id)
    if not profile:
        return ActionResponse(success=False, message=f"Unknown profile: {profile_id}")

    if profile.is_hub:
        return ActionResponse(success=False, message="Hub bot cannot be toggled — it runs the dashboard")

    hub = _get_hub_db()
    currently_enabled = hub.is_bot_enabled(profile_id, default=is_default_enabled(profile_id))
    new_enabled = not currently_enabled
    hub.set_bot_enabled(profile_id, new_enabled)

    if new_enabled:
        _write_activation_file(profile_id)
        _pending_profile_enable_since[profile_id] = time.time()
    else:
        # Drop stale in-memory snapshot so disabled cards don't show old runtime state.
        _bot_reports.pop(profile_id, None)
        _pending_profile_enable_since.pop(profile_id, None)

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
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _get_db_conn() -> Any:
    """Get hub DB connection for the database explorer."""
    hub = _get_hub_db()
    return hub.conn


def _db_backend_for_conn(conn: Any) -> str:
    if conn is None:
        return "postgres"
    if getattr(conn, "is_local_fallback", False):
        return "local"
    return "postgres"


def _quote_ident(name: str, *, backend: str) -> str:
    if not _IDENT_RE.match(name):
        raise HTTPException(status_code=400, detail="invalid table name")
    if backend == "local":
        return f"[{name}]"
    return f'"{name}"'


def _get_db_tables() -> list[dict[str, Any]]:
    conn = _get_db_conn()
    if not conn:
        return []
    backend = _db_backend_for_conn(conn)
    if backend == "local":
        rows = conn.execute(
            """
            SELECT name
            FROM pragma_table_list
            WHERE type='table'
            ORDER BY name
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT table_name AS name
            FROM information_schema.tables
            WHERE table_schema='public'
            ORDER BY table_name
            """
        ).fetchall()
    result = []
    for r in rows:
        name = r["name"]
        qname = _quote_ident(name, backend=backend)
        count = conn.execute(f"SELECT COUNT(*) as c FROM {qname}").fetchone()["c"]
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
    backend = _db_backend_for_conn(conn)
    qname = _quote_ident(table_name, backend=backend)

    total = conn.execute(f"SELECT COUNT(*) as c FROM {qname}").fetchone()["c"]
    preview_cursor = conn.execute(f"SELECT * FROM {qname} LIMIT 0")
    preview_cols = [desc[0] for desc in preview_cursor.description] if preview_cursor.description else []
    order_col = next((c for c in ("id", "updated_at", "created_at", "opened_at") if c in preview_cols), "")
    order_sql = f" ORDER BY {_quote_ident(order_col, backend=backend)} DESC" if order_col else ""
    cursor = conn.execute(
        f"SELECT * FROM {qname}{order_sql} LIMIT ? OFFSET ?",
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
    if not isinstance(data, dict):
        return
    bot_id = _normalize_bot_id(data.get("bot_id", ""))
    if not bot_id:
        return
    stamped = dict(data)
    stamped["bot_id"] = bot_id
    stamped["_reported_at"] = time.time()
    if "exchange_balance" in stamped or "exchange_wallet_balance" in stamped:
        # Track when the raw exchange balance was actually observed.
        # Quick hub-check heartbeats update _reported_at but may not carry a
        # fresh exchange balance values; recency must follow this marker.
        stamped["_exchange_balance_reported_at"] = stamped["_reported_at"]
    existing = _bot_reports.get(bot_id)
    status_payload = stamped.get("status")
    has_full_status_payload = isinstance(status_payload, dict) and bool(status_payload)
    has_aux_payload = any(
        isinstance(stamped.get(key), list) and len(stamped.get(key) or []) > 0
        for key in ("positions", "wick_scalps", "strategies", "trade_log", "foreign_positions", "orphan_positions")
    ) or isinstance(stamped.get("daily_report"), dict)
    if existing is None:
        # Ignore first heartbeat-only payloads (quick hub checks) so they don't
        # create zero-state dashboard frames before first full status snapshot.
        if not (has_full_status_payload or has_aux_payload):
            return
        _bot_reports[bot_id] = stamped
    else:
        for key, value in stamped.items():
            existing[key] = value

    status = stamped.get("status")
    if isinstance(status, dict) and bool(status.get("running")):
        _pending_profile_enable_since.pop(bot_id, None)


def _maybe_record_exchange_equity_snapshot(data: dict[str, Any], hub: HubDB) -> None:
    """Persist throttled exchange equity snapshots from bot reports."""
    if not isinstance(data, dict):
        return
    exchange = str(data.get("exchange", "") or "").strip().upper()
    if not exchange:
        return
    status = data.get("status")
    if not isinstance(status, dict):
        return

    interval = max(10, int(get_settings().exchange_equity_snapshot_interval_seconds))
    now_ts = time.monotonic()
    last_ts = _exchange_snapshot_last_ts.get(exchange, 0.0)
    if (now_ts - last_ts) < interval:
        return

    available = float(data.get("exchange_balance", 0.0) or 0.0)
    positions = data.get("positions")
    if not isinstance(positions, list):
        positions = []
    margin_used = 0.0
    unrealized = 0.0
    open_positions = 0
    for p in positions:
        if not isinstance(p, dict):
            continue
        amount = float(p.get("amount", 0.0) or 0.0)
        if amount <= 0:
            continue
        open_positions += 1
        lev = max(float(p.get("leverage", 1.0) or 1.0), 1.0)
        notional = float(p.get("notional_value", 0.0) or 0.0)
        if notional <= 0:
            current = float(p.get("current_price", 0.0) or 0.0)
            notional = abs(amount) * current
        margin_used += notional / lev
        unrealized += float(p.get("pnl_usd", p.get("pnl", 0.0)) or 0.0)
    estimated_equity = max(0.0, available + margin_used + unrealized)
    source_bot = _normalize_bot_id(data.get("bot_id", ""))
    hub.insert_exchange_equity_snapshot(
        exchange=exchange,
        available_usdt=available,
        estimated_equity_usdt=estimated_equity,
        open_positions=open_positions,
        source_bot=source_bot,
        source="bot_report",
    )
    _exchange_snapshot_last_ts[exchange] = now_ts


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
    from config.bot_profiles import ALL_PROFILES, PROFILES_BY_ID, is_default_enabled

    reports = [r for r in _bot_reports.values() if isinstance(r, dict)]
    hub = _get_hub_db()
    enabled_map = hub.get_all_bot_enabled()

    all_positions: list[dict[str, Any]] = []
    all_wicks: list[dict[str, Any]] = []
    foreign_observations: list[dict[str, Any]] = []
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
    any_exchange_access_halted = False
    exchange_access_alert_message = ""
    exchange_access_alert_exchange = ""
    strategies_count = 0
    dynamic_count = 0
    bot_count = 0
    first_status: dict[str, Any] = {}
    exchange_balances: dict[str, float] = {}
    exchange_available: dict[str, float] = {}
    exchange_wallet: dict[str, float] = {}
    exchange_report_ts: dict[str, float] = {}
    bot_running: dict[str, bool] = {}
    bot_exchange: dict[str, str] = {}

    for p in ALL_PROFILES:
        if p.is_hub:
            continue
        bot_exchange[p.id] = str(p.env_overrides.get("EXCHANGE", "") or "").strip().upper()
        bot_running[p.id] = False

    for rpt in reports:
        s = rpt.get("status", {})
        if not isinstance(s, dict):
            s = {}
        bid = _normalize_bot_id(rpt.get("bot_id", ""))
        ex_name = str(rpt.get("exchange", "") or "").strip().upper()
        if bid and bid != "hub":
            if ex_name:
                bot_exchange[bid] = ex_name
            bot_running[bid] = bool(s.get("running"))

        if bid == "hub":
            continue
        default_enabled = is_default_enabled(bid) if bid in PROFILES_BY_ID else True
        if not enabled_map.get(bid, default_enabled):
            continue

        if not first_status:
            first_status = s

        ex_bal_raw = rpt.get("exchange_balance", 0)
        ex_wallet_raw = rpt.get("exchange_wallet_balance", 0)
        ex_reported_at = float(rpt.get("_exchange_balance_reported_at", 0.0) or 0.0)
        try:
            ex_bal = float(ex_bal_raw or 0.0)
        except (TypeError, ValueError):
            ex_bal = 0.0
        try:
            ex_wallet = float(ex_wallet_raw or 0.0)
        except (TypeError, ValueError):
            ex_wallet = 0.0
        if ex_name and ex_bal > 0:
            prev_ts = exchange_report_ts.get(ex_name, -1.0)
            if ex_reported_at >= prev_ts:
                exchange_report_ts[ex_name] = ex_reported_at
                # Use the newest bot snapshot for this exchange to avoid stale
                # high-watermark values when one bot report is old.
                exchange_available[ex_name] = ex_bal
                if ex_wallet > 0:
                    exchange_wallet[ex_name] = ex_wallet

        total_balance += float(s.get("balance", 0) or 0)
        total_available += float(s.get("available_margin", 0) or 0)
        total_daily_pnl += float(s.get("daily_pnl", 0) or 0)
        total_daily_pnl_pct += float(s.get("daily_pnl_pct", 0) or 0)
        total_growth_usd += float(s.get("total_growth_usd", 0) or 0)
        total_growth_pct += float(s.get("total_growth_pct", 0) or 0)
        total_profit_buffer += float(s.get("profit_buffer_pct", 0) or 0)
        total_uptime = max(total_uptime, float(s.get("uptime_seconds", 0) or 0))
        if s.get("running"):
            any_running = True
        if s.get("manual_stop_active"):
            any_halted = True
        if bool(s.get("exchange_access_halted")):
            any_exchange_access_halted = True
            if not exchange_access_alert_exchange:
                exchange_access_alert_exchange = ex_name or str(s.get("exchange_name", "") or "").strip().upper()
            reason = str(s.get("exchange_access_reason", "") or "").strip()
            if reason and not exchange_access_alert_message:
                exchange_access_alert_message = reason[:300]
        strategies_count += int(s.get("strategies_count", 0) or 0)
        dynamic_count += int(s.get("dynamic_strategies_count", 0) or 0)
        bot_count += 1

        positions = rpt.get("positions", [])
        if not isinstance(positions, list):
            positions = []
        for p in positions:
            if not isinstance(p, dict):
                continue
            p["bot_id"] = bid
            p["exchange_name"] = ex_name
            all_positions.append(p)
        wicks = rpt.get("wick_scalps", [])
        if not isinstance(wicks, list):
            wicks = []
        for w in wicks:
            if not isinstance(w, dict):
                continue
            w["bot_id"] = bid
            w["exchange_name"] = ex_name
            all_wicks.append(w)
        observations = rpt.get("foreign_positions", rpt.get("orphan_positions", []))
        if not isinstance(observations, list):
            observations = []
        for o in observations:
            if not isinstance(o, dict):
                continue
            o["detected_by_bot"] = bid
            o["exchange_name"] = ex_name
            foreign_observations.append(o)

        bot_snapshots.append(
            {
                "bot_id": bid,
                "exchange": ex_name,
                "connected": True,
                "data": {
                    "status": s,
                    "positions": positions,
                    "wick_scalps": wicks,
                    "intel": None,
                    "logs": [],
                },
            }
        )

    managed_symbol_keys = {
        (
            str(p.get("exchange_name", p.get("exchange", "")) or "").strip().upper(),
            _pair_symbol(p.get("symbol", "")),
        )
        for p in all_positions
        if isinstance(p, dict) and _pair_symbol(p.get("symbol", ""))
    }
    for wick in all_wicks:
        if not isinstance(wick, dict):
            continue
        managed_symbol_keys.add(
            (
                str(wick.get("exchange_name", wick.get("exchange", "")) or "").strip().upper(),
                _pair_symbol(wick.get("symbol", "")),
            )
        )

    ownership_by_key: dict[tuple[str, str], str] = {}
    ownership_by_symbol: dict[str, str] = {}
    original_owner_by_symbol: dict[str, str] = {}
    for row in hub.get_open_trade_owner_rows():
        owner_bot = _normalize_bot_id(row.get("bot_id", ""))
        symbol = _pair_symbol(row.get("symbol", ""))
        if not owner_bot or not symbol:
            continue
        if symbol not in ownership_by_symbol:
            ownership_by_symbol[symbol] = owner_bot
        exchange = bot_exchange.get(owner_bot, "").strip().upper()
        if exchange:
            key = (exchange, symbol)
            if key not in ownership_by_key:
                ownership_by_key[key] = owner_bot

    original_owner_rows_fn = getattr(hub, "get_original_trade_owner_rows", None)
    if callable(original_owner_rows_fn):
        for row in original_owner_rows_fn():
            owner_bot = _normalize_bot_id(row.get("bot_id", ""))
            symbol = _pair_symbol(row.get("symbol", ""))
            if not owner_bot or not symbol:
                continue
            if symbol not in original_owner_by_symbol:
                original_owner_by_symbol[symbol] = owner_bot

    recent_owner_rows_fn = getattr(hub, "get_recent_recovery_owner_rows", None)
    if callable(recent_owner_rows_fn):
        for row in recent_owner_rows_fn(24):
            owner_bot = _normalize_bot_id(row.get("bot_id", ""))
            symbol = _pair_symbol(row.get("symbol", ""))
            if not owner_bot or not symbol:
                continue
            if symbol not in ownership_by_symbol:
                ownership_by_symbol[symbol] = owner_bot

    latest_foreign_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for obs in foreign_observations:
        if not isinstance(obs, dict):
            continue
        exchange = str(obs.get("exchange_name", obs.get("exchange", "")) or "").strip().upper()
        symbol = _pair_symbol(obs.get("symbol", ""))
        if not exchange or not symbol:
            continue
        key = (exchange, symbol)
        if key in managed_symbol_keys:
            continue
        existing = latest_foreign_by_key.get(key)
        if existing is None:
            latest_foreign_by_key[key] = obs
            continue
        if str(obs.get("detected_at", "") or "") >= str(existing.get("detected_at", "") or ""):
            latest_foreign_by_key[key] = obs

    all_orphans: list[dict[str, Any]] = []
    for (exchange, symbol), obs in latest_foreign_by_key.items():
        current_owner_bot = ownership_by_key.get((exchange, symbol), "") or ownership_by_symbol.get(symbol, "")
        owner_is_running = bool(current_owner_bot and bot_running.get(current_owner_bot, False))
        if current_owner_bot and owner_is_running:
            continue
        original_owner_bot = original_owner_by_symbol.get(symbol, "") or current_owner_bot
        all_orphans.append(
            {
                **obs,
                "exchange_name": exchange,
                "symbol": symbol,
                "originally_opened_by": original_owner_bot,
                "owner_running": owner_is_running,
                "orphan_reason": "owner_not_running" if current_owner_bot else "no_owner_record",
            }
        )

    intel = _intel_snapshot()

    # When raw exchange balances are available, treat them as account-level
    # anchors and compute dashboard equity from available + used margin + uPnL.
    # Include both managed and orphan positions so equity does not oscillate
    # when a position temporarily moves between those groups.
    if exchange_available:
        exchange_margin_used: dict[str, float] = {}
        exchange_unrealized: dict[str, float] = {}
        all_live_rows = list(all_positions) + list(all_orphans)
        for p in all_live_rows:
            ex = str(p.get("exchange_name", "") or "")
            if not ex:
                continue
            lev = max(float(p.get("leverage", 1) or 1), 1.0)
            notional = float(p.get("notional_value", 0) or 0)
            if notional <= 0:
                amount = abs(float(p.get("amount", 0) or 0))
                current_price = float(p.get("current_price", 0) or 0)
                notional = amount * current_price
            exchange_margin_used[ex] = exchange_margin_used.get(ex, 0.0) + (notional / lev)
            upnl = float(p.get("pnl_usd", p.get("pnl", 0)) or 0)
            if upnl == 0 and "entry_price" in p and "current_price" in p and "amount" in p:
                entry = float(p.get("entry_price", 0) or 0)
                current = float(p.get("current_price", 0) or 0)
                amount = abs(float(p.get("amount", 0) or 0))
                side = str(p.get("side", "") or "").lower()
                if entry > 0 and amount > 0:
                    upnl = (entry - current) * amount if side in {"sell", "short"} else (current - entry) * amount
            exchange_unrealized[ex] = exchange_unrealized.get(ex, 0.0) + upnl

        exchange_equity: dict[str, float] = {}
        for ex in exchange_available:
            available = max(0.0, exchange_available.get(ex, 0.0))
            used = max(0.0, exchange_margin_used.get(ex, 0.0))
            upnl = exchange_unrealized.get(ex, 0.0)
            # Prefer wallet anchor from exchange payload when available.
            # Synthetic available+used+uPnL remains fallback for legacy reports.
            wallet = float(exchange_wallet.get(ex, 0.0) or 0.0)
            exchange_equity[ex] = wallet if wallet > 0 else max(0.0, available + used + upnl)

        real_balance = sum(exchange_equity.values())
        real_available = sum(max(0.0, v) for v in exchange_available.values())
        exchange_balances = exchange_equity
    else:
        real_balance = total_balance
        real_available = max(0.0, total_available)

    merged_status = {
        "bot_id": "all",
        "running": any_running,
        "trading_mode": first_status.get("trading_mode", "paper_local"),
        "exchange_name": first_status.get("exchange_name", ""),
        "exchange_url": first_status.get("exchange_url", ""),
        "balance": real_balance,
        "available_margin": real_available,
        "daily_pnl": total_daily_pnl,
        "daily_pnl_pct": total_daily_pnl_pct / bot_count if bot_count else 0,
        "tier": first_status.get("tier", "building"),
        "tier_progress_pct": first_status.get("tier_progress_pct", 0),
        "daily_target_pct": first_status.get("daily_target_pct", 10),
        "total_growth_usd": total_growth_usd,
        "total_growth_pct": total_growth_pct / bot_count if bot_count else 0,
        "uptime_seconds": total_uptime,
        "manual_stop_active": any_halted,
        "exchange_access_halted": any_exchange_access_halted,
        "exchange_access_alert_exchange": exchange_access_alert_exchange,
        "exchange_access_alert_message": exchange_access_alert_message,
        "strategies_count": strategies_count,
        "dynamic_strategies_count": dynamic_count,
        "profit_buffer_pct": total_profit_buffer / bot_count if bot_count else 0,
    }

    snapshot = {
        "status": merged_status,
        "positions": all_positions,
        "wick_scalps": all_wicks,
        "orphan_positions": all_orphans,
        "intel": intel.model_dump() if intel else None,
        "logs": list(_log_buffer),
        "bots": bot_snapshots,
        "exchange_balances": exchange_balances,
    }

    # Prevent brief zero-state flicker when an intermediate empty frame appears.
    global _last_stable_snapshot, _last_stable_snapshot_ts
    now_mono = time.monotonic()
    if bot_count == 0:
        if _last_stable_snapshot and (now_mono - _last_stable_snapshot_ts) <= 15.0:
            return _last_stable_snapshot
        return snapshot

    _last_stable_snapshot = snapshot
    _last_stable_snapshot_ts = now_mono
    return snapshot


def _backfill_open_trade_ownership_from_report(bot_id: str, payload: dict[str, Any], hub: Any) -> None:
    """Create minimal open ownership rows for live managed positions missing in DB."""
    positions = payload.get("positions")
    if not isinstance(positions, list):
        return
    existing = {_pair_symbol(s) for s in hub.get_open_trade_symbols()}
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        symbol = _pair_symbol(pos.get("symbol", ""))
        if not symbol or symbol in existing:
            continue
        side = str(pos.get("side", "") or "").strip().lower()
        if side not in {"long", "buy", "short", "sell"}:
            continue
        opened_at = str(pos.get("opened_at", "") or "").strip()
        if not opened_at:
            opened_at = datetime.now(UTC).isoformat()
        trade_row = {
            "symbol": symbol,
            "side": "short" if side in {"short", "sell"} else "long",
            "strategy": str(pos.get("strategy", "") or "runtime_recovered").strip() or "runtime_recovered",
            "action": "open",
            "opened_at": opened_at,
            "entry_price": float(pos.get("entry_price", 0.0) or 0.0),
            "amount": float(pos.get("amount", 0.0) or 0.0),
            "leverage": int(max(1, round(float(pos.get("leverage", 1) or 1)))),
            "close_source": "",
            "close_reason": "",
        }
        request_key = f"backfill:{bot_id}:{symbol}:{opened_at}"
        hub.insert_trade(bot_id, trade_row, request_key=request_key)
        existing.add(symbol)


@app.post("/internal/report")
async def receive_bot_report(request: Request, _: str = Depends(verify_token)) -> dict[str, Any]:
    """Bots POST snapshots here; hub returns all data bots need.

    Bots never touch the shared data volume — the hub acts as a proxy:
    - Reads intel, analytics, trade_queue, extreme_watchlist on their behalf
    - Writes bot_status on their behalf
    - Returns enabled flag, confirmed ack keys, and trade proposal
    """
    try:
        data = await request.json()
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    bot_id = _normalize_bot_id(data.get("bot_id", ""))
    if bot_id:
        data["bot_id"] = bot_id
    if bot_id:
        url = f"http://bot-{bot_id}:9035"
        if _bot_urls.get(bot_id) != url:
            _bot_urls[bot_id] = url
            _save_bot_registry()

    if bot_id and _hub_state_ref is not None:
        try:
            from shared.models import BotDeploymentStatus

            bot_status_data = data.get("bot_status")
            if bot_status_data:
                try:
                    _hub_state_ref.write_bot_status(BotDeploymentStatus(**bot_status_data))
                except Exception:
                    logger.warning("Ignoring malformed bot_status from {}", bot_id)

            exchange = str(data.get("exchange", "") or "").strip()

            combined_symbols: set[str] = set()
            open_symbols = data.get("open_symbols")
            if isinstance(open_symbols, list):
                combined_symbols |= {str(sym).strip() for sym in open_symbols if str(sym).strip()}
            if "positions" in data:
                positions = data["positions"] if isinstance(data["positions"], list) else []
                combined_symbols |= {
                    str(p.get("symbol", "")).strip()
                    for p in positions
                    if isinstance(p, dict) and str(p.get("symbol", "")).strip()
                }
            if exchange and combined_symbols:
                _hub_state_ref.update_bot_positions(bot_id, exchange, combined_symbols)
            elif exchange and isinstance(open_symbols, list):
                _hub_state_ref.update_bot_positions(bot_id, exchange, set())
        except Exception as e:
            logger.warning("Failed to process bot report metadata for {}: {}", bot_id, e)

    report_bot_snapshot(data)
    hub = _get_hub_db()
    if bot_id:
        with contextlib.suppress(Exception):
            _backfill_open_trade_ownership_from_report(bot_id, data, hub)
    with contextlib.suppress(Exception):
        _maybe_record_exchange_equity_snapshot(data, hub)
    confirmed = hub.drain_confirmed_keys(bot_id) if bot_id else []
    if bot_id:
        from config.bot_profiles import PROFILES_BY_ID, is_default_enabled

        default_enabled = is_default_enabled(bot_id) if bot_id in PROFILES_BY_ID else True
        enabled = hub.is_bot_enabled(bot_id, default=default_enabled)
    else:
        enabled = True

    response: dict[str, Any] = {
        "status": "ok",
        "confirmed_keys": confirmed,
        "enabled": enabled,
    }

    bot_ready = data.get("ready", False)
    if _GLOBAL_STOP_FILE.exists():
        bot_ready = False
    if bot_id and _hub_state_ref is not None and bot_ready:
        with contextlib.suppress(Exception):
            from config.bot_profiles import PROFILES_BY_ID
            from shared.models import SignalPriority

            bot_style = data.get("bot_style", bot_id)
            bot_exchange = data.get("exchange", "")
            if not bot_exchange:
                existing_rpt = _bot_reports.get(bot_id, {})
                bot_exchange = existing_rpt.get("exchange", "")

            profile = PROFILES_BY_ID.get(bot_id)
            allowed = None
            if profile and profile.allowed_priorities:
                allowed = [SignalPriority(p) for p in profile.allowed_priorities]

            open_db_syms = hub.get_open_trade_symbols()
            proposal = _hub_state_ref.serve_proposal_to_bot(
                bot_style=bot_style,
                bot_id=bot_id,
                exchange=bot_exchange,
                allowed_priorities=allowed,
                open_db_symbols=open_db_syms,
            )
            if proposal:
                response["proposal"] = proposal.model_dump()

    return response


@app.post("/internal/bot-disable")
async def internal_disable_bot(request: Request, _: str = Depends(verify_token)) -> dict[str, Any]:
    """Internal endpoint: force-disable a bot profile in hub config."""
    data = await request.json()
    bot_id = _normalize_bot_id(data.get("bot_id", ""))
    reason = str(data.get("reason", "") or "").strip()
    if not bot_id:
        return {"status": "error", "detail": "missing bot_id"}
    hub = _get_hub_db()
    hub.set_bot_enabled(bot_id, False)
    logger.warning("Hub auto-disabled bot {} ({})", bot_id, reason or "no reason")
    nudge_ws()
    return {"status": "ok", "bot_id": bot_id, "enabled": False}


@app.post("/internal/bot-error")
async def internal_bot_error(request: Request, _: str = Depends(verify_token)) -> dict[str, Any]:
    """Receive critical/error bot events and surface them in hub logs/dashboard."""
    try:
        data = await request.json()
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}

    bot_id = _normalize_bot_id(data.get("bot_id", ""))
    exchange = str(data.get("exchange", "") or "").strip().upper() or "UNKNOWN"
    severity = str(data.get("severity", "critical") or "critical").strip().lower()
    code = str(data.get("code", "unknown") or "unknown").strip() or "unknown"
    context = str(data.get("context", "runtime") or "runtime").strip() or "runtime"
    message = str(data.get("message", "") or "").strip()[:1200]
    extra = data.get("extra")
    if not isinstance(extra, dict):
        extra = {}

    if not bot_id:
        return {"status": "error", "detail": "missing bot_id"}
    if not message:
        return {"status": "error", "detail": "missing message"}

    if severity == "warning":
        logger.warning(
            "BOT_EVENT [{}] {} {} [{}] {} | extra={}",
            bot_id,
            exchange,
            code,
            context,
            message,
            extra,
        )
    else:
        logger.error(
            "BOT_EVENT [{}] {} {} [{}] {} | extra={}",
            bot_id,
            exchange,
            code,
            context,
            message,
            extra,
        )
    nudge_ws()
    return {"status": "ok"}


@app.post("/internal/queue-update")
async def queue_update(request: Request, _: str = Depends(verify_token)) -> dict[str, Any]:
    """Immediate consume/reject report from a bot — updates the queue right away."""
    data = await request.json()
    bot_id = _normalize_bot_id(data.get("bot_id", ""))
    exchange = data.get("exchange", "")
    action = data.get("action", "")
    proposal_id = data.get("proposal_id", "")
    reason = data.get("reason", "")

    if not bot_id or not proposal_id or action not in ("consumed", "rejected"):
        return {"status": "error", "detail": "missing bot_id, proposal_id, or invalid action"}

    if _hub_state_ref is None:
        return {"status": "error", "detail": "hub not ready"}

    if action == "consumed":
        _hub_state_ref.handle_consume(proposal_id, exchange, bot_id)
    else:
        _hub_state_ref.handle_reject(proposal_id, exchange, bot_id, reason)

    return {"status": "ok"}


@app.get("/internal/intel")
async def get_bot_intel(_: str = Depends(verify_token)) -> dict[str, Any]:
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


@app.post("/internal/trade-reserve")
async def reserve_trade_open(request: Request, _: str = Depends(verify_token)) -> dict[str, Any]:
    """Create a pre-open ownership row so restart recovery has deterministic bot ownership."""
    data = await request.json()
    bot_id = _normalize_bot_id(data.get("bot_id", ""))
    trade = data.get("trade", {}) or {}
    request_key = data.get("request_key", "")
    if not bot_id or not isinstance(trade, dict):
        return {"status": "error", "detail": "missing bot_id or trade"}

    opened_at = str(trade.get("opened_at", "") or "")
    if not opened_at:
        return {"status": "error", "detail": "missing opened_at in trade reservation"}

    reserve_trade = {
        "symbol": trade.get("symbol", ""),
        "side": trade.get("side", ""),
        "strategy": trade.get("strategy", ""),
        "action": "open",
        "opened_at": opened_at,
        "market_regime": trade.get("market_regime", ""),
        "fear_greed": trade.get("fear_greed", 50),
        "daily_tier": trade.get("daily_tier", ""),
        "daily_pnl_at_entry": trade.get("daily_pnl_at_entry", 0.0),
        "signal_strength": trade.get("signal_strength", 0.0),
        "planned_stop_loss": trade.get("planned_stop_loss", 0.0),
        "planned_tp1": trade.get("planned_tp1", 0.0),
        "planned_tp2": trade.get("planned_tp2", 0.0),
    }
    hub = _get_hub_db()
    try:
        hub.insert_trade(bot_id, reserve_trade, request_key=request_key)
    except Exception as e:
        msg = str(e)
        if msg.startswith("open_owner_conflict:"):
            parts = msg.split(":", 2)
            conflict_symbol = parts[1] if len(parts) > 1 else reserve_trade.get("symbol", "")
            owner_bot = parts[2] if len(parts) > 2 else ""
            return {
                "status": "conflict",
                "detail": "symbol already has open owner",
                "symbol": conflict_symbol,
                "owner_bot_id": owner_bot,
                "request_key": request_key,
            }
        logger.warning("Trade reserve failed for {} {}: {}", bot_id, reserve_trade.get("symbol", ""), e)
        raise HTTPException(status_code=503, detail=f"hub_db_unavailable: {e}") from e
    return {"status": "ok", "opened_at": opened_at, "request_key": request_key}


@app.post("/internal/trade")
async def receive_trade(request: Request, _: str = Depends(verify_token)) -> dict[str, Any]:
    """Bots push trade open/close events here. Hub writes to its own DB.

    Accepts ``request_key`` for idempotent writes and deferred ack.
    """
    data = await request.json()
    bot_id = _normalize_bot_id(data.get("bot_id", ""))
    action = data.get("action", "")
    trade = data.get("trade", {})
    request_key = data.get("request_key", "")
    if not bot_id or not trade:
        return {"status": "error", "detail": "missing bot_id or trade"}
    opened_at = str(trade.get("opened_at", "") or "").strip()

    hub = _get_hub_db()
    try:
        if action == "cancel_reservation":
            if not opened_at:
                logger.warning(
                    "Ignoring cancel_reservation write for bot={} symbol={} (missing opened_at)",
                    bot_id,
                    trade.get("symbol", ""),
                )
                return {
                    "status": "ignored",
                    "action": action,
                    "request_key": request_key,
                    "detail": "missing_opened_at",
                }
            hub.cancel_trade_reservation(bot_id, opened_at, request_key=request_key)
        elif action == "open":
            if not opened_at:
                logger.warning(
                    "Ignoring open write for bot={} symbol={} (missing opened_at)",
                    bot_id,
                    trade.get("symbol", ""),
                )
                return {
                    "status": "ignored",
                    "action": action,
                    "request_key": request_key,
                    "detail": "missing_opened_at",
                }
            updated = hub.update_trade_open(bot_id, opened_at, trade, request_key=request_key)
            if not updated:
                logger.warning(
                    "Deferred open write for bot={} opened_at={} symbol={} (reservation row missing)",
                    bot_id,
                    trade.get("opened_at", ""),
                    trade.get("symbol", ""),
                )
                return {"status": "deferred", "action": action, "request_key": request_key}
        elif action == "close":
            if not opened_at:
                logger.warning(
                    "Ignoring close write for bot={} symbol={} (missing opened_at)",
                    bot_id,
                    trade.get("symbol", ""),
                )
                return {
                    "status": "ignored",
                    "action": action,
                    "request_key": request_key,
                    "detail": "missing_opened_at",
                }
            updated = hub.update_trade_close(bot_id, opened_at, trade, request_key=request_key)
            if not updated:
                logger.warning(
                    "Deferred close write for bot={} opened_at={} symbol={} (open row missing)",
                    bot_id,
                    trade.get("opened_at", ""),
                    trade.get("symbol", ""),
                )
                return {"status": "deferred", "action": action, "request_key": request_key}
        elif action == "update":
            if not opened_at:
                logger.warning(
                    "Ignoring runtime update for bot={} symbol={} (missing opened_at)",
                    bot_id,
                    trade.get("symbol", ""),
                )
                return {
                    "status": "ignored",
                    "action": action,
                    "request_key": request_key,
                    "detail": "missing_opened_at",
                }
            updated = hub.update_trade_runtime(bot_id, opened_at, trade, request_key=request_key)
            if not updated:
                logger.warning(
                    "Deferred runtime update for bot={} opened_at={} symbol={} (open row missing)",
                    bot_id,
                    trade.get("opened_at", ""),
                    trade.get("symbol", ""),
                )
                return {"status": "deferred", "action": action, "request_key": request_key}
        else:
            hub.insert_trade(bot_id, trade, request_key=request_key)
    except Exception as e:
        msg = str(e)
        if msg.startswith("open_owner_conflict:"):
            parts = msg.split(":", 2)
            conflict_symbol = parts[1] if len(parts) > 1 else str(trade.get("symbol", "") or "")
            owner_bot = parts[2] if len(parts) > 2 else ""
            return {
                "status": "conflict",
                "action": action,
                "request_key": request_key,
                "detail": "symbol already has open owner",
                "symbol": conflict_symbol,
                "owner_bot_id": owner_bot,
            }
        logger.warning("Trade write failed action={} bot_id={}: {}", action, bot_id, e)
        raise HTTPException(status_code=503, detail=f"hub_db_unavailable: {e}") from e

    return {"status": "ok", "action": action, "request_key": request_key}


@app.get("/internal/trades/{bot_id}/open")
async def get_bot_open_trades(bot_id: str, _: str = Depends(verify_token)) -> list[dict[str, Any]]:
    """Return open (unclosed) trades for a bot — used on bot startup to recover state."""
    hub = _get_hub_db()
    trades = hub.get_open_trades_for_bot(_normalize_bot_id(bot_id))
    return [t.model_dump() for t in trades]


@app.get("/internal/trades/{bot_id}/recovery-owners")
async def get_bot_recovery_owner_symbols(bot_id: str, _: str = Depends(verify_token)) -> list[str]:
    """Return recent recovery-owner symbols for bot startup orphan re-attachment."""
    hub = _get_hub_db()
    return hub.get_recent_recovery_owner_symbols(_normalize_bot_id(bot_id), lookback_hours=24)


@app.get("/internal/trades/{bot_id}/stats")
async def get_bot_strategy_stats(bot_id: str, _: str = Depends(verify_token)) -> dict[str, dict[str, Any]]:
    """Return per-strategy stats for a bot, keyed by 'strategy:symbol'."""
    hub = _get_hub_db()
    return hub.get_all_strategy_stats_for_bot(_normalize_bot_id(bot_id))


@app.post("/internal/entry-plan/sync")
async def sync_swing_entry_plan(request: Request, _: str = Depends(verify_token)) -> dict[str, Any]:
    """Persist swing ladder entries for one open trade."""
    data = await request.json()
    bot_id = _normalize_bot_id(data.get("bot_id", ""))
    symbol = _pair_symbol(data.get("symbol", ""))
    opened_at = str(data.get("opened_at", "") or "").strip()
    entries = data.get("entries", [])
    if not bot_id or not symbol or not opened_at or not isinstance(entries, list):
        return {"status": "error", "detail": "missing bot_id/symbol/opened_at/entries"}
    hub = _get_hub_db()
    hub.replace_swing_entry_plan(bot_id, symbol, opened_at, entries)
    return {"status": "ok", "count": len(entries)}


@app.get("/internal/entry-plan/{bot_id}/{symbol:path}")
async def get_swing_entry_plan(
    bot_id: str,
    symbol: str,
    opened_at: str,
    _: str = Depends(verify_token),
) -> dict[str, Any]:
    """Load persisted swing ladder entries for one open trade."""
    hub = _get_hub_db()
    entries = hub.get_swing_entry_plan(_normalize_bot_id(bot_id), _pair_symbol(symbol), str(opened_at or "").strip())
    return {"status": "ok", "entries": entries}


@app.post("/internal/recovery-close")
async def recovery_close_trade(request: Request, _: str = Depends(verify_token)) -> dict[str, Any]:
    """Bot reports a trade that died while it was down.

    Accepts optional estimated exit/PnL fields for forensic visibility.
    """
    data = await request.json()
    bot_id = _normalize_bot_id(data.get("bot_id", ""))
    opened_at = data.get("opened_at", "")
    if not bot_id or not opened_at:
        return {"status": "error", "detail": "missing bot_id or opened_at"}
    estimated_exit_price = float(data.get("estimated_exit_price", 0.0) or 0.0)
    estimated_pnl_usd = float(data.get("estimated_pnl_usd", 0.0) or 0.0)
    estimated_pnl_pct = float(data.get("estimated_pnl_pct", 0.0) or 0.0)
    hub = _get_hub_db()
    updated = hub.mark_recovery_close(
        bot_id,
        opened_at,
        estimated_exit_price=estimated_exit_price,
        estimated_pnl_usd=estimated_pnl_usd,
        estimated_pnl_pct=estimated_pnl_pct,
    )
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
