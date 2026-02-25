"""Tests for services/monitor.py — MonitorService coverage."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hub.state import HubState
from shared.models import (
    BotDeploymentStatus,
    DeploymentLevel,
    IntelSnapshot,
    TradeQueue,
)


@pytest.fixture
def mock_settings():
    s = MagicMock()
    s.coinglass_api_key = ""
    s.intel_symbol_list = ["BTC", "ETH"]
    s.tv_exchange = "MEXC"
    s.tv_interval_list = ["1h", "4h"]
    s.tv_poll_interval = 120
    s.cmc_api_key = ""
    s.cmc_poll_interval = 300
    s.coingecko_api_key = ""
    s.coingecko_poll_interval = 300
    return s


@pytest.fixture
def monitor(mock_settings, tmp_path):
    """MonitorService with all dependencies mocked."""
    with patch("services.monitor.HubState") as mock_state_cls:
        with patch("services.monitor.FearGreedClient") as mock_fg:
            with patch("services.monitor.LiquidationMonitor") as mock_liq:
                with patch("services.monitor.MacroCalendar") as mock_macro:
                    with patch("services.monitor.WhaleSentiment") as mock_whales:
                        with patch("services.monitor.TradingViewClient") as mock_tv:
                            with patch("services.monitor.CoinMarketCapClient") as mock_cmc:
                                with patch("services.monitor.CoinGeckoClient") as mock_gecko:
                                    with patch("services.monitor.OpenClawClient") as mock_openclaw:
                                        with patch("services.monitor.TrendingScanner") as mock_scanner:
                                            with patch("services.monitor.SignalGenerator") as _mock_sg:
                                                with patch("services.monitor.get_settings", return_value=mock_settings):
                                                    from services.monitor import MonitorService

                                                    # Build mock state
                                                    mock_state = MagicMock()
                                                    mock_state.read_bot_status.return_value = BotDeploymentStatus(
                                                        level=DeploymentLevel.HUNTING
                                                    )
                                                    mock_state.read_all_bot_statuses.return_value = []
                                                    mock_state.read_trade_queue.return_value = TradeQueue()
                                                    mock_state.write_intel = MagicMock()
                                                    mock_state.write_trade_queue = MagicMock()
                                                    mock_state_cls.return_value = mock_state

                                                    # Async start/stop for all clients
                                                    for m in (
                                                        mock_fg,
                                                        mock_liq,
                                                        mock_macro,
                                                        mock_whales,
                                                        mock_tv,
                                                        mock_cmc,
                                                        mock_gecko,
                                                        mock_openclaw,
                                                        mock_scanner,
                                                    ):
                                                        m.return_value.start = AsyncMock()
                                                        m.return_value.stop = AsyncMock()
                                                    mock_openclaw.return_value.latest = None

                                                    svc = MonitorService(settings=mock_settings)
                                                    svc.state = mock_state
                                                    return svc


# ── start / stop / _run_loop ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_starts_all_clients_and_enters_loop(monitor):
    monitor._run_loop = AsyncMock()
    await monitor.start()
    monitor.fear_greed.start.assert_awaited_once()
    monitor.liquidations.start.assert_awaited_once()
    monitor.macro.start.assert_awaited_once()
    monitor.whales.start.assert_awaited_once()
    monitor.tv.start.assert_awaited_once()
    monitor.cmc.start.assert_awaited_once()
    monitor.gecko.start.assert_awaited_once()
    monitor.openclaw.start.assert_awaited_once()
    monitor.scanner.start.assert_awaited_once()
    monitor._run_loop.assert_awaited_once()
    assert monitor._running is True


@pytest.mark.asyncio
async def test_stop_sets_running_false_and_stops_clients(monitor):
    monitor._running = True
    await monitor.stop()
    assert monitor._running is False
    monitor.fear_greed.stop.assert_awaited_once()
    monitor.liquidations.stop.assert_awaited_once()
    monitor.macro.stop.assert_awaited_once()
    monitor.whales.stop.assert_awaited_once()
    monitor.tv.stop.assert_awaited_once()
    monitor.cmc.stop.assert_awaited_once()
    monitor.gecko.stop.assert_awaited_once()
    monitor.openclaw.stop.assert_awaited_once()
    monitor.scanner.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_openclaw_enabled_false_stops_and_clears(monitor):
    monitor.openclaw.base_url = "http://localhost:18080/intel"
    monitor.openclaw.set_enabled = AsyncMock(return_value=False)
    monitor.openclaw.fetch_once = AsyncMock()
    enabled = await monitor.set_openclaw_enabled(False)
    assert enabled is False
    monitor.openclaw.set_enabled.assert_awaited_once_with(False)
    monitor.openclaw.fetch_once.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_openclaw_enabled_true_fetches_immediately(monitor):
    monitor.openclaw.base_url = "http://localhost:18080/intel"
    monitor.openclaw.set_enabled = AsyncMock(return_value=True)
    monitor.openclaw.fetch_once = AsyncMock()
    enabled = await monitor.set_openclaw_enabled(True)
    assert enabled is True
    monitor.openclaw.set_enabled.assert_awaited_once_with(True)
    monitor.openclaw.fetch_once.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_loop_handles_tick_exception(monitor):
    monitor._running = True
    monitor.state.read_all_bot_statuses.side_effect = RuntimeError("tick error")

    async def stop_after_one(secs):
        monitor._running = False

    with patch("asyncio.get_event_loop") as mock_loop:
        mock_loop.return_value.time.return_value = 0.0
        with patch("asyncio.sleep", side_effect=stop_after_one):
            await monitor._run_loop()
    # Loop should have caught the exception and slept
    monitor.state.read_all_bot_statuses.assert_called()


# ── _update_intensity ─────────────────────────────────────────────────────


def test_update_intensity_logs_when_level_changes(monitor):
    monitor._current_level = DeploymentLevel.HUNTING
    status = BotDeploymentStatus(level=DeploymentLevel.DEPLOYED)
    monitor._update_intensity(status)
    assert monitor._current_level == DeploymentLevel.DEPLOYED


# ── _refresh_tv ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_tv_hunting_adds_hot_movers(monitor):
    hot = MagicMock()
    hot.trading_pair = "SOL/USDT"
    monitor.scanner.hot_movers = [hot]
    monitor._current_level = DeploymentLevel.HUNTING
    monitor.tv.analyze_multi = AsyncMock(return_value=[])
    await monitor._refresh_tv(BotDeploymentStatus(level=DeploymentLevel.HUNTING))
    monitor.tv.analyze_multi.assert_awaited()
    call_args = monitor.tv.analyze_multi.call_args_list[0][0][0]
    assert "BTC/USDT" in call_args
    assert "ETH/USDT" in call_args
    assert "SOL/USDT" in call_args


@pytest.mark.asyncio
async def test_refresh_tv_deployed_only_btc_eth(monitor):
    monitor._current_level = DeploymentLevel.DEPLOYED
    monitor._tv_symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
    monitor.tv.analyze_multi = AsyncMock(return_value=[])
    await monitor._refresh_tv(BotDeploymentStatus(level=DeploymentLevel.DEPLOYED))
    call_args = monitor.tv.analyze_multi.call_args_list[0][0][0]
    assert call_args == ["BTC/USDT", "ETH/USDT"]


@pytest.mark.asyncio
async def test_refresh_tv_stressed_adds_hot_movers(monitor):
    hot = MagicMock()
    hot.trading_pair = "AVAX/USDT"
    monitor.scanner.hot_movers = [hot]
    monitor._current_level = DeploymentLevel.STRESSED
    monitor.tv.analyze_multi = AsyncMock(return_value=[])
    await monitor._refresh_tv(BotDeploymentStatus(level=DeploymentLevel.STRESSED))
    call_args = monitor.tv.analyze_multi.call_args_list[0][0][0]
    assert "AVAX/USDT" in call_args


@pytest.mark.asyncio
async def test_refresh_tv_not_deployed_calls_4h(monitor):
    monitor._current_level = DeploymentLevel.HUNTING
    monitor.tv.analyze_multi = AsyncMock(return_value=[])
    await monitor._refresh_tv(BotDeploymentStatus(level=DeploymentLevel.HUNTING))
    assert monitor.tv.analyze_multi.await_count >= 2


@pytest.mark.asyncio
async def test_refresh_tv_handles_exception(monitor):
    monitor._current_level = DeploymentLevel.HUNTING
    monitor.tv.analyze_multi = AsyncMock(side_effect=ConnectionError("tv error"))
    await monitor._refresh_tv(BotDeploymentStatus(level=DeploymentLevel.HUNTING))
    # No raise
    monitor.tv.analyze_multi.assert_awaited()


# ── _refresh_scanner_symbols ──────────────────────────────────────────────


def test_refresh_scanner_symbols_adds_cmc_tradable(monitor):
    coin = MagicMock()
    coin.symbol = "sol"
    coin.is_tradable_size = True
    monitor.cmc.all_interesting = [coin]
    monitor.gecko.all_interesting = []
    monitor._refresh_scanner_symbols()
    assert "SOL/USDT" in monitor._tv_symbols


def test_refresh_scanner_symbols_adds_gecko_volume(monitor):
    monitor.cmc.all_interesting = []
    coin = MagicMock()
    coin.symbol = "avax"
    coin.volume_24h = 2_000_000
    monitor.gecko.all_interesting = [coin]
    monitor._refresh_scanner_symbols()
    assert "AVAX/USDT" in monitor._tv_symbols


def test_refresh_scanner_symbols_skips_gecko_low_volume(monitor):
    monitor.cmc.all_interesting = []
    coin = MagicMock()
    coin.symbol = "low"
    coin.volume_24h = 500_000
    monitor.gecko.all_interesting = [coin]
    monitor._refresh_scanner_symbols()
    assert "LOW/USDT" not in monitor._tv_symbols


# ── _build_snapshot ──────────────────────────────────────────────────────


def test_build_snapshot_includes_fear_greed_and_liquidations(monitor):
    monitor.fear_greed.value = 25
    monitor.fear_greed.trade_direction_bias = MagicMock(return_value="long")
    monitor.fear_greed.position_bias = MagicMock(return_value=1.0)
    monitor.fear_greed.is_extreme_fear = False
    monitor.fear_greed.is_extreme_greed = False
    monitor.fear_greed.is_fear = True
    monitor.fear_greed.is_greed = False
    monitor.fear_greed.latest = True

    liq = MagicMock()
    liq.total_24h = 1e9
    liq.is_mass_liquidation = True
    monitor.liquidations.latest = liq
    monitor.liquidations.reversal_bias = MagicMock(return_value="short")
    monitor.liquidations.aggression_boost = MagicMock(return_value=1.0)

    monitor.macro.has_imminent_event = MagicMock(return_value=False)
    monitor.macro.exposure_multiplier = MagicMock(return_value=1.0)
    monitor.macro.is_spike_opportunity = MagicMock(return_value=False)
    monitor.macro.next_event_info = MagicMock(return_value="")

    monitor.whales.contrarian_bias = MagicMock(return_value="neutral")
    monitor.whales.get = MagicMock(return_value=None)

    monitor.tv.consensus = MagicMock(side_effect=lambda s: "neutral")
    monitor.tv.signal_boost = MagicMock(return_value=1.0)
    monitor.tv._cache = {}

    monitor.scanner.hot_movers = []
    monitor.cmc.trending = []
    monitor.cmc.all_interesting = []
    monitor.gecko.trending = []
    monitor.gecko.all_interesting = []

    mult = {"base": 1.0, "tv": 1.0, "scanner": 1.0, "intel": 1.0}
    snap = monitor._build_snapshot(mult)
    assert snap.fear_greed == 25
    assert snap.fear_greed_bias == "long"
    assert snap.liquidation_24h == 1e9
    assert snap.mass_liquidation is True
    assert snap.liquidation_bias == "short"
    assert "fear_greed" in snap.sources_active
    assert "liquidations" in snap.sources_active


def test_build_snapshot_merges_openclaw_advisory_payload(monitor):
    from intel.openclaw import (
        OpenClawAltData,
        OpenClawIdeaBrief,
        OpenClawRegimeCommentary,
        OpenClawSnapshot,
        OpenClawTriageEntry,
    )

    monitor.fear_greed.value = 50
    monitor.fear_greed.trade_direction_bias = MagicMock(return_value="neutral")
    monitor.fear_greed.position_bias = MagicMock(return_value=1.0)
    monitor.fear_greed.is_extreme_fear = False
    monitor.fear_greed.is_extreme_greed = False
    monitor.fear_greed.is_fear = False
    monitor.fear_greed.is_greed = False
    monitor.fear_greed.latest = True

    monitor.liquidations.latest = None
    monitor.liquidations.reversal_bias = MagicMock(return_value="neutral")
    monitor.liquidations.aggression_boost = MagicMock(return_value=1.0)
    monitor.macro.has_imminent_event = MagicMock(return_value=False)
    monitor.macro.exposure_multiplier = MagicMock(return_value=1.0)
    monitor.macro.is_spike_opportunity = MagicMock(return_value=False)
    monitor.macro.next_event_info = MagicMock(return_value="")
    monitor.whales.contrarian_bias = MagicMock(return_value="neutral")
    monitor.whales.get = MagicMock(return_value=None)
    monitor.tv.consensus = MagicMock(return_value="neutral")
    monitor.tv.signal_boost = MagicMock(return_value=1.0)
    monitor.tv._cache = {}
    monitor.scanner.hot_movers = []
    monitor.cmc.trending = []
    monitor.cmc.all_interesting = []
    monitor.gecko.trending = []
    monitor.gecko.all_interesting = []

    monitor.openclaw.latest = OpenClawSnapshot(
        regime_commentary=OpenClawRegimeCommentary(regime="risk_on", confidence=0.77, why=["fear reset"]),
        alt_data=OpenClawAltData(
            sentiment_score=12,
            long_short_ratio=0.88,
            liquidations_24h_usd=321_000_000,
            open_interest_24h_usd=89_000_000_000,
        ),
        idea_briefs=[OpenClawIdeaBrief(symbol="SOL/USDT", side="long", confidence=0.66)],
        failure_triage=[OpenClawTriageEntry(severity="high", component="monitor", issue="stale intel")],
    )

    mult = {"base": 1.0, "tv": 1.0, "scanner": 1.0, "intel": 1.0}
    snap = monitor._build_snapshot(mult)
    assert snap.openclaw_regime == "risk_on"
    assert snap.openclaw_regime_confidence == 0.77
    assert snap.openclaw_sentiment_score == 12
    assert snap.openclaw_long_short_ratio == 0.88
    assert snap.openclaw_liquidations_24h_usd == 321_000_000
    assert len(snap.openclaw_idea_briefs) == 1
    assert "openclaw" in snap.sources_active


# ── _derive_regime ───────────────────────────────────────────────────────


def test_derive_regime_caution(monitor):
    monitor.fear_greed.is_extreme_fear = False
    monitor.fear_greed.is_extreme_greed = False
    monitor.fear_greed.is_fear = False
    monitor.fear_greed.is_greed = False
    snap = IntelSnapshot(should_reduce_exposure=True, mass_liquidation=False)
    assert monitor._derive_regime(snap) == "caution"


# ── _compute_size_mult / _compute_direction (already in test_services, extra here) ──


def test_compute_size_mult_uses_fear_greed_and_macro(monitor):
    monitor.fear_greed.position_bias = MagicMock(return_value=0.8)
    monitor.liquidations.aggression_boost = MagicMock(return_value=1.0)
    snap = IntelSnapshot(macro_exposure_mult=0.5)
    mult = monitor._compute_size_mult(snap)
    assert mult == 0.4


# ── _aggregate_bot_statuses ────────────────────────────────────────────


class TestAggregateBotStatuses:
    def test_empty_returns_default(self, monitor):
        from services.monitor import MonitorService

        result = MonitorService._aggregate_bot_statuses([])
        assert result.level == DeploymentLevel.HUNTING

    def test_single_bot(self, monitor):
        from services.monitor import MonitorService

        statuses = [BotDeploymentStatus(bot_id="m", level=DeploymentLevel.DEPLOYED, open_positions=2, max_positions=5)]
        result = MonitorService._aggregate_bot_statuses(statuses)
        assert result.level == DeploymentLevel.DEPLOYED
        assert result.open_positions == 2

    def test_picks_worst_level(self, monitor):
        from services.monitor import MonitorService

        statuses = [
            BotDeploymentStatus(level=DeploymentLevel.HUNTING, open_positions=0),
            BotDeploymentStatus(level=DeploymentLevel.STRESSED, open_positions=5),
        ]
        result = MonitorService._aggregate_bot_statuses(statuses)
        assert result.level == DeploymentLevel.STRESSED

    def test_sums_positions(self, monitor):
        from services.monitor import MonitorService

        statuses = [
            BotDeploymentStatus(open_positions=3, max_positions=5),
            BotDeploymentStatus(open_positions=2, max_positions=5),
        ]
        result = MonitorService._aggregate_bot_statuses(statuses)
        assert result.open_positions == 5
        assert result.max_positions == 10

    def test_averages_daily_pnl(self, monitor):
        from services.monitor import MonitorService

        statuses = [
            BotDeploymentStatus(daily_pnl_pct=4.0),
            BotDeploymentStatus(daily_pnl_pct=6.0),
        ]
        result = MonitorService._aggregate_bot_statuses(statuses)
        assert result.daily_pnl_pct == pytest.approx(5.0)


# ── _route_to_bots ────────────────────────────────────────────────────


class TestRouteToBotsMonitor:
    def test_routes_by_style(self, monitor, tmp_path):
        from shared.models import SignalPriority, TradeProposal

        hub_state = HubState()
        hub_state.write_trade_queue = MagicMock()
        monitor.state = hub_state

        staging = TradeQueue()
        p = TradeProposal(
            priority=SignalPriority.DAILY,
            symbol="BTC/USDT",
            side="long",
            strength=0.8,
            target_bot="momentum",
        )
        staging.add(p)
        statuses = [
            BotDeploymentStatus(bot_id="bot-momentum", bot_style="momentum", should_trade=True, has_capacity=True),
        ]
        monitor._route_to_bots(staging, statuses)
        hub_state.write_trade_queue.assert_called_once()

    def test_skips_consumed_proposals(self, monitor, tmp_path):
        from shared.models import SignalPriority, TradeProposal

        hub_state = HubState()
        hub_state.write_trade_queue = MagicMock()
        monitor.state = hub_state

        staging = TradeQueue()
        p = TradeProposal(
            priority=SignalPriority.DAILY,
            symbol="BTC/USDT",
            side="long",
            strength=0.8,
            consumed=True,
        )
        staging.add(p)
        monitor._route_to_bots(staging, [])
        hub_state.write_trade_queue.assert_called_once()
        call_queue = hub_state.write_trade_queue.call_args[0][0]
        assert call_queue.pending_count == 0

    def test_merges_to_shared_queue_even_when_bot_at_capacity(self, monitor, tmp_path):
        from shared.models import SignalPriority, TradeProposal

        hub_state = HubState()
        hub_state.write_trade_queue = MagicMock()
        monitor.state = hub_state

        staging = TradeQueue()
        p = TradeProposal(
            priority=SignalPriority.DAILY,
            symbol="BTC/USDT",
            side="long",
            strength=0.8,
            target_bot="momentum",
        )
        staging.add(p)
        statuses = [
            BotDeploymentStatus(
                bot_id="bot-momentum", bot_style="momentum", should_trade=True, open_positions=10, max_positions=10
            ),
        ]
        monitor._route_to_bots(staging, statuses)
        hub_state.write_trade_queue.assert_called_once()
        call_queue = hub_state.write_trade_queue.call_args[0][0]
        assert call_queue.pending_count >= 1

    def test_route_to_bots_filters_untradeable_symbols(self, monitor):
        from shared.models import SignalPriority, TradeProposal

        hub_state = HubState()
        monitor.state = hub_state
        monitor._exchange_symbols = {"BINANCE": {"BTC/USDT"}}

        staging = TradeQueue()
        staging.add(
            TradeProposal(
                priority=SignalPriority.DAILY,
                symbol="FAKE/USDT",
                side="long",
                strength=0.6,
                supported_exchanges=["BINANCE"],
            )
        )
        staging.add(
            TradeProposal(
                priority=SignalPriority.DAILY,
                symbol="BTC/USDT",
                side="long",
                strength=0.7,
                supported_exchanges=["BINANCE"],
            )
        )

        with patch("db.hub_store.HubDB") as mock_db_cls:
            mock_db = MagicMock()
            mock_db.connect = MagicMock()
            mock_db.close = MagicMock()
            mock_db.get_open_trade_symbols = MagicMock(return_value=set())
            mock_db_cls.return_value = mock_db
            monitor._route_to_bots(staging, [])

        queued = hub_state.read_trade_queue().proposals
        assert len(queued) == 1
        assert queued[0].symbol == "BTC/USDT"


class TestExchangeSymbolFiltering:
    @pytest.mark.asyncio
    async def test_fetch_exchange_symbols_filters_by_market_type_and_active(self, monitor):
        class FakeExchange:
            def __init__(self, *_args, **_kwargs):
                self.markets = {}

            def set_sandbox_mode(self, *_args, **_kwargs):
                return None

            async def load_markets(self):
                self.markets = {
                    "BTC/USDT": {"spot": True, "future": False, "swap": False, "active": True},
                    "ETH/USDT:USDT": {"spot": False, "future": False, "swap": True, "active": True},
                    "BAD/USDT:USDT": {"spot": False, "future": False, "swap": True, "active": False},
                }

            async def close(self):
                return None

        monitor._SUPPORTED_EXCHANGES = ("binance",)
        monitor.settings.futures_allowed = True
        monitor.settings.trading_mode = "paper_live"

        with patch("ccxt.async_support.binance", FakeExchange):
            await monitor._fetch_exchange_symbols()

        assert monitor._exchange_symbols["BINANCE"] == {"ETH/USDT"}


# ── _on_news ─────────────────────────────────────────────────────────


class TestOnNews:
    @pytest.mark.asyncio
    async def test_on_news_appends(self, monitor):
        from news.monitor import NewsItem

        item = NewsItem(headline="BTC pumps", source="coindesk", matched_symbols=["BTC/USDT"], sentiment_score=0.5)
        await monitor._on_news(item)
        assert len(monitor._recent_news) == 1
        assert monitor._recent_news[0].headline == "BTC pumps"

    @pytest.mark.asyncio
    async def test_on_news_trims_to_200(self, monitor):
        from news.monitor import NewsItem

        for i in range(210):
            await monitor._on_news(NewsItem(headline=f"News {i}", source="test"))
        assert len(monitor._recent_news) == 200


# ── _run_loop one successful iteration ────────────────────────────────


class TestRunLoopIteration:
    @pytest.mark.asyncio
    async def test_run_loop_one_tick_writes_intel_and_routes(self, monitor):

        monitor._running = True
        monitor._last_tv_refresh = 0
        monitor._last_scanner_refresh = 0

        monitor.state.read_all_bot_statuses.return_value = [
            BotDeploymentStatus(bot_id="m", level=DeploymentLevel.HUNTING)
        ]
        monitor._build_snapshot = MagicMock(return_value=IntelSnapshot(regime="normal"))
        monitor._refresh_tv = AsyncMock()
        monitor._refresh_scanner_symbols = MagicMock()
        monitor.signal_gen = MagicMock()
        monitor.signal_gen.generate = MagicMock(return_value=TradeQueue())
        monitor._route_to_bots = MagicMock()

        async def stop_on_sleep(sec):
            monitor._running = False

        with patch("asyncio.sleep", side_effect=stop_on_sleep):
            with patch("time.monotonic", return_value=99999):
                await monitor._run_loop()

        monitor.state.write_intel.assert_called_once()
        monitor.signal_gen.generate.assert_called_once()
