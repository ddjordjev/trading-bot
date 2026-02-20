"""Tests for services/monitor.py — MonitorService coverage."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
    with patch("services.monitor.SharedState") as mock_state_cls:
        with patch("services.monitor.FearGreedClient") as mock_fg:
            with patch("services.monitor.LiquidationMonitor") as mock_liq:
                with patch("services.monitor.MacroCalendar") as mock_macro:
                    with patch("services.monitor.WhaleSentiment") as mock_whales:
                        with patch("services.monitor.TradingViewClient") as mock_tv:
                            with patch("services.monitor.CoinMarketCapClient") as mock_cmc:
                                with patch("services.monitor.CoinGeckoClient") as mock_gecko:
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
                                                mock_state.write_bot_trade_queue = MagicMock()
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
                                                    mock_scanner,
                                                ):
                                                    m.return_value.start = AsyncMock()
                                                    m.return_value.stop = AsyncMock()

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
    monitor.scanner.stop.assert_awaited_once()


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
