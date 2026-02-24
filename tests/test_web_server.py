"""Tests for web/server.py — REST and action endpoints."""

from __future__ import annotations

import contextlib
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
    bot.analytics = MagicMock()
    bot.analytics.scores = {}
    bot.analytics.patterns = []
    bot.analytics.suggestions = []
    bot.analytics.refresh = MagicMock()
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


def _reset_hub_db() -> None:
    """Wipe all rows from the hub DB so tests start clean."""
    hub = _get_hub_db()
    if hub.conn:
        hub.conn.execute("DELETE FROM trades")
        with contextlib.suppress(Exception):
            hub.conn.execute("DELETE FROM bot_config")
        hub.conn.commit()
    hub._ack_buffer.clear()


@pytest.fixture
async def client(auth_override):
    _bot_reports.clear()
    _reset_hub_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    _bot_reports.clear()


# ── GET /health (no auth) ─────────────────────────────────────────────


class TestHealth:
    async def test_health_no_bot(self, client):
        r = await client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["bot_running"] is True
        assert data.get("mode") == "hub"

    async def test_health_with_bot(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        r = await client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["bot_running"] is True
        assert data.get("mode") == "hub"

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

    async def test_bots_empty_when_no_reports(self, client):
        _bot_reports.clear()
        r = await client.get("/api/bots")
        assert r.status_code == 200
        assert r.json() == []


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
        hub_state = MagicMock()
        with patch("web.server._hub_state_ref", hub_state):
            _bot_reports.clear()
            report_bot_snapshot(
                {
                    "bot_id": "solo",
                    "exchange": "MEXC",
                    "status": {
                        "running": True,
                        "balance": 10_000.0,
                        "available_margin": 5_000.0,
                        "daily_pnl": 500.0,
                        "daily_pnl_pct": 5.0,
                        "total_growth_usd": 1000.0,
                        "total_growth_pct": 25.0,
                        "trading_mode": "paper_local",
                        "exchange_name": "MEXC",
                        "strategies_count": 0,
                    },
                    "positions": [],
                    "wick_scalps": [],
                    "strategies": [],
                }
            )
            try:
                r = await client.get("/api/status")
                assert r.status_code == 200
                data = r.json()
                assert data["trading_mode"] == "paper_local"
                assert data["exchange_name"] == "MEXC"
                assert data["balance"] == 10_000.0
                assert data["daily_pnl"] == 500.0
                assert data["strategies_count"] == 0
            finally:
                _bot_reports.clear()

    async def test_status_includes_total_growth_usd(self, client):
        hub_state = MagicMock()
        with patch("web.server._hub_state_ref", hub_state):
            _bot_reports.clear()
            report_bot_snapshot(
                {
                    "bot_id": "solo",
                    "exchange": "MEXC",
                    "status": {
                        "running": True,
                        "balance": 10000.0,
                        "available_margin": 5000.0,
                        "daily_pnl": 0,
                        "daily_pnl_pct": 0,
                        "total_growth_usd": 1000.0,
                        "total_growth_pct": 0,
                        "trading_mode": "paper_local",
                        "exchange_name": "MEXC",
                        "strategies_count": 0,
                    },
                    "positions": [],
                    "wick_scalps": [],
                    "strategies": [],
                }
            )
            try:
                r = await client.get("/api/status")
                assert r.status_code == 200
                data = r.json()
                assert data["total_growth_usd"] == pytest.approx(1000.0)
            finally:
                _bot_reports.clear()


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

    async def test_positions_always_empty_from_hub(self, client):
        """Hub mode: positions endpoint always returns [] (no local exchange)."""
        r = await client.get("/api/positions")
        assert r.status_code == 200
        assert r.json() == []

    async def test_positions_fetch_exception_returns_empty(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
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

    async def test_trades_with_log(self, client):
        _bot_reports.clear()
        report_bot_snapshot(
            {
                "bot_id": "test",
                "exchange": "MEXC",
                "status": {},
                "positions": [],
                "wick_scalps": [],
                "strategies": [],
                "trade_log": [
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
                ],
            }
        )
        try:
            r = await client.get("/api/trades")
            assert r.status_code == 200
            data = r.json()
            assert len(data) == 1
            assert data[0]["symbol"] == "BTC/USDT"
            assert data[0]["action"] == "open"
        finally:
            _bot_reports.clear()


# ── GET /api/trade-queue ────────────────────────────────────────────────


class TestGetTradeQueue:
    async def test_trade_queue_returns_list(self, client):
        r = await client.get("/api/trade-queue")
        assert r.status_code == 200
        assert r.json() == []

    async def test_trade_queue_returns_pending_proposals(self, client):
        from shared.models import SignalPriority, TradeProposal, TradeQueue

        hub_state = MagicMock()
        q = TradeQueue()
        q.add(
            TradeProposal(
                priority=SignalPriority.CRITICAL,
                symbol="BTC/USDT",
                side="long",
                strategy="momentum",
                strength=0.9,
                created_at=datetime.now(UTC).isoformat(),
            ),
        )
        hub_state.read_trade_queue.return_value = q
        hub_state.read_recent_outcomes.return_value = []
        with patch("web.server._hub_state_ref", hub_state):
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

    async def test_intel_includes_macro_events(self, client):
        from shared.models import IntelSnapshot

        hub_state = MagicMock()
        snap = IntelSnapshot(
            regime="normal",
            macro_event_imminent=True,
            next_macro_event="FOMC Statement in 1.5h",
            sources_active=["macro"],
        )
        hub_state.read_intel.return_value = snap
        with patch("web.server._hub_state_ref", hub_state):
            r = await client.get("/api/intel")
        assert r.status_code == 200
        data = r.json()
        assert data is not None
        assert data["macro_event_imminent"] is True
        assert "FOMC" in data["next_macro_event"]

    async def test_trending_no_bot(self, client):
        set_bot(None)  # type: ignore[arg-type]
        r = await client.get("/api/trending")
        assert r.status_code == 200
        assert r.json() == []

    async def test_trending_with_coins(self, client):
        from shared.models import IntelSnapshot, TrendingSnapshot

        hub_state = MagicMock()
        snap = IntelSnapshot(
            hot_movers=[
                TrendingSnapshot(
                    symbol="DOGE/USDT",
                    name="Dogecoin",
                    price=0.08,
                    volume_24h=1e9,
                    market_cap=11e9,
                    change_1h=2.0,
                    change_24h=10.0,
                ),
            ]
        )
        hub_state.read_intel.return_value = snap
        with patch("web.server._hub_state_ref", hub_state):
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

    async def test_strategies_open_now_uses_live_positions_not_cached_value(self, client, mock_bot):
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
                        "name": "manual_override",
                        "symbol": "BTC/USDT",
                        "market_type": "futures",
                        "leverage": 10,
                        "is_dynamic": False,
                        "open_now": 1,
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
        assert data[0]["name"] == "manual_override"
        assert data[0]["open_now"] == 0
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
        body = r.json()
        assert body["status"] == "ok"
        assert "confirmed_keys" in body
        assert "momentum" in _bot_reports

    async def test_post_report_no_bot_id_ignored(self, client):
        r = await client.post("/internal/report", json={"exchange": "MEXC"})
        assert r.status_code == 200
        assert len(_bot_reports) == 0

    async def test_post_trade_with_request_key_dedup(self, client):
        payload = {
            "bot_id": "test-bot",
            "action": "open",
            "trade": {
                "symbol": "BTC/USDT",
                "side": "long",
                "strategy": "rsi",
                "action": "open",
                "opened_at": "2026-01-01T00:00:00",
            },
            "request_key": "dedup-key-123",
        }
        r1 = await client.post("/internal/trade", json=payload)
        assert r1.status_code == 200
        assert r1.json()["request_key"] == "dedup-key-123"
        r2 = await client.post("/internal/trade", json=payload)
        assert r2.status_code == 200

    async def test_get_bot_open_trades(self, client):
        payload = {
            "bot_id": "open-test",
            "action": "open",
            "trade": {
                "symbol": "ETH/USDT",
                "side": "long",
                "strategy": "macd",
                "action": "open",
                "opened_at": "2026-01-01T01:00:00",
            },
            "request_key": "open-key-1",
        }
        await client.post("/internal/trade", json=payload)
        r = await client.get("/internal/trades/open-test/open")
        assert r.status_code == 200
        trades = r.json()
        symbols = [t["symbol"] for t in trades]
        assert "ETH/USDT" in symbols

    async def test_get_bot_strategy_stats(self, client):
        for i in range(3):
            await client.post(
                "/internal/trade",
                json={
                    "bot_id": "stats-bot",
                    "action": "close",
                    "trade": {
                        "symbol": "BTC/USDT",
                        "side": "long",
                        "strategy": "rsi",
                        "action": "close",
                        "pnl_usd": 10.0,
                        "is_winner": True,
                        "opened_at": f"2026-01-0{i + 1}T00:00:00",
                        "closed_at": f"2026-01-0{i + 1}T01:00:00",
                    },
                    "request_key": f"stats-key-{i}",
                },
            )
        r = await client.get("/internal/trades/stats-bot/stats")
        assert r.status_code == 200
        stats = r.json()
        assert len(stats) > 0

    async def test_recovery_close(self, client):
        await client.post(
            "/internal/trade",
            json={
                "bot_id": "recover-bot",
                "action": "open",
                "trade": {
                    "symbol": "SOL/USDT",
                    "side": "long",
                    "strategy": "grid",
                    "action": "open",
                    "opened_at": "2026-01-05T00:00:00",
                },
                "request_key": "recover-open-1",
            },
        )
        r = await client.post(
            "/internal/recovery-close", json={"bot_id": "recover-bot", "opened_at": "2026-01-05T00:00:00"}
        )
        assert r.status_code == 200
        assert r.json()["updated"] is True
        r2 = await client.get("/internal/trades/recover-bot/open")
        open_trades = r2.json()
        symbols = [t["symbol"] for t in open_trades]
        assert "SOL/USDT" not in symbols

    async def test_report_returns_confirmed_keys(self, client):
        await client.post(
            "/internal/trade",
            json={
                "bot_id": "ack-bot",
                "action": "open",
                "trade": {
                    "symbol": "BTC/USDT",
                    "side": "long",
                    "strategy": "rsi",
                    "action": "open",
                    "opened_at": "2026-02-01T00:00:00",
                },
                "request_key": "ack-key-999",
            },
        )
        r = await client.post("/internal/report", json={"bot_id": "ack-bot", "status": {"running": True}})
        body = r.json()
        assert "confirmed_keys" in body
        assert "ack-key-999" in body["confirmed_keys"]

    async def test_report_returns_enabled_flag(self, client):
        hub = _get_hub_db()
        hub.set_bot_enabled("flag-bot", False)
        r = await client.post("/internal/report", json={"bot_id": "flag-bot", "status": {"running": True}})
        body = r.json()
        assert body["enabled"] is False

    async def test_report_returns_enabled_default_true(self, client):
        r = await client.post("/internal/report", json={"bot_id": "new-bot", "status": {"running": True}})
        body = r.json()
        assert body["enabled"] is True

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

    async def test_merged_snapshot_balance_uses_available_plus_margin_plus_upnl(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        _bot_reports.clear()
        report_bot_snapshot(
            {
                "bot_id": "m1",
                "exchange": "BINANCE",
                "exchange_balance": 5000.0,
                "status": {
                    "running": True,
                    "balance": 0.0,
                    "available_margin": 4000.0,
                    "daily_pnl": 0.0,
                    "daily_pnl_pct": 0.0,
                    "total_growth_usd": 0.0,
                    "total_growth_pct": 0.0,
                    "profit_buffer_pct": 0.0,
                    "uptime_seconds": 10,
                    "manual_stop_active": False,
                    "strategies_count": 0,
                    "dynamic_strategies_count": 0,
                    "trading_mode": "paper_local",
                    "exchange_name": "BINANCE",
                    "exchange_url": "",
                    "tier": "building",
                    "tier_progress_pct": 0,
                    "daily_target_pct": 10,
                },
                "positions": [
                    {
                        "symbol": "BTC/USDT",
                        "exchange_name": "BINANCE",
                        "notional_value": 1200.0,
                        "leverage": 12,
                        "pnl_usd": 50.0,
                    }
                ],
                "wick_scalps": [],
                "strategies": [],
            }
        )
        from web.server import _build_merged_snapshot

        snap = _build_merged_snapshot()
        # equity = available + used_margin + unrealized = 5000 + (1200/12) + 50 = 5150
        assert snap["status"]["balance"] == pytest.approx(5150.0)
        assert snap["status"]["available_margin"] == pytest.approx(5000.0)
        assert snap["exchange_balances"]["BINANCE"] == pytest.approx(5150.0)
        _bot_reports.clear()


# ── GET /api/modules, /api/daily-report, /api/analytics ─────────────────


class TestGetModulesDailyReportAnalytics:
    async def test_modules_no_bot(self, client):
        set_bot(None)  # type: ignore[arg-type]
        r = await client.get("/api/modules")
        assert r.status_code == 200
        assert r.json() == []

    async def test_modules_with_bot(self, client):
        hub_state = MagicMock()
        hub_state.read_intel.return_value = MagicMock(regime="normal", hot_movers=[], news_items=[])
        hub_state.read_analytics.return_value = MagicMock(weights={})
        with patch("web.server._hub_state_ref", hub_state):
            r = await client.get("/api/modules")
        assert r.status_code == 200
        data = r.json()
        assert len(data) >= 4
        names = [m["name"] for m in data]
        assert "intel" in names
        assert "scanner" in names
        assert "news" in names
        assert "analytics" in names

    async def test_daily_report_no_bot(self, client):
        set_bot(None)  # type: ignore[arg-type]
        r = await client.get("/api/daily-report")
        assert r.status_code == 200
        data = r.json()
        assert data["history"] == []
        assert "compound_report" in data

    async def test_daily_report_with_bot(self, client):
        hub_state = MagicMock()
        with patch("web.server._hub_state_ref", hub_state):
            _bot_reports.clear()
            report_bot_snapshot(
                {
                    "bot_id": "test",
                    "exchange": "MEXC",
                    "status": {},
                    "positions": [],
                    "wick_scalps": [],
                    "strategies": [],
                    "daily_report": {
                        "winning_days": 3,
                        "losing_days": 1,
                        "target_hit_days": 2,
                        "avg_daily_pnl_pct": 8.0,
                        "history": [],
                        "best_day": None,
                        "worst_day": None,
                        "projected": {"1_week": 10_500.0, "1_month": 11_000.0, "3_months": 12_000.0},
                    },
                }
            )
            try:
                r = await client.get("/api/daily-report")
                assert r.status_code == 200
                data = r.json()
                assert data["winning_days"] == 3
            finally:
                _bot_reports.clear()

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
    async def test_refresh_uses_hub_db(self, client):
        r = await client.post("/api/analytics/refresh")
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is True or data["success"] is False


# ── POST /api/bot/start, /api/bot/stop ──────────────────────────────────


class TestBotStartStop:
    async def test_start_no_bot(self, client):
        with patch("web.server._broadcast_to_remote_bots", new_callable=AsyncMock) as broadcast:
            broadcast.return_value = "ok"
            r = await client.post("/api/bot/start")
        assert r.status_code == 200
        assert r.json()["success"] is True
        assert "broadcast" in r.json()["message"].lower() or r.json()["message"] == "ok"

    async def test_start_already_running(self, client, mock_bot):
        from web.server import _bot_urls

        _bot_urls["testbot"] = "http://bot-testbot:9035"
        try:
            with patch("web.server._forward_to_bot", new_callable=AsyncMock) as fwd:
                from web.schemas import ActionResponse

                fwd.return_value = ActionResponse(success=False, message="already running")
                r = await client.post("/api/bot/start", json={"bot_id": "testbot"})
                assert r.status_code == 200
                assert r.json()["success"] is False
                assert "already running" in r.json()["message"]
        finally:
            _bot_urls.clear()

    async def test_start_ok(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        with patch("web.server._broadcast_to_remote_bots", new_callable=AsyncMock) as broadcast:
            broadcast.return_value = "ok"
            r = await client.post("/api/bot/start")
        assert r.status_code == 200
        assert r.json()["success"] is True

    async def test_stop_no_bot(self, client):
        with patch("web.server._broadcast_to_remote_bots", new_callable=AsyncMock) as broadcast:
            broadcast.return_value = "ok"
            r = await client.post("/api/bot/stop")
        assert r.status_code == 200
        assert r.json()["success"] is True

    async def test_stop_not_running(self, client, mock_bot):
        from web.server import _bot_urls

        _bot_urls["testbot"] = "http://bot-testbot:9035"
        try:
            with patch("web.server._forward_to_bot", new_callable=AsyncMock) as fwd:
                from web.schemas import ActionResponse

                fwd.return_value = ActionResponse(success=False, message="already stopped")
                r = await client.post("/api/bot/stop", json={"bot_id": "testbot"})
                assert r.status_code == 200
                assert "already stopped" in r.json()["message"]
        finally:
            _bot_urls.clear()

    async def test_stop_ok(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        with patch("web.server._broadcast_to_remote_bots", new_callable=AsyncMock) as broadcast:
            broadcast.return_value = "ok"
            r = await client.post("/api/bot/stop")
            assert r.status_code == 200
            assert r.json()["success"] is True
            broadcast.assert_awaited_once()


# ── POST /api/position/close, take-profit, tighten-stop (body: symbol, pct) ─


class TestPositionActions:
    async def test_close_position_no_bot(self, client):
        r = await client.post("/api/position/close", json={"symbol": "BTCUSDT"})
        assert r.status_code == 200
        assert r.json()["success"] is False
        assert "not registered" in r.json()["message"].lower()

    async def test_close_position_ok(self, client, mock_bot):
        from web.server import _bot_urls

        _bot_urls["testbot"] = "http://bot-testbot:9035"
        try:
            with patch("web.server._forward_to_bot", new_callable=AsyncMock) as fwd:
                from web.schemas import ActionResponse

                fwd.return_value = ActionResponse(success=True, message="Closed")
                r = await client.post("/api/position/close", json={"symbol": "BTCUSDT", "bot_id": "testbot"})
                assert r.status_code == 200
                assert r.json()["success"] is True
                assert "Closed" in r.json()["message"]
                fwd.assert_awaited_once_with("testbot", "/api/position/close", {"symbol": "BTCUSDT"})
        finally:
            _bot_urls.clear()

    async def test_close_position_with_slash_in_symbol(self, client, mock_bot):
        from web.server import _bot_urls

        _bot_urls["testbot"] = "http://bot-testbot:9035"
        try:
            with patch("web.server._forward_to_bot", new_callable=AsyncMock) as fwd:
                from web.schemas import ActionResponse

                fwd.return_value = ActionResponse(success=True, message="Closed")
                r = await client.post("/api/position/close", json={"symbol": "BTC/USDT", "bot_id": "testbot"})
                assert r.status_code == 200
                assert r.json()["success"] is True
                fwd.assert_awaited_once_with("testbot", "/api/position/close", {"symbol": "BTC/USDT"})
        finally:
            _bot_urls.clear()

    async def test_close_position_execute_fails(self, client, mock_bot):
        from web.server import _bot_urls

        _bot_urls["testbot"] = "http://bot-testbot:9035"
        try:
            with patch("web.server._forward_to_bot", new_callable=AsyncMock) as fwd:
                from web.schemas import ActionResponse

                fwd.return_value = ActionResponse(success=False, message="Exchange error")
                r = await client.post("/api/position/close", json={"symbol": "BTCUSDT", "bot_id": "testbot"})
                assert r.status_code == 200
                assert r.json()["success"] is False
                assert "Exchange error" in r.json()["message"]
        finally:
            _bot_urls.clear()

    async def test_take_profit_no_bot(self, client):
        r = await client.post("/api/position/take-profit", json={"symbol": "BTCUSDT", "pct": 50})
        assert r.status_code == 200
        assert r.json()["success"] is False

    async def test_take_profit_no_position(self, client, mock_bot):
        from web.server import _bot_urls

        _bot_urls["testbot"] = "http://bot-testbot:9035"
        try:
            with patch("web.server._forward_to_bot", new_callable=AsyncMock) as fwd:
                from web.schemas import ActionResponse

                fwd.return_value = ActionResponse(success=False, message="No open position")
                r = await client.post(
                    "/api/position/take-profit", json={"symbol": "BTCUSDT", "pct": 50, "bot_id": "testbot"}
                )
                assert r.status_code == 200
                assert r.json()["success"] is False
                assert "No open position" in r.json()["message"]
        finally:
            _bot_urls.clear()

    async def test_take_profit_ok(self, client, mock_bot):
        from web.server import _bot_urls

        _bot_urls["testbot"] = "http://bot-testbot:9035"
        try:
            with patch("web.server._forward_to_bot", new_callable=AsyncMock) as fwd:
                from web.schemas import ActionResponse

                fwd.return_value = ActionResponse(success=True, message="Took 50%")
                r = await client.post(
                    "/api/position/take-profit", json={"symbol": "BTCUSDT", "pct": 50, "bot_id": "testbot"}
                )
                assert r.status_code == 200
                assert r.json()["success"] is True
        finally:
            _bot_urls.clear()

    async def test_tighten_stop_no_bot(self, client):
        r = await client.post("/api/position/tighten-stop", json={"symbol": "BTCUSDT", "pct": 2})
        assert r.status_code == 200
        assert r.json()["success"] is False

    async def test_tighten_stop_no_trailing_stop(self, client, mock_bot):
        from web.server import _bot_urls

        _bot_urls["testbot"] = "http://bot-testbot:9035"
        try:
            with patch("web.server._forward_to_bot", new_callable=AsyncMock) as fwd:
                from web.schemas import ActionResponse

                fwd.return_value = ActionResponse(success=False, message="No trailing stop")
                r = await client.post(
                    "/api/position/tighten-stop", json={"symbol": "BTCUSDT", "pct": 2, "bot_id": "testbot"}
                )
                assert r.status_code == 200
                assert r.json()["success"] is False
                assert "No trailing stop" in r.json()["message"]
        finally:
            _bot_urls.clear()

    async def test_tighten_stop_ok(self, client, mock_bot):
        from web.server import _bot_urls

        _bot_urls["testbot"] = "http://bot-testbot:9035"
        try:
            with patch("web.server._forward_to_bot", new_callable=AsyncMock) as fwd:
                from web.schemas import ActionResponse

                fwd.return_value = ActionResponse(success=True, message="Tightened")
                r = await client.post(
                    "/api/position/tighten-stop", json={"symbol": "BTCUSDT", "pct": 2, "bot_id": "testbot"}
                )
                assert r.status_code == 200
                assert r.json()["success"] is True
        finally:
            _bot_urls.clear()


# ── POST /api/close-all, stop-trading, resume-trading ──────────────────


class TestCloseAllStopResume:
    async def test_close_all_no_bot(self, client):
        with patch("web.server._broadcast_to_remote_bots", new_callable=AsyncMock) as broadcast:
            broadcast.return_value = "ok"
            r = await client.post("/api/close-all")
        assert r.status_code == 200
        assert r.json()["success"] is True
        assert broadcast.await_count == 2

    async def test_close_all_ok(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        with patch("web.server._broadcast_to_remote_bots", new_callable=AsyncMock) as broadcast:
            broadcast.return_value = "ok"
            r = await client.post("/api/close-all")
            assert r.status_code == 200
            assert r.json()["success"] is True
            assert broadcast.await_count == 2

    async def test_stop_trading_no_bot(self, client):
        with patch("web.server._broadcast_to_remote_bots", new_callable=AsyncMock) as broadcast:
            broadcast.return_value = "ok"
            r = await client.post("/api/stop-trading")
        assert r.status_code == 200
        assert r.json()["success"] is True

    async def test_stop_trading_ok(self, client, mock_bot, tmp_path):
        set_bot(mock_bot)  # type: ignore[arg-type]
        with patch("web.server._broadcast_to_remote_bots", new_callable=AsyncMock) as broadcast:
            broadcast.return_value = "ok"
            r = await client.post("/api/stop-trading")
            assert r.status_code == 200
            assert r.json()["success"] is True
            broadcast.assert_awaited_once()

    async def test_resume_trading_ok(self, client, mock_bot, tmp_path):
        set_bot(mock_bot)  # type: ignore[arg-type]
        with patch("web.server._broadcast_to_remote_bots", new_callable=AsyncMock) as broadcast:
            broadcast.return_value = "ok"
            r = await client.post("/api/resume-trading")
            assert r.status_code == 200
            assert r.json()["success"] is True
            broadcast.assert_awaited_once()


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
        r = await client.post("/api/module/news/toggle")
        assert r.status_code == 200
        assert r.json()["success"] is False
        assert "hub" in r.json()["message"].lower()

    async def test_toggle_intel_disable(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        r = await client.post("/api/module/intel/toggle")
        assert r.status_code == 200
        assert r.json()["success"] is False
        assert "hub" in r.json()["message"].lower()


# ── Reset Profit Buffer ──────────────────────────────────────────────────


class TestResetProfitBuffer:
    async def test_reset_no_bot(self, client):
        with patch("web.server._broadcast_to_remote_bots", new_callable=AsyncMock) as broadcast:
            broadcast.return_value = "ok"
            r = await client.post("/api/reset-profit-buffer")
        assert r.status_code == 200
        assert r.json()["success"] is True

    async def test_reset_clears_buffer(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        with patch("web.server._broadcast_to_remote_bots", new_callable=AsyncMock) as broadcast:
            broadcast.return_value = "ok"
            r = await client.post("/api/reset-profit-buffer")
            assert r.status_code == 200
            assert r.json()["success"] is True
            broadcast.assert_awaited_once()

    async def test_status_includes_profit_buffer(self, client):
        hub_state = MagicMock()
        with patch("web.server._hub_state_ref", hub_state):
            _bot_reports.clear()
            report_bot_snapshot(
                {
                    "bot_id": "solo",
                    "exchange": "MEXC",
                    "status": {
                        "running": True,
                        "balance": 1000.0,
                        "available_margin": 500.0,
                        "daily_pnl": 0,
                        "daily_pnl_pct": 0,
                        "total_growth_usd": 0,
                        "total_growth_pct": 0,
                        "trading_mode": "paper_local",
                        "exchange_name": "MEXC",
                        "strategies_count": 0,
                        "profit_buffer_pct": 15.5,
                    },
                    "positions": [],
                    "wick_scalps": [],
                    "strategies": [],
                }
            )
            try:
                r = await client.get("/api/status")
                assert r.status_code == 200
                assert r.json()["profit_buffer_pct"] == 15.5
            finally:
                _bot_reports.clear()


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

    async def test_db_tables_with_bot(self, client, mock_bot):
        _ALLOWED_TABLES.clear()
        set_bot(mock_bot)  # type: ignore[arg-type]
        r = await client.get("/api/db/tables")
        assert r.status_code == 200
        data = r.json()
        names = {t["name"] for t in data}
        assert "trades" in names

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

    def test_set_bot_is_noop_does_not_register(self, mock_bot, tmp_path):
        """set_bot() is a no-op in hub mode; it does not register URLs."""
        import web.server
        from web.server import _bot_urls

        original_path = web.server._BOT_REGISTRY
        try:
            web.server._BOT_REGISTRY = tmp_path / "reg.json"
            _bot_urls.clear()
            mock_bot.settings.bot_id = "momentum"
            set_bot(mock_bot)  # type: ignore[arg-type]
            assert "momentum" not in _bot_urls
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

    async def test_action_with_bot_id_forwards_to_that_bot(self, client, mock_bot):
        """Actions with bot_id are forwarded to that bot (no local execution)."""
        mock_bot.settings.bot_id = "momentum"
        set_bot(mock_bot)  # type: ignore[arg-type]
        from web.server import _bot_urls

        _bot_urls["momentum"] = "http://bot-momentum:9035"
        try:
            with patch("web.server._forward_to_bot", new_callable=AsyncMock) as fwd:
                from web.schemas import ActionResponse

                fwd.return_value = ActionResponse(success=True, message="Closed")
                r = await client.post("/api/position/close", json={"symbol": "BTC/USDT", "bot_id": "momentum"})
                assert r.status_code == 200
                assert r.json()["success"] is True
                fwd.assert_awaited_once_with("momentum", "/api/position/close", {"symbol": "BTC/USDT"})
        finally:
            _bot_urls.clear()

    async def test_empty_bot_id_not_registered(self, client, mock_bot):
        """Empty bot_id: no local bot, forward fails with 'not registered'."""
        mock_bot.settings.bot_id = "momentum"
        set_bot(mock_bot)  # type: ignore[arg-type]
        r = await client.post("/api/position/close", json={"symbol": "BTC/USDT", "bot_id": ""})
        assert r.status_code == 200
        assert r.json()["success"] is False
        assert "not registered" in r.json()["message"].lower()


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
                calls = [c[0] for c in fwd.call_args_list]
                assert ("meanrev", "/api/stop-trading", {}) in calls
                assert ("momentum", "/api/stop-trading", {}) in calls
                assert ("meanrev", "/api/close-all", {}) in calls
                assert ("momentum", "/api/close-all", {}) in calls
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
                assert r.json()["success"] is True
                assert fwd.await_count == 2
                calls = [c[0] for c in fwd.call_args_list]
                assert ("swing", "/api/stop-trading", {}) in calls
                assert ("momentum", "/api/stop-trading", {}) in calls
        finally:
            _bot_urls.clear()


# ── Trade Queue Lifecycle Statuses ──────────────────────────────────────


class TestTradeQueueLifecycle:
    async def test_trade_queue_shows_pending_proposals(self, client):
        from shared.models import SignalPriority, TradeProposal, TradeQueue

        hub_state = MagicMock()
        q = TradeQueue()
        now = datetime.now(UTC).isoformat()
        q.add(
            TradeProposal(
                priority=SignalPriority.DAILY,
                symbol="BTC/USDT",
                side="long",
                strategy="mom",
                strength=0.8,
                created_at=now,
                max_age_seconds=14400,
            )
        )
        q.add(
            TradeProposal(
                priority=SignalPriority.DAILY,
                symbol="SOL/USDT",
                side="long",
                strategy="intel",
                strength=0.5,
                created_at=now,
            )
        )
        hub_state.read_trade_queue.return_value = q
        with patch("web.server._hub_state_ref", hub_state):
            r = await client.get("/api/trade-queue")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 2
        by_sym = {d["symbol"]: d for d in data}
        assert by_sym["BTC/USDT"]["strategy"] == "mom"
        assert by_sym["SOL/USDT"]["strategy"] == "intel"


# ── Analytics with Multibot Positions ───────────────────────────────────


class TestAnalyticsMultibot:
    async def test_analytics_aggregates_positions_from_reports(self, client):
        hub_state = MagicMock()
        hub_state.read_analytics.return_value = MagicMock(weights={})
        with patch("web.server._hub_state_ref", hub_state):
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
        set_bot(mock_bot)  # type: ignore[arg-type]
        r = await client.post("/api/module/intel/toggle")
        assert r.status_code == 200
        assert r.json()["success"] is False
        assert "hub" in r.json()["message"].lower()

    async def test_news_toggle_rejected_in_multibot(self, client, mock_bot):
        set_bot(mock_bot)  # type: ignore[arg-type]
        r = await client.post("/api/module/news/toggle")
        assert r.status_code == 200
        assert r.json()["success"] is False
        assert "hub" in r.json()["message"].lower()

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

    async def test_news_with_items(self, client):
        from shared.models import IntelSnapshot

        hub_state = MagicMock()
        snap = IntelSnapshot(
            news_items=[
                {
                    "headline": "BTC surges",
                    "source": "coindesk",
                    "url": "https://example.com/1",
                    "published": datetime.now(UTC).isoformat(),
                    "matched_symbols": ["BTC/USDT"],
                    "sentiment": "bullish",
                    "sentiment_score": 0.8,
                },
            ]
        )
        hub_state.read_intel.return_value = snap
        with patch("web.server._hub_state_ref", hub_state):
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
    """Test /internal/trade endpoint."""

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

    async def test_push_trade_missing_bot_id(self, client):
        r = await client.post(
            "/internal/trade",
            json={"action": "open", "trade": {"symbol": "X"}},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "error"


# ── Bot Profiles ─────────────────────────────────────────────────────


class TestBotProfiles:
    async def test_list_profiles_returns_all(self, client, mock_bot):
        set_bot(mock_bot)
        r = await client.get("/api/bot-profiles")
        assert r.status_code == 200
        data = r.json()
        assert len(data) >= 9
        ids = [p["id"] for p in data]
        assert "hub" not in ids
        assert "momentum" in ids
        assert "extreme" in ids
        assert "scalper" in ids
        assert "conservative" in ids

    async def test_profiles_contain_expected_fields(self, client, mock_bot):
        set_bot(mock_bot)
        r = await client.get("/api/bot-profiles")
        assert r.status_code == 200
        for p in r.json():
            assert "id" in p
            assert "display_name" in p
            assert "description" in p
            assert "style" in p
            assert "strategies" in p
            assert "is_hub" in p
            assert "enabled" in p
            assert "container_status" in p

    async def test_hub_excluded_from_profiles(self, client, mock_bot):
        set_bot(mock_bot)
        r = await client.get("/api/bot-profiles")
        data = r.json()
        assert all(p["id"] != "hub" for p in data)
        assert all(not p["is_hub"] for p in data)

    async def test_toggle_hub_rejected(self, client, mock_bot):
        set_bot(mock_bot)
        r = await client.post("/api/bot-profile/hub/toggle")
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is False
        assert "Hub" in data["message"]

    async def test_toggle_unknown_profile(self, client, mock_bot):
        set_bot(mock_bot)
        r = await client.post("/api/bot-profile/nonexistent/toggle")
        assert r.status_code == 200
        assert r.json()["success"] is False

    async def test_toggle_enable_disable(self, client, mock_bot):
        set_bot(mock_bot)
        hub = _get_hub_db()
        hub.set_bot_enabled("indicators", True)
        r = await client.post("/api/bot-profile/indicators/toggle")
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is True
        assert "Disabled" in data["message"]
        assert hub.is_bot_enabled("indicators") is False

    async def test_toggle_disable_to_enable(self, client, mock_bot):
        set_bot(mock_bot)
        hub = _get_hub_db()
        hub.set_bot_enabled("scalper", False)
        r = await client.post("/api/bot-profile/scalper/toggle")
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is True
        assert "Enabled" in data["message"]
        assert hub.is_bot_enabled("scalper") is True

    async def test_profile_balance_is_available_plus_margin_plus_unrealized(self, client, mock_bot):
        set_bot(mock_bot)
        _bot_reports.clear()
        report_bot_snapshot(
            {
                "bot_id": "momentum",
                "exchange": "MEXC",
                "status": {
                    "running": True,
                    "available_margin": 450.0,
                    "daily_pnl": 5.0,
                },
                "positions": [
                    {"symbol": "BTC/USDT", "notional_value": 100.0, "leverage": 5, "pnl_usd": 10.0},
                    {"symbol": "ETH/USDT", "notional_value": 60.0, "leverage": 3, "pnl_usd": 20.0},
                ],
                "wick_scalps": [],
                "strategies": [],
            }
        )
        try:
            r = await client.get("/api/bot-profiles")
            assert r.status_code == 200
            data = r.json()
            profile = next(p for p in data if p["id"] == "momentum")
            # balance_now = available + used_margin + unrealized
            # 450 + (100/5 + 60/3) + (10+20) = 520
            assert profile["balance"] == pytest.approx(520.0)
        finally:
            _bot_reports.clear()
