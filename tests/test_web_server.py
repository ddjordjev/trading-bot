"""Tests for web/server.py — REST and action endpoints."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from web.server import _ALLOWED_TABLES, app, set_bot

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
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


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
    async def test_bots_from_data_dirs(self, client, mock_bot, tmp_path):
        set_bot(mock_bot)  # type: ignore[arg-type]
        data_dir = tmp_path / "data"
        momentum_dir = data_dir / "momentum"
        momentum_dir.mkdir(parents=True)
        (momentum_dir / "bot_status.json").write_text(
            '{"bot_id":"momentum","bot_style":"momentum","exchange":"MEXC","level":"hunting"}'
        )
        with patch("web.server.Path", return_value=data_dir):
            r = await client.get("/api/bots")
        assert r.status_code == 200
        bots = r.json()
        assert len(bots) >= 1
        bot0 = bots[0]
        assert bot0["bot_id"] == "momentum"
        assert bot0["exchange"] == "MEXC"

    async def test_bots_fallback_to_current_bot(self, client, mock_bot, tmp_path):
        mock_bot.settings.bot_id = "solo"
        mock_bot.settings.dashboard_port = 9035
        mock_bot.settings.bot_strategy_list = ["rsi"]
        set_bot(mock_bot)  # type: ignore[arg-type]
        empty_dir = tmp_path / "data_empty"
        empty_dir.mkdir()
        with patch("web.server.Path", return_value=empty_dir):
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
        strat = MagicMock()
        strat.name = "rsi"
        strat.symbol = "BTC/USDT"
        strat.market_type = "futures"
        strat.leverage = 10
        mock_bot._strategies = [strat]
        r = await client.get("/api/strategies")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["name"] == "rsi"
        assert data[0]["is_dynamic"] is False


class TestGetStrategiesNoneStats:
    """Strategies endpoint when get_strategy_stats returns None values (no 500)."""

    async def test_strategies_none_stats_returns_zero(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        strat = MagicMock()
        strat.name = "rsi"
        strat.symbol = "BTC/USDT"
        strat.market_type = "futures"
        strat.leverage = 10
        mock_bot._strategies = [strat]
        mock_bot.trade_db.get_strategy_stats = MagicMock(return_value={"total": None, "winners": None, "losers": None})
        mock_bot.orders.scaler.active_positions = {}
        r = await client.get("/api/strategies")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["applied_count"] == 0
        assert data[0]["success_count"] == 0
        assert data[0]["fail_count"] == 0


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
        r = await client.post("/api/bot/start")
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
        r = await client.post("/api/bot/stop")
        assert r.status_code == 200
        assert r.json()["success"] is False

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
        assert r.json() == []

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
        assert len(data) == 1
        assert data[0]["name"] == "trades"
        assert data[0]["row_count"] == 0

    async def test_db_table_rows_empty(self, client, mock_bot):
        _ALLOWED_TABLES.clear()
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, symbol TEXT)")
        conn.commit()
        mock_bot.trade_db._conn = conn
        set_bot(mock_bot)  # type: ignore[arg-type]
        r = await client.get("/api/db/table/trades")
        assert r.status_code == 200
        data = r.json()
        assert "id" in data["columns"]
        assert "symbol" in data["columns"]
        assert data["rows"] == []
        assert data["total"] == 0

    async def test_db_table_rows_with_data(self, client, mock_bot):
        _ALLOWED_TABLES.clear()
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, symbol TEXT)")
        for i in range(15):
            conn.execute("INSERT INTO trades (id, symbol) VALUES (?, ?)", (i + 1, "BTC"))
        conn.commit()
        mock_bot.trade_db._conn = conn
        set_bot(mock_bot)  # type: ignore[arg-type]
        r = await client.get("/api/db/table/trades?page=1&page_size=10")
        assert r.status_code == 200
        data = r.json()
        assert len(data["rows"]) == 10
        assert data["total"] == 15
        assert data["total_pages"] == 2

    async def test_db_table_not_found(self, client, mock_bot):
        _ALLOWED_TABLES.clear()
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, symbol TEXT)")
        conn.commit()
        mock_bot.trade_db._conn = conn
        set_bot(mock_bot)  # type: ignore[arg-type]
        # Populate _ALLOWED_TABLES so "trades" exists but "nonexistent" does not
        await client.get("/api/db/tables")
        r = await client.get("/api/db/table/nonexistent")
        assert r.status_code == 404

    async def test_db_table_pagination(self, client, mock_bot):
        _ALLOWED_TABLES.clear()
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, symbol TEXT)")
        for i in range(25):
            conn.execute("INSERT INTO trades (id, symbol) VALUES (?, ?)", (i + 1, "BTC"))
        conn.commit()
        mock_bot.trade_db._conn = conn
        set_bot(mock_bot)  # type: ignore[arg-type]
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
            r = await client.get("/docs/summary")
            assert r.status_code == 404
            assert "not found" in r.text.lower()
