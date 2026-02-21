"""Tests for web/server.py — REST and action endpoints."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from web.server import _ALLOWED_TABLES, _bot_reports, _get_hub_db, app, report_bot_snapshot, set_bot

# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def mock_bot():
    """Minimal TradingBot-like mock for dashboard endpoints."""
    bot = MagicMock()
    bot._running = False
    bot.settings = MagicMock()
    bot.settings.bot_id = ""
    bot.settings.trading_mode = "paper_local"
    bot.settings.exchange = "mexc"
    bot.settings.platform_url = "https://www.mexc.com"
    bot.settings.symbol_platform_url = MagicMock(return_value="https://www.mexc.com/trade/BTC_USDT")
    bot.settings.intel_enabled = True
    bot.settings.news_enabled = False
    bot.settings.spike_threshold_pct = 5.0
    bot.settings.dashboard_port = 8765
    bot.target = MagicMock()
    bot.target._current_balance = 10_000.0
    bot.target.todays_pnl = 500.0
    bot.target.todays_pnl_pct = 5.0
    bot.target.tier = MagicMock(value="strong")
    bot.target.progress_pct = 50.0
    bot.target.daily_target_pct = 10.0
    bot.target.total_growth_pct = 25.0
    bot.target.manual_stop = False
    bot.target.profit_buffer_pct = 0.0
    bot.target._profit_buffer_pct = 0.0
    bot.target._initial_capital = 9_000.0
    bot.target.history = []
    bot.target.winning_days = 3
    bot.target.losing_days = 1
    bot.target.target_hit_days = 2
    bot.target.avg_daily_pnl_pct = 8.0
    bot.target.best_day = None
    bot.target.worst_day = None
    bot.target.compound_report = MagicMock(return_value="Day 1: +5%")
    bot.target.projected_balance = {"1_week": 10_500.0, "1_month": 11_000.0, "3_months": 12_000.0}
    bot.target.STOP_FILE = Path("STOP")
    bot.start = AsyncMock()
    bot._strategies = []
    bot._dynamic_strategies = {}
    bot.risk = MagicMock()
    bot.risk._base_max_daily_loss_pct = 3.0
    bot.risk.max_daily_loss_pct = 3.0
    bot.orders = MagicMock()
    bot.orders.trailing = MagicMock()
    bot.orders.trailing.active_stops = {}
    bot.orders.scaler = MagicMock()
    bot.orders.scaler.get = MagicMock(return_value=None)
    bot.orders._trade_log = []
    bot.orders.wick_scalper = MagicMock()
    bot.orders.wick_scalper.active_scalps = {}
    bot.orders.execute_signal = AsyncMock()
    bot.exchange = AsyncMock()
    bot.exchange.fetch_positions = AsyncMock(return_value=[])
    bot.intel = None
    bot._multibot = False
    bot.news = MagicMock()
    bot.news.enabled = False
    bot.news._running = False
    bot.news.start = AsyncMock()
    bot.news.stop = AsyncMock()
    bot.scanner = MagicMock()
    bot.scanner.hot_movers = []
    bot._recent_news = []
    bot.analytics = MagicMock()
    bot.analytics.scores = {}
    bot.analytics.patterns = []
    bot.analytics.suggestions = []
    bot.analytics.refresh = MagicMock()
    bot.trade_db = MagicMock()
    bot.trade_db.get_hourly_performance = MagicMock(return_value=[])
    bot.trade_db.get_regime_performance = MagicMock(return_value=[])
    bot.trade_db.trade_count = MagicMock(return_value=0)
    bot._close_all_positions = AsyncMock()
    return bot


@pytest.fixture
def auth_override():
    """Override auth so endpoints don't require a real token."""
    from web.auth import verify_token

    async def _no_auth():
        return "test-token"

    app.dependency_overrides[verify_token] = _no_auth
    yield
    app.dependency_overrides.pop(verify_token, None)


@pytest.fixture
async def client(auth_override):
    _bot_reports.clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    _bot_reports.clear()


# ── GET /health (no auth) ─────────────────────────────────────────────


class TestHealth:
    async def test_health_no_bot(self, client):
        set_bot(None)  # type: ignore[arg-type]
        r = await client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["bot_running"] is False

    async def test_health_with_bot(self, client, mock_bot):
        mock_bot._running = True
        set_bot(mock_bot)  # type: ignore[arg-type]
        r = await client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["bot_running"] is True

    async def test_health_no_auth_required(self):
        """Health endpoint works without auth override."""
        from web.auth import verify_token

        app.dependency_overrides.pop(verify_token, None)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.get("/health")
        assert r.status_code == 200


# ── GET /api/bots ────────────────────────────────────────────────────


class TestGetBots:
    async def test_bots_from_reports(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        _bot_reports.clear()
        report_bot_snapshot(
            {
                "bot_id": "momentum",
                "bot_style": "momentum",
                "exchange": "MEXC",
                "status": {"running": True},
                "positions": [],
                "wick_scalps": [],
                "strategies": [{"name": "rsi", "symbol": "BTC/USDT"}],
            }
        )
        r = await client.get("/api/bots")
        assert r.status_code == 200
        bots = r.json()
        assert len(bots) >= 1
        bot0 = bots[0]
        assert bot0["bot_id"] == "momentum"
        assert bot0["exchange"] == "MEXC"
        _bot_reports.clear()

    async def test_bots_fallback_to_current_bot(self, client, mock_bot):
        mock_bot.settings.bot_id = "solo"
        mock_bot.settings.dashboard_port = 9035
        mock_bot.settings.bot_strategy_list = ["rsi"]
        set_bot(mock_bot)  # type: ignore[arg-type]
        _bot_reports.clear()
        r = await client.get("/api/bots")
        assert r.status_code == 200
        bots = r.json()
        assert len(bots) == 1
        assert bots[0]["bot_id"] == "solo"
        assert bots[0]["exchange"] == "MEXC"


# ── GET /api/status ───────────────────────────────────────────────────


class TestGetStatus:
    async def test_status_no_bot(self, client):
        set_bot(None)  # type: ignore[arg-type]
        r = await client.get("/api/status")
        assert r.status_code == 200
        data = r.json()
        assert data["running"] is False
        assert "balance" in data

    async def test_status_with_bot(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        r = await client.get("/api/status")
        assert r.status_code == 200
        data = r.json()
        assert data["trading_mode"] == "paper_local"
        assert data["exchange_name"] == "MEXC"
        assert data["balance"] == 10_000.0
        assert data["daily_pnl"] == 500.0
        assert data["strategies_count"] == 0

    async def test_status_includes_total_growth_usd(self, client, mock_bot):
        mock_bot.target._current_balance = 10000.0
        mock_bot.target._initial_capital = 9000.0
        set_bot(mock_bot)  # type: ignore[arg-type]
        r = await client.get("/api/status")
        assert r.status_code == 200
        data = r.json()
        assert data["total_growth_usd"] == pytest.approx(1000.0)


# ── GET /api/positions ────────────────────────────────────────────────


class TestGetPositions:
    async def test_positions_no_bot(self, client):
        set_bot(None)  # type: ignore[arg-type]
        r = await client.get("/api/positions")
        assert r.status_code == 200
        assert r.json() == []

    async def test_positions_empty(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        mock_bot.exchange.fetch_positions = AsyncMock(return_value=[])
        r = await client.get("/api/positions")
        assert r.status_code == 200
        assert r.json() == []

    async def test_positions_with_position(self, client, mock_bot):
        from core.models import OrderSide, Position

        set_bot(mock_bot)  # type: ignore[arg-type]
        pos = Position(
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            amount=0.1,
            entry_price=50_000.0,
            current_price=52_000.0,
            leverage=10,
            market_type="futures",
            unrealized_pnl=200.0,
            strategy="rsi",
            opened_at=datetime.now(UTC),
        )
        mock_bot.exchange.fetch_positions = AsyncMock(return_value=[pos])
        r = await client.get("/api/positions")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["symbol"] == "BTC/USDT"
        assert data[0]["side"] == "buy"
        assert data[0]["amount"] == 0.1

    async def test_positions_fetch_exception_returns_empty(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        mock_bot.exchange.fetch_positions = AsyncMock(side_effect=Exception("API error"))
        r = await client.get("/api/positions")
        assert r.status_code == 200
        assert r.json() == []


# ── GET /api/trades ────────────────────────────────────────────────────


class TestGetTrades:
    async def test_trades_no_bot(self, client):
        set_bot(None)  # type: ignore[arg-type]
        r = await client.get("/api/trades")
        assert r.status_code == 200
        assert r.json() == []

    async def test_trades_with_log(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        mock_bot.orders._trade_log = [
            {
                "timestamp": "2024-01-01T12:00:00",
                "symbol": "BTC/USDT",
                "side": "buy",
                "action": "open",
                "amount": 0.1,
                "price": 50000,
                "strategy": "rsi",
                "pnl": 0,
            },
        ]
        r = await client.get("/api/trades")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["symbol"] == "BTC/USDT"
        assert data[0]["action"] == "open"


# ── GET /api/trade-queue ────────────────────────────────────────────────


class TestGetTradeQueue:
    async def test_trade_queue_returns_list(self, client):
        with patch("web.server.SharedState") as MockSharedState:
            mock_state = MagicMock()
            from shared.models import TradeQueue

            mock_state.read_trade_queue.return_value = TradeQueue()
            MockSharedState.return_value = mock_state
            r = await client.get("/api/trade-queue")
        assert r.status_code == 200
        assert r.json() == []

    async def test_trade_queue_returns_pending_proposals(self, client):
        from shared.models import SignalPriority, TradeProposal, TradeQueue

        with patch("web.server.SharedState") as MockSharedState:
            mock_state = MagicMock()
            q = TradeQueue()
            q.critical = [
                TradeProposal(
                    priority=SignalPriority.CRITICAL,
                    symbol="BTC/USDT",
                    side="long",
                    strategy="momentum",
                    strength=0.9,
                    created_at=datetime.now(UTC).isoformat(),
                ),
            ]
            mock_state.read_trade_queue.return_value = q
            MockSharedState.return_value = mock_state
            r = await client.get("/api/trade-queue")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["symbol"] == "BTC/USDT"
        assert data[0]["side"] == "long"
        assert data[0]["strategy"] == "momentum"
        assert data[0]["strength"] == 0.9
        assert "age_seconds" in data[0]


# ── GET /api/intel, /api/trending, /api/strategies ─────────────────────


class TestGetIntelTrendingStrategies:
    async def test_intel_no_bot(self, client):
        set_bot(None)  # type: ignore[arg-type]
        r = await client.get("/api/intel")
        assert r.status_code == 200
        assert r.json() is None

    async def test_intel_with_bot_no_intel(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        mock_bot.intel = None
        r = await client.get("/api/intel")
        assert r.status_code == 200
        assert r.json() is None

    async def test_intel_includes_macro_events(self, client, mock_bot):
        from intel.macro_calendar import EventImpact, MacroEvent

        set_bot(mock_bot)  # type: ignore[arg-type]
        cond = MagicMock()
        cond.regime = MagicMock(value="normal")
        cond.fear_greed = 45
        cond.fear_greed_bias = "fear"
        cond.liquidation_24h = 0
        cond.mass_liquidation = False
        cond.liquidation_bias = "neutral"
        cond.macro_event_imminent = True
        cond.macro_exposure_mult = 0.5
        cond.macro_spike_opportunity = False
        cond.next_macro_event = "FOMC in 1.5h"
        cond.whale_bias = "neutral"
        cond.overleveraged_side = ""
        cond.position_size_multiplier = 1.0
        cond.should_reduce_exposure = True
        cond.preferred_direction = "neutral"
        mock_bot.intel = MagicMock()
        mock_bot.intel.condition = cond
        mock_bot.intel.macro = MagicMock()
        mock_bot.intel.macro.upcoming_high_impact = [
            MacroEvent(
                title="FOMC Statement",
                date=datetime(2026, 3, 1, 18, 0, tzinfo=UTC),
                impact=EventImpact.CRITICAL,
            ),
        ]
        r = await client.get("/api/intel")
        assert r.status_code == 200
        data = r.json()
        assert data is not None
        assert len(data["macro_events"]) == 1
        assert data["macro_events"][0]["title"] == "FOMC Statement"
        assert data["macro_events"][0]["impact"] == "critical"

    async def test_trending_no_bot(self, client):
        set_bot(None)  # type: ignore[arg-type]
        r = await client.get("/api/trending")
        assert r.status_code == 200
        assert r.json() == []

    async def test_trending_with_coins(self, client, mock_bot):
        from scanner.trending import TrendingCoin

        set_bot(mock_bot)  # type: ignore[arg-type]
        mock_bot.scanner.hot_movers = [
            TrendingCoin(
                symbol="DOGE/USDT",
                name="Dogecoin",
                price=0.08,
                volume_24h=1e9,
                market_cap=11e9,
                change_1h=2.0,
                change_24h=10.0,
            ),
        ]
        r = await client.get("/api/trending")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["symbol"] == "DOGE/USDT"
        assert data[0]["change_24h"] == 10.0

    async def test_strategies_no_bot(self, client):
        set_bot(None)  # type: ignore[arg-type]
        r = await client.get("/api/strategies")
        assert r.status_code == 200
        assert r.json() == []

    async def test_strategies_with_strategies(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        _bot_reports.clear()
        report_bot_snapshot(
            {
                "bot_id": "test",
                "exchange": "MEXC",
                "status": {},
                "positions": [],
                "wick_scalps": [],
                "strategies": [
                    {
                        "name": "rsi",
                        "symbol": "BTC/USDT",
                        "market_type": "futures",
                        "leverage": 10,
                        "is_dynamic": False,
                        "open_now": 0,
                        "applied_count": 5,
                        "success_count": 3,
                        "fail_count": 2,
                    },
                ],
            }
        )
        r = await client.get("/api/strategies")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["name"] == "rsi"
        assert data[0]["is_dynamic"] is False
        _bot_reports.clear()


class TestGetStrategiesNoneStats:
    """Strategies endpoint when report has zero stats."""

    async def test_strategies_none_stats_returns_zero(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        _bot_reports.clear()
        report_bot_snapshot(
            {
                "bot_id": "test",
                "exchange": "MEXC",
                "status": {},
                "positions": [],
                "wick_scalps": [],
                "strategies": [
                    {
                        "name": "rsi",
                        "symbol": "BTC/USDT",
                        "market_type": "futures",
                        "leverage": 10,
                        "is_dynamic": False,
                        "open_now": 0,
                        "applied_count": 0,
                        "success_count": 0,
                        "fail_count": 0,
                    },
                ],
            }
        )
        r = await client.get("/api/strategies")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["applied_count"] == 0
        assert data[0]["success_count"] == 0
        assert data[0]["fail_count"] == 0
        _bot_reports.clear()


# ── POST /internal/report & merged snapshot ──────────────────────────


class TestInternalReport:
    async def test_post_internal_report(self, client):
        payload = {
            "bot_id": "momentum",
            "exchange": "MEXC",
            "status": {
                "running": True,
                "balance": 1000,
                "available_margin": 500,
                "daily_pnl": 50,
                "daily_pnl_pct": 5,
                "total_growth_usd": 100,
                "total_growth_pct": 10,
                "profit_buffer_pct": 2,
                "uptime_seconds": 3600,
                "manual_stop_active": False,
                "strategies_count": 2,
                "dynamic_strategies_count": 1,
                "trading_mode": "paper_local",
                "exchange_name": "MEXC",
                "exchange_url": "",
                "tier": "building",
                "tier_progress_pct": 50,
                "daily_target_pct": 10,
            },
            "positions": [{"symbol": "BTC/USDT", "side": "long", "pnl": 10}],
            "wick_scalps": [{"symbol": "ETH/USDT"}],
            "strategies": [{"name": "rsi", "symbol": "BTC/USDT"}],
        }
        r = await client.post("/internal/report", json=payload)
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}
        assert "momentum" in _bot_reports

    async def test_post_report_no_bot_id_ignored(self, client):
        r = await client.post("/internal/report", json={"exchange": "MEXC"})
        assert r.status_code == 200
        assert len(_bot_reports) == 0

    async def test_merged_snapshot_aggregates_bots(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        _bot_reports.clear()
        for bid, bal in [("a", 500), ("b", 700)]:
            report_bot_snapshot(
                {
                    "bot_id": bid,
                    "exchange": "MEXC",
                    "status": {
                        "running": True,
                        "balance": bal,
                        "available_margin": bal / 2,
                        "daily_pnl": 10,
                        "daily_pnl_pct": 1,
                        "total_growth_usd": 20,
                        "total_growth_pct": 2,
                        "profit_buffer_pct": 1,
                        "uptime_seconds": 60,
                        "manual_stop_active": False,
                        "strategies_count": 1,
                        "dynamic_strategies_count": 0,
                        "trading_mode": "paper_local",
                        "exchange_name": "MEXC",
                        "exchange_url": "",
                        "tier": "building",
                        "tier_progress_pct": 50,
                        "daily_target_pct": 10,
                    },
                    "positions": [{"symbol": "BTC/USDT", "side": "long", "pnl": 5}],
                    "wick_scalps": [],
                    "strategies": [],
                }
            )
        from web.server import _build_merged_snapshot

        snap = _build_merged_snapshot()
        assert snap["status"]["balance"] == 1200
        assert snap["status"]["running"] is True
        assert len(snap["positions"]) == 2
        assert len(snap["bots"]) == 2
        _bot_reports.clear()


# ── GET /api/modules, /api/daily-report, /api/analytics ─────────────────


class TestGetModulesDailyReportAnalytics:
    async def test_modules_no_bot(self, client):
        set_bot(None)  # type: ignore[arg-type]
        r = await client.get("/api/modules")
        assert r.status_code == 200
        assert r.json() == []

    async def test_modules_with_bot(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        mock_bot.intel = MagicMock()
        mock_bot.intel.condition = MagicMock(regime=MagicMock(value="normal"))
        r = await client.get("/api/modules")
        assert r.status_code == 200
        data = r.json()
        assert len(data) >= 4
        names = [m["name"] for m in data]
        assert "intel" in names
        assert "scanner" in names
        assert "news" in names
        assert "volatility" in names

    async def test_daily_report_no_bot(self, client):
        set_bot(None)  # type: ignore[arg-type]
        r = await client.get("/api/daily-report")
        assert r.status_code == 200
        data = r.json()
        assert data["history"] == []
        assert "compound_report" in data

    async def test_daily_report_with_bot(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        r = await client.get("/api/daily-report")
        assert r.status_code == 200
        data = r.json()
        assert data["winning_days"] == 3
        assert data["projected"] == {"1_week": 10_500.0, "1_month": 11_000.0, "3_months": 12_000.0}

    async def test_analytics_no_bot(self, client):
        set_bot(None)  # type: ignore[arg-type]
        r = await client.get("/api/analytics")
        assert r.status_code == 200
        data = r.json()
        assert data["total_trades_logged"] == 0
        assert data["strategy_scores"] == []

    async def test_analytics_with_bot(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        r = await client.get("/api/analytics")
        assert r.status_code == 200
        data = r.json()
        assert "hourly_performance" in data
        assert "regime_performance" in data


# ── POST /api/analytics/refresh ────────────────────────────────────────


class TestRefreshAnalytics:
    async def test_refresh_no_bot(self, client):
        set_bot(None)  # type: ignore[arg-type]
        r = await client.post("/api/analytics/refresh")
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is False
        assert "not initialized" in data["message"]

    async def test_refresh_with_bot(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        mock_bot.analytics.scores = {"rsi": MagicMock()}
        r = await client.post("/api/analytics/refresh")
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is True
        mock_bot.analytics.refresh.assert_called_once()


# ── POST /api/bot/start, /api/bot/stop ──────────────────────────────────


class TestBotStartStop:
    async def test_start_no_bot(self, client):
        set_bot(None)  # type: ignore[arg-type]
        r = await client.post("/api/bot/start")
        assert r.status_code == 200
        assert r.json()["success"] is False

    async def test_start_already_running(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        mock_bot._running = True
        mock_bot.settings.bot_id = "testbot"
        r = await client.post("/api/bot/start", json={"bot_id": "testbot"})
        assert r.status_code == 200
        assert r.json()["success"] is False
        assert "already running" in r.json()["message"]

    async def test_start_ok(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        mock_bot._running = False
        r = await client.post("/api/bot/start")
        assert r.status_code == 200
        assert r.json()["success"] is True

    async def test_stop_no_bot(self, client):
        set_bot(None)  # type: ignore[arg-type]
        r = await client.post("/api/bot/stop")
        assert r.status_code == 200
        assert r.json()["success"] is False

    async def test_stop_not_running(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        mock_bot._running = False
        mock_bot.settings.bot_id = "testbot"
        r = await client.post("/api/bot/stop", json={"bot_id": "testbot"})
        assert r.status_code == 200
        assert "already stopped" in r.json()["message"]

    async def test_stop_ok(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        mock_bot._running = True
        mock_bot.stop = AsyncMock()
        r = await client.post("/api/bot/stop")
        assert r.status_code == 200
        assert r.json()["success"] is True
        mock_bot.stop.assert_awaited_once()


# ── POST /api/position/close, take-profit, tighten-stop (body: symbol, pct) ─


class TestPositionActions:
    async def test_close_position_no_bot(self, client):
        set_bot(None)  # type: ignore[arg-type]
        r = await client.post("/api/position/close", json={"symbol": "BTCUSDT"})
        assert r.status_code == 200
        assert r.json()["success"] is False

    async def test_close_position_ok(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        mock_bot.orders.execute_signal = AsyncMock()
        r = await client.post("/api/position/close", json={"symbol": "BTCUSDT"})
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is True
        assert "Closed" in data["message"]
        mock_bot.orders.execute_signal.assert_awaited_once()

    async def test_close_position_with_slash_in_symbol(self, client, mock_bot):
        """Symbols like BTC/USDT work when sent in request body (no path encoding issue)."""
        set_bot(mock_bot)  # type: ignore[arg-type]
        mock_bot.orders.execute_signal = AsyncMock()
        r = await client.post("/api/position/close", json={"symbol": "BTC/USDT"})
        assert r.status_code == 200
        assert r.json()["success"] is True
        mock_bot.orders.execute_signal.assert_awaited_once()

    async def test_close_position_execute_fails(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        mock_bot.orders.execute_signal = AsyncMock(side_effect=Exception("Exchange error"))
        r = await client.post("/api/position/close", json={"symbol": "BTCUSDT"})
        assert r.status_code == 200
        assert r.json()["success"] is False
        assert "Exchange error" in r.json()["message"]

    async def test_take_profit_no_bot(self, client):
        set_bot(None)  # type: ignore[arg-type]
        r = await client.post("/api/position/take-profit", json={"symbol": "BTCUSDT", "pct": 50})
        assert r.status_code == 200
        assert r.json()["success"] is False

    async def test_take_profit_no_position(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        mock_bot.exchange.fetch_positions = AsyncMock(return_value=[])
        r = await client.post("/api/position/take-profit", json={"symbol": "BTCUSDT", "pct": 50})
        assert r.status_code == 200
        assert r.json()["success"] is False
        assert "No open position" in r.json()["message"]

    async def test_take_profit_ok(self, client, mock_bot):
        from core.models import OrderSide, Position

        set_bot(mock_bot)  # type: ignore[arg-type]
        pos = Position(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            amount=1.0,
            entry_price=50_000.0,
            current_price=52_000.0,
            leverage=10,
            market_type="futures",
            opened_at=datetime.now(UTC),
        )
        mock_bot.exchange.fetch_positions = AsyncMock(return_value=[pos])
        mock_bot.exchange.place_order = AsyncMock(return_value=MagicMock())
        r = await client.post("/api/position/take-profit", json={"symbol": "BTCUSDT", "pct": 50})
        assert r.status_code == 200
        assert r.json()["success"] is True

    async def test_tighten_stop_no_bot(self, client):
        set_bot(None)  # type: ignore[arg-type]
        r = await client.post("/api/position/tighten-stop", json={"symbol": "BTCUSDT", "pct": 2})
        assert r.status_code == 200
        assert r.json()["success"] is False

    async def test_tighten_stop_no_trailing_stop(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        mock_bot.orders.trailing.active_stops = {}
        r = await client.post("/api/position/tighten-stop", json={"symbol": "BTCUSDT", "pct": 2})
        assert r.status_code == 200
        assert r.json()["success"] is False
        assert "No trailing stop" in r.json()["message"]

    async def test_tighten_stop_ok(self, client, mock_bot):
        from core.models import OrderSide, Position
        from core.orders.trailing import TrailingStop

        set_bot(mock_bot)  # type: ignore[arg-type]
        ts = TrailingStop(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            entry_price=50_000.0,
            initial_stop_pct=5.0,
            trail_pct=2.0,
            current_stop=47_500.0,
        )
        mock_bot.orders.trailing.active_stops = {"BTCUSDT": ts}
        pos = Position(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            amount=0.1,
            entry_price=50_000.0,
            current_price=52_000.0,
            leverage=10,
            market_type="futures",
            opened_at=datetime.now(UTC),
        )
        mock_bot.exchange.fetch_positions = AsyncMock(return_value=[pos])
        r = await client.post("/api/position/tighten-stop", json={"symbol": "BTCUSDT", "pct": 2})
        assert r.status_code == 200
        assert r.json()["success"] is True
        assert ts.current_stop != 47_500.0


# ── POST /api/close-all, stop-trading, resume-trading ──────────────────


class TestCloseAllStopResume:
    async def test_close_all_no_bot(self, client):
        set_bot(None)  # type: ignore[arg-type]
        r = await client.post("/api/close-all")
        assert r.status_code == 200
        assert r.json()["success"] is False

    async def test_close_all_ok(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        r = await client.post("/api/close-all")
        assert r.status_code == 200
        assert r.json()["success"] is True
        mock_bot._close_all_positions.assert_awaited_once()

    async def test_stop_trading_no_bot(self, client):
        set_bot(None)  # type: ignore[arg-type]
        r = await client.post("/api/stop-trading")
        assert r.status_code == 200
        assert r.json()["success"] is False

    async def test_stop_trading_ok(self, client, mock_bot, tmp_path):
        set_bot(mock_bot)  # type: ignore[arg-type]
        mock_bot.target.STOP_FILE = tmp_path / "STOP"
        r = await client.post("/api/stop-trading")
        assert r.status_code == 200
        assert r.json()["success"] is True
        assert mock_bot.target.STOP_FILE.exists()

    async def test_resume_trading_ok(self, client, mock_bot, tmp_path):
        set_bot(mock_bot)  # type: ignore[arg-type]
        stop_file = tmp_path / "STOP"
        stop_file.touch()
        mock_bot.target.STOP_FILE = stop_file
        r = await client.post("/api/resume-trading")
        assert r.status_code == 200
        assert r.json()["success"] is True
        assert not stop_file.exists()


# ── POST /api/module/{name}/toggle ─────────────────────────────────────


class TestToggleModule:
    async def test_toggle_no_bot(self, client):
        set_bot(None)  # type: ignore[arg-type]
        r = await client.post("/api/module/intel/toggle")
        assert r.status_code == 200
        assert r.json()["success"] is False

    async def test_toggle_unknown_module(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        r = await client.post("/api/module/unknown/toggle")
        assert r.status_code == 200
        assert r.json()["success"] is False
        assert "Unknown module" in r.json()["message"]

    async def test_toggle_news(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        assert mock_bot.settings.news_enabled is False
        r = await client.post("/api/module/news/toggle")
        assert r.status_code == 200
        assert r.json()["success"] is True
        assert mock_bot.settings.news_enabled is True

    async def test_toggle_intel_disable(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        intel_stop_mock = AsyncMock()
        intel_mock = MagicMock()
        intel_mock.stop = intel_stop_mock
        mock_bot.intel = intel_mock
        r = await client.post("/api/module/intel/toggle")
        assert r.status_code == 200
        assert r.json()["success"] is True
        assert "disabled" in r.json()["message"].lower()
        intel_stop_mock.assert_awaited_once()


# ── Reset Profit Buffer ──────────────────────────────────────────────────


class TestResetProfitBuffer:
    async def test_reset_no_bot(self, client):
        set_bot(None)  # type: ignore[arg-type]
        r = await client.post("/api/reset-profit-buffer")
        assert r.status_code == 200
        assert r.json()["success"] is False

    async def test_reset_clears_buffer(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        mock_bot.target.profit_buffer_pct = 20.0
        mock_bot.target._profit_buffer_pct = 20.0
        mock_bot.risk._base_max_daily_loss_pct = 3.0
        mock_bot.risk.max_daily_loss_pct = 13.0
        r = await client.post("/api/reset-profit-buffer")
        assert r.status_code == 200
        assert r.json()["success"] is True
        assert "20.0" in r.json()["message"]
        assert mock_bot.target._profit_buffer_pct == 0.0
        assert mock_bot.risk.max_daily_loss_pct == 3.0

    async def test_status_includes_profit_buffer(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        mock_bot.target.profit_buffer_pct = 15.5
        mock_bot.orders.scaler.active_positions = {}
        r = await client.get("/api/status")
        assert r.status_code == 200
        assert r.json()["profit_buffer_pct"] == 15.5


# ── DB Explorer ─────────────────────────────────────────────────────────


class TestDbExplorer:
    async def test_db_tables_no_bot(self, client):
        _ALLOWED_TABLES.clear()
        set_bot(None)  # type: ignore[arg-type]
        r = await client.get("/api/db/tables")
        assert r.status_code == 200
        tables = r.json()
        names = {t["name"] for t in tables}
        assert "trades" in names
        assert "deposits" in names

    async def test_db_tables_with_bot(self, client, mock_bot):
        _ALLOWED_TABLES.clear()
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, symbol TEXT)")
        conn.commit()
        mock_bot.trade_db._conn = conn
        set_bot(mock_bot)  # type: ignore[arg-type]
        r = await client.get("/api/db/tables")
        assert r.status_code == 200
        data = r.json()
        names = {t["name"] for t in data}
        assert "trades" in names
        assert "deposits" in names

    async def test_db_table_rows_empty(self, client, mock_bot):
        _ALLOWED_TABLES.clear()
        set_bot(mock_bot)  # type: ignore[arg-type]
        r = await client.get("/api/db/table/trades")
        assert r.status_code == 200
        data = r.json()
        assert "id" in data["columns"]
        assert "symbol" in data["columns"]

    async def test_db_table_rows_with_data(self, client, mock_bot):
        _ALLOWED_TABLES.clear()
        set_bot(mock_bot)  # type: ignore[arg-type]
        hub = _get_hub_db()
        for i in range(15):
            hub.insert_trade(
                "test",
                {
                    "symbol": "BTC/USDT",
                    "side": "long",
                    "strategy": "rsi",
                    "action": "open",
                    "opened_at": f"2026-02-20T{10 + i}:00:00",
                },
            )
        r = await client.get("/api/db/table/trades?page=1&page_size=10")
        assert r.status_code == 200
        data = r.json()
        assert len(data["rows"]) == 10
        assert data["total"] >= 15
        assert data["total_pages"] >= 2

    async def test_db_table_not_found(self, client, mock_bot):
        _ALLOWED_TABLES.clear()
        set_bot(mock_bot)  # type: ignore[arg-type]
        await client.get("/api/db/tables")
        r = await client.get("/api/db/table/nonexistent")
        assert r.status_code == 404

    async def test_db_table_pagination(self, client, mock_bot):
        _ALLOWED_TABLES.clear()
        set_bot(mock_bot)  # type: ignore[arg-type]
        hub = _get_hub_db()
        for i in range(25):
            hub.insert_trade(
                "test",
                {
                    "symbol": "ETH/USDT",
                    "side": "long",
                    "strategy": "macd",
                    "action": "open",
                    "opened_at": f"2026-02-20T{i}:00:00",
                },
            )
        r = await client.get("/api/db/table/trades?page=2&page_size=10")
        assert r.status_code == 200
        data = r.json()
        assert len(data["rows"]) == 10
        assert data["page"] == 2


# ── System metrics (no trading block) ───────────────────────────────────


class TestSystemMetricsNoTrading:
    """System metrics / get_metrics_json must not expose trading state."""

    def test_metrics_json_excludes_trading(self):
        from web.metrics import get_metrics_json

        data = get_metrics_json(None, 0.0)
        assert "trading" not in data
        assert "positions" not in data
        assert "system" in data
        assert "process" in data


# ── Static /docs/summary ───────────────────────────────────────────────


class TestServeSummary:
    async def test_summary_missing_returns_404(self, client):
        with patch("web.server.DOCS_DIR", Path("/nonexistent/docs_dir_404")):
            r = await client.get("/api/summary-html")
            assert r.status_code == 404
            assert "not found" in r.text.lower()


# ── Bot Registry Persistence ────────────────────────────────────────────


class TestBotRegistry:
    def test_save_and_load_registry(self, tmp_path):
        import json

        from web.server import _BOT_REGISTRY, _bot_urls, _load_bot_registry, _save_bot_registry

        original_path = _BOT_REGISTRY
        test_path = tmp_path / "bot_registry.json"
        try:
            import web.server

            web.server._BOT_REGISTRY = test_path
            _bot_urls.clear()
            _bot_urls["momentum"] = "http://bot-momentum:9035"
            _bot_urls["meanrev"] = "http://bot-meanrev:9035"
            _save_bot_registry()

            assert test_path.exists()
            saved = json.loads(test_path.read_text())
            assert saved["momentum"] == "http://bot-momentum:9035"
            assert saved["meanrev"] == "http://bot-meanrev:9035"

            _bot_urls.clear()
            assert len(_bot_urls) == 0
            _load_bot_registry()
            assert _bot_urls["momentum"] == "http://bot-momentum:9035"
            assert _bot_urls["meanrev"] == "http://bot-meanrev:9035"
        finally:
            web.server._BOT_REGISTRY = original_path
            _bot_urls.clear()

    def test_load_registry_handles_missing_file(self, tmp_path):
        import web.server
        from web.server import _BOT_REGISTRY, _bot_urls, _load_bot_registry

        original_path = _BOT_REGISTRY
        try:
            web.server._BOT_REGISTRY = tmp_path / "nonexistent.json"
            _bot_urls.clear()
            _load_bot_registry()
            assert len(_bot_urls) == 0
        finally:
            web.server._BOT_REGISTRY = original_path

    def test_load_registry_handles_corrupt_file(self, tmp_path):
        import web.server
        from web.server import _BOT_REGISTRY, _bot_urls, _load_bot_registry

        original_path = _BOT_REGISTRY
        try:
            corrupt = tmp_path / "corrupt.json"
            corrupt.write_text("{bad json")
            web.server._BOT_REGISTRY = corrupt
            _bot_urls.clear()
            _load_bot_registry()
            assert len(_bot_urls) == 0
        finally:
            web.server._BOT_REGISTRY = original_path

    def test_set_bot_registers_local_bot(self, mock_bot, tmp_path):
        import web.server
        from web.server import _bot_urls

        original_path = web.server._BOT_REGISTRY
        try:
            web.server._BOT_REGISTRY = tmp_path / "reg.json"
            _bot_urls.clear()
            mock_bot.settings.bot_id = "momentum"
            set_bot(mock_bot)  # type: ignore[arg-type]
            assert "momentum" in _bot_urls
            assert _bot_urls["momentum"] == "http://bot-momentum:9035"
        finally:
            web.server._BOT_REGISTRY = original_path
            _bot_urls.clear()


# ── Action Forwarding ───────────────────────────────────────────────────


class TestActionForwarding:
    async def test_close_forwards_to_remote_bot(self, client, mock_bot):
        from web.server import _bot_urls

        mock_bot.settings.bot_id = "momentum"
        set_bot(mock_bot)  # type: ignore[arg-type]
        _bot_urls["meanrev"] = "http://bot-meanrev:9035"
        try:
            with patch("web.server._forward_to_bot", new_callable=AsyncMock) as fwd:
                from web.schemas import ActionResponse

                fwd.return_value = ActionResponse(success=True, message="Closed remotely")
                r = await client.post("/api/position/close", json={"symbol": "ETH/USDT", "bot_id": "meanrev"})
                assert r.status_code == 200
                assert r.json()["success"] is True
                assert "remotely" in r.json()["message"]
                fwd.assert_awaited_once_with("meanrev", "/api/position/close", {"symbol": "ETH/USDT"})
        finally:
            _bot_urls.clear()

    async def test_take_profit_forwards_to_remote_bot(self, client, mock_bot):
        from web.server import _bot_urls

        mock_bot.settings.bot_id = "momentum"
        set_bot(mock_bot)  # type: ignore[arg-type]
        _bot_urls["swing"] = "http://bot-swing:9035"
        try:
            with patch("web.server._forward_to_bot", new_callable=AsyncMock) as fwd:
                from web.schemas import ActionResponse

                fwd.return_value = ActionResponse(success=True, message="Took 25%")
                r = await client.post(
                    "/api/position/take-profit", json={"symbol": "SOL/USDT", "pct": 25, "bot_id": "swing"}
                )
                assert r.status_code == 200
                assert r.json()["success"] is True
                fwd.assert_awaited_once_with("swing", "/api/position/take-profit", {"symbol": "SOL/USDT", "pct": 25})
        finally:
            _bot_urls.clear()

    async def test_tighten_forwards_to_remote_bot(self, client, mock_bot):
        from web.server import _bot_urls

        mock_bot.settings.bot_id = "momentum"
        set_bot(mock_bot)  # type: ignore[arg-type]
        _bot_urls["meanrev"] = "http://bot-meanrev:9035"
        try:
            with patch("web.server._forward_to_bot", new_callable=AsyncMock) as fwd:
                from web.schemas import ActionResponse

                fwd.return_value = ActionResponse(success=True, message="Tightened")
                r = await client.post(
                    "/api/position/tighten-stop", json={"symbol": "BTC/USDT", "pct": 3, "bot_id": "meanrev"}
                )
                assert r.status_code == 200
                assert r.json()["success"] is True
                fwd.assert_awaited_once_with("meanrev", "/api/position/tighten-stop", {"symbol": "BTC/USDT", "pct": 3})
        finally:
            _bot_urls.clear()

    async def test_local_bot_action_not_forwarded(self, client, mock_bot):
        """Actions for the local bot execute locally, not forwarded."""
        mock_bot.settings.bot_id = "momentum"
        set_bot(mock_bot)  # type: ignore[arg-type]
        mock_bot.orders.execute_signal = AsyncMock()
        r = await client.post("/api/position/close", json={"symbol": "BTC/USDT", "bot_id": "momentum"})
        assert r.status_code == 200
        assert r.json()["success"] is True
        mock_bot.orders.execute_signal.assert_awaited_once()

    async def test_empty_bot_id_defaults_to_local(self, client, mock_bot):
        mock_bot.settings.bot_id = "momentum"
        set_bot(mock_bot)  # type: ignore[arg-type]
        mock_bot.orders.execute_signal = AsyncMock()
        r = await client.post("/api/position/close", json={"symbol": "BTC/USDT", "bot_id": ""})
        assert r.status_code == 200
        assert r.json()["success"] is True
        mock_bot.orders.execute_signal.assert_awaited_once()


# ── Broadcast Actions ───────────────────────────────────────────────────


class TestBroadcastActions:
    async def test_close_all_broadcasts_to_remote_bots(self, client, mock_bot):
        from web.server import _bot_urls

        mock_bot.settings.bot_id = "momentum"
        set_bot(mock_bot)  # type: ignore[arg-type]
        _bot_urls["meanrev"] = "http://bot-meanrev:9035"
        _bot_urls["momentum"] = "http://bot-momentum:9035"
        try:
            with patch("web.server._forward_to_bot", new_callable=AsyncMock) as fwd:
                from web.schemas import ActionResponse

                fwd.return_value = ActionResponse(success=True, message="ok")
                r = await client.post("/api/close-all")
                assert r.status_code == 200
                assert r.json()["success"] is True
                mock_bot._close_all_positions.assert_awaited_once()
                fwd.assert_awaited_once_with("meanrev", "/api/close-all", {})
        finally:
            _bot_urls.clear()

    async def test_stop_trading_broadcasts(self, client, mock_bot):
        from web.server import _bot_urls

        mock_bot.settings.bot_id = "momentum"
        set_bot(mock_bot)  # type: ignore[arg-type]
        _bot_urls["swing"] = "http://bot-swing:9035"
        _bot_urls["momentum"] = "http://bot-momentum:9035"
        try:
            with patch("web.server._forward_to_bot", new_callable=AsyncMock) as fwd:
                from web.schemas import ActionResponse

                fwd.return_value = ActionResponse(success=True, message="ok")
                r = await client.post("/api/stop-trading")
                assert r.status_code == 200
                fwd.assert_awaited_once_with("swing", "/api/stop-trading", {})
        finally:
            _bot_urls.clear()
            mock_bot.target.STOP_FILE.unlink(missing_ok=True)


# ── Trade Queue Lifecycle Statuses ──────────────────────────────────────


class TestTradeQueueLifecycle:
    async def test_trade_queue_shows_only_pending(self, client):
        from shared.models import SignalPriority, TradeProposal, TradeQueue

        with patch("web.server.SharedState") as MockSharedState:
            mock_state = MagicMock()
            q = TradeQueue()
            now = datetime.now(UTC).isoformat()
            q.daily = [
                TradeProposal(
                    priority=SignalPriority.DAILY,
                    symbol="BTC/USDT",
                    side="long",
                    strategy="mom",
                    strength=0.8,
                    created_at=now,
                    max_age_seconds=14400,
                ),
                TradeProposal(
                    priority=SignalPriority.DAILY,
                    symbol="ETH/USDT",
                    side="short",
                    strategy="fade",
                    strength=0.6,
                    created_at=now,
                    consumed=True,
                    consumed_at=now,
                ),
                TradeProposal(
                    priority=SignalPriority.DAILY,
                    symbol="SOL/USDT",
                    side="long",
                    strategy="intel",
                    strength=0.5,
                    created_at=now,
                    rejected=True,
                    reject_reason="size too big",
                ),
            ]
            mock_state.read_trade_queue.return_value = q
            mock_data_dir = MagicMock()
            mock_data_dir.iterdir.return_value = iter([])
            mock_state._data_dir = mock_data_dir
            MockSharedState.return_value = mock_state
            r = await client.get("/api/trade-queue")

        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["symbol"] == "BTC/USDT"
        assert data[0]["status"] == "pending"
        assert not any(d["symbol"] == "SOL/USDT" for d in data)
        assert not any(d["symbol"] == "ETH/USDT" for d in data)


# ── Analytics with Multibot Positions ───────────────────────────────────


class TestAnalyticsMultibot:
    async def test_analytics_aggregates_positions_from_reports(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        _bot_reports["meanrev"] = {
            "bot_id": "meanrev",
            "positions": [
                {
                    "symbol": "ETH/USDT",
                    "side": "buy",
                    "entry_price": 3000.0,
                    "current_price": 3100.0,
                    "pnl_pct": 3.33,
                    "pnl_usd": 33.0,
                    "notional_value": 1000.0,
                    "leverage": 10,
                    "strategy": "bollinger",
                    "age_minutes": 15,
                    "dca_count": 0,
                },
            ],
        }
        try:
            r = await client.get("/api/analytics")
            assert r.status_code == 200
            data = r.json()
            positions = data.get("live_positions", [])
            assert len(positions) >= 1
            eth_pos = next((p for p in positions if p["symbol"] == "ETH/USDT"), None)
            assert eth_pos is not None
            assert eth_pos["side"] == "long"
            assert eth_pos["pnl_pct"] == 3.33
        finally:
            _bot_reports.clear()


# ── Closed Trades Endpoint ──────────────────────────────────────────────


class TestClosedTrades:
    async def test_closed_trades_no_bot(self, client):
        set_bot(None)  # type: ignore[arg-type]
        r = await client.get("/api/closed-trades")
        assert r.status_code == 200
        assert r.json() == []

    async def test_closed_trades_from_hub_db(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        hub = _get_hub_db()
        hub.insert_trade(
            "test_ct",
            {
                "symbol": "SOL/USDT",
                "side": "long",
                "strategy": "momentum",
                "action": "close",
                "entry_price": 100.0,
                "exit_price": 110.0,
                "amount": 1.0,
                "pnl_usd": 10.0,
                "pnl_pct": 10.0,
                "is_winner": True,
                "leverage": 5,
                "opened_at": "2026-02-20T10:00:00",
                "closed_at": "2026-02-20T11:00:00",
            },
        )
        r = await client.get("/api/closed-trades?limit=500")
        assert r.status_code == 200
        data = r.json()
        sol_trades = [d for d in data if d["symbol"] == "SOL/USDT"]
        assert len(sol_trades) >= 1
        assert sol_trades[0]["pnl_usd"] == 10.0


# ── Module Toggle in Multibot Mode ─────────────────────────────────────


class TestModuleToggleMultibot:
    async def test_intel_toggle_rejected_in_multibot(self, client, mock_bot):
        mock_bot._multibot = True
        set_bot(mock_bot)  # type: ignore[arg-type]
        r = await client.post("/api/module/intel/toggle")
        assert r.status_code == 200
        assert r.json()["success"] is False
        assert "monitor" in r.json()["message"].lower()

    async def test_news_toggle_rejected_in_multibot(self, client, mock_bot):
        mock_bot._multibot = True
        set_bot(mock_bot)  # type: ignore[arg-type]
        r = await client.post("/api/module/news/toggle")
        assert r.status_code == 200
        assert r.json()["success"] is False
        assert "monitor" in r.json()["message"].lower()

    async def test_unknown_module_rejected(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        r = await client.post("/api/module/fakething/toggle")
        assert r.status_code == 200
        assert r.json()["success"] is False
        assert "Unknown" in r.json()["message"]


# ── Internal Report Registers Bot URL ───────────────────────────────────


class TestInternalReportRegistersUrl:
    async def test_report_registers_bot_url(self, client, tmp_path):
        import web.server
        from web.server import _bot_urls

        original_path = web.server._BOT_REGISTRY
        web.server._BOT_REGISTRY = tmp_path / "reg.json"
        _bot_urls.clear()
        try:
            r = await client.post(
                "/internal/report",
                json={"bot_id": "swing", "positions": [], "strategies": []},
            )
            assert r.status_code == 200
            assert _bot_urls.get("swing") == "http://bot-swing:9035"
        finally:
            web.server._BOT_REGISTRY = original_path
            _bot_urls.clear()

    async def test_report_without_bot_id_skipped(self, client):
        from web.server import _bot_urls

        _bot_urls.clear()
        r = await client.post("/internal/report", json={"positions": []})
        assert r.status_code == 200
        assert len(_bot_urls) == 0


# ── Merged Snapshot ─────────────────────────────────────────────────────


class TestBuildMergedSnapshot:
    async def test_merged_snapshot_aggregates_bots(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        mock_bot.intel = None
        mock_bot.shared_intel = MagicMock()
        mock_bot.shared_intel.read_intel = MagicMock(return_value=MagicMock(sources_active=[]))
        _bot_reports["m1"] = {
            "bot_id": "m1",
            "exchange": "MEXC",
            "status": {
                "balance": 1000,
                "available_margin": 500,
                "daily_pnl": 50,
                "daily_pnl_pct": 5,
                "total_growth_usd": 100,
                "total_growth_pct": 10,
                "running": True,
                "strategies_count": 10,
                "dynamic_strategies_count": 2,
                "uptime_seconds": 3600,
                "trading_mode": "paper_local",
                "exchange_name": "MEXC",
                "exchange_url": "",
                "tier": "strong",
                "tier_progress_pct": 50,
                "daily_target_pct": 10,
                "manual_stop_active": False,
                "profit_buffer_pct": 2.0,
            },
            "positions": [
                {
                    "symbol": "BTC/USDT",
                    "side": "buy",
                    "entry_price": 50000,
                    "current_price": 51000,
                    "pnl_pct": 2,
                    "pnl_usd": 20,
                    "notional_value": 1000,
                    "leverage": 10,
                    "strategy": "rsi",
                    "age_minutes": 5,
                    "dca_count": 0,
                }
            ],
            "wick_scalps": [
                {
                    "symbol": "ETH/USDT",
                    "scalp_side": "buy",
                    "entry_price": 3000,
                    "amount": 0.1,
                    "age_minutes": 2,
                    "max_hold_minutes": 30,
                }
            ],
            "strategies": [],
        }
        _bot_reports["m2"] = {
            "bot_id": "m2",
            "exchange": "Binance",
            "status": {
                "balance": 2000,
                "available_margin": 1000,
                "daily_pnl": 100,
                "daily_pnl_pct": 5,
                "total_growth_usd": 200,
                "total_growth_pct": 10,
                "running": True,
                "strategies_count": 8,
                "dynamic_strategies_count": 1,
                "uptime_seconds": 7200,
                "trading_mode": "paper_local",
                "exchange_name": "Binance",
                "exchange_url": "",
                "tier": "building",
                "tier_progress_pct": 30,
                "daily_target_pct": 10,
                "manual_stop_active": False,
                "profit_buffer_pct": 1.5,
            },
            "positions": [],
            "wick_scalps": [],
            "strategies": [],
        }
        try:
            from web.server import _build_merged_snapshot

            snap = _build_merged_snapshot()
            assert snap["status"]["balance"] == 3000
            assert snap["status"]["running"] is True
            assert snap["status"]["strategies_count"] == 18
            assert len(snap["positions"]) == 1
            assert snap["positions"][0]["bot_id"] == "m1"
            assert len(snap["wick_scalps"]) == 1
            assert len(snap["bots"]) == 2
        finally:
            _bot_reports.clear()


# ── Wick Scalps Endpoint ────────────────────────────────────────────────


class TestWickScalps:
    def test_wick_scalps_helper_returns_data(self, mock_bot):
        from web.server import _wick_scalps

        set_bot(mock_bot)  # type: ignore[arg-type]
        ws = MagicMock()
        ws.scalp_side = "buy"
        ws.entry_price = 50000
        ws.amount = 0.01
        ws.age_minutes = 5
        ws.max_hold_minutes = 30
        mock_bot.orders.wick_scalper.active_scalps = {"BTC/USDT": ws}
        result = _wick_scalps()
        assert len(result) == 1
        assert result[0].symbol == "BTC/USDT"
        assert result[0].scalp_side == "buy"
        assert result[0].entry_price == 50000

    def test_wick_scalps_no_bot(self):
        from web.server import _wick_scalps

        set_bot(None)  # type: ignore[arg-type]
        result = _wick_scalps()
        assert result == []


# ── News Endpoint ───────────────────────────────────────────────────────


class TestNewsEndpoint:
    async def test_news_no_bot(self, client):
        set_bot(None)  # type: ignore[arg-type]
        r = await client.get("/api/news")
        assert r.status_code == 200
        assert r.json() == []

    async def test_news_with_items(self, client, mock_bot):
        from datetime import UTC, datetime

        from news.monitor import NewsItem

        set_bot(mock_bot)  # type: ignore[arg-type]
        mock_bot._recent_news = [
            NewsItem(
                headline="BTC surges",
                source="coindesk",
                url="https://example.com/1",
                published=datetime.now(UTC),
                matched_symbols=["BTC/USDT"],
                sentiment="bullish",
                sentiment_score=0.8,
            ),
        ]
        r = await client.get("/api/news")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["headline"] == "BTC surges"
        assert data[0]["sentiment"] == "bullish"


# ── Grafana URL ─────────────────────────────────────────────────────────


class TestGrafanaUrl:
    async def test_grafana_url(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        mock_bot.settings.grafana_port = 3001
        r = await client.get("/api/grafana-url")
        assert r.status_code == 200
        data = r.json()
        assert data["port"] == 3001
        assert "dashboard_uid" in data


# ── System Metrics & Prometheus ─────────────────────────────────────────


class TestSystemMetricsAndPrometheus:
    async def test_system_metrics_returns_json(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        with patch("web.metrics.get_metrics_json", return_value={"cpu": 10, "memory": 50}):
            r = await client.get("/api/system-metrics")
        assert r.status_code == 200
        assert r.json()["cpu"] == 10

    async def test_prometheus_metrics_returns_text(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        with patch("web.metrics.collect_metrics", return_value="bot_uptime 100\n"):
            r = await client.get("/metrics")
        assert r.status_code == 200
        assert "bot_uptime" in r.text


# ── Analytics Fallback (local positions) ────────────────────────────────


class TestAnalyticsFallbackPositions:
    async def test_analytics_local_fallback_when_no_reports(self, client, mock_bot):
        from core.models import OrderSide, Position
        from core.orders.scaler import ScaledPosition

        set_bot(mock_bot)  # type: ignore[arg-type]
        _bot_reports.clear()
        sp = MagicMock(spec=ScaledPosition)
        sp.side = "long"
        sp.strategy = "rsi"
        sp.avg_entry_price = 50000.0
        sp.last_add_price = 50000.0
        sp.current_size = 0.01
        sp.current_leverage = 10
        sp.adds = 0
        sp.opened_at = 0
        mock_bot.orders.scaler.active_positions = {"BTC/USDT": sp}
        mock_bot.exchange.fetch_positions = AsyncMock(
            return_value=[
                Position(
                    symbol="BTC/USDT",
                    side=OrderSide.BUY,
                    amount=0.01,
                    entry_price=50000.0,
                    current_price=51000.0,
                    leverage=10,
                    market_type="futures",
                    opened_at=datetime.now(UTC),
                ),
            ]
        )
        r = await client.get("/api/analytics")
        assert r.status_code == 200
        data = r.json()
        positions = data.get("live_positions", [])
        assert len(positions) >= 1
        btc = next((p for p in positions if p["symbol"] == "BTC/USDT"), None)
        assert btc is not None
        assert btc["side"] == "long"


# ── Resume Trading ──────────────────────────────────────────────────────


class TestResumeTrading:
    async def test_resume_trading(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        mock_bot.target.STOP_FILE = Path("/tmp/test_stop_file")
        mock_bot.target.STOP_FILE.write_text("stopped")
        try:
            r = await client.post("/api/resume-trading")
            assert r.status_code == 200
            assert r.json()["success"] is True
            assert mock_bot.target.manual_stop is False
        finally:
            mock_bot.target.STOP_FILE.unlink(missing_ok=True)


# ── Hub push endpoints ──────────────────────────────────────────────────


class TestHubPushEndpoints:
    """Test /internal/trade and /internal/deposit endpoints."""

    async def test_push_trade_open(self, client):
        r = await client.post(
            "/internal/trade",
            json={
                "bot_id": "momentum",
                "action": "open",
                "trade": {
                    "symbol": "BTC/USDT",
                    "side": "long",
                    "strategy": "rsi",
                    "action": "open",
                    "entry_price": 50000,
                    "amount": 0.01,
                    "leverage": 10,
                    "opened_at": "2026-02-20T10:00:00",
                },
            },
        )
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    async def test_push_trade_close_updates_open_row(self, client):
        await client.post(
            "/internal/trade",
            json={
                "bot_id": "meanrev",
                "action": "open",
                "trade": {
                    "symbol": "ETH/USDT",
                    "side": "long",
                    "strategy": "bollinger",
                    "action": "open",
                    "entry_price": 3000,
                    "amount": 0.5,
                    "leverage": 5,
                    "opened_at": "2026-02-20T11:00:00",
                },
            },
        )
        r = await client.post(
            "/internal/trade",
            json={
                "bot_id": "meanrev",
                "action": "close",
                "trade": {
                    "symbol": "ETH/USDT",
                    "side": "long",
                    "strategy": "bollinger",
                    "action": "close",
                    "entry_price": 3000,
                    "exit_price": 3100,
                    "amount": 0.5,
                    "leverage": 5,
                    "pnl_usd": 50,
                    "pnl_pct": 3.33,
                    "is_winner": True,
                    "hold_minutes": 120,
                    "opened_at": "2026-02-20T11:00:00",
                    "closed_at": "2026-02-20T13:00:00",
                },
            },
        )
        assert r.status_code == 200
        assert r.json()["action"] == "close"

    async def test_push_deposit(self, client):
        r = await client.post(
            "/internal/deposit",
            json={
                "bot_id": "momentum",
                "amount": 500.0,
                "exchange": "mexc",
                "detected_at": "2026-02-20T14:00:00",
                "balance_before": 1000.0,
                "balance_after": 1500.0,
            },
        )
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        assert r.json()["deposit_id"] > 0

    async def test_get_deposits(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        await client.post(
            "/internal/deposit",
            json={
                "bot_id": "test",
                "amount": 250.0,
                "exchange": "binance",
                "detected_at": "2026-02-20T15:00:00",
                "balance_before": 500.0,
                "balance_after": 750.0,
            },
        )
        r = await client.get("/api/deposits")
        assert r.status_code == 200
        data = r.json()
        assert len(data) >= 1
        assert any(d["amount"] == 250.0 for d in data)

    async def test_push_trade_missing_bot_id(self, client):
        r = await client.post(
            "/internal/trade",
            json={"action": "open", "trade": {"symbol": "X"}},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "error"
