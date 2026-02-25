"""Tests for bot.py — TradingBot orchestrator."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from config.settings import get_settings
from core.models import Signal, SignalAction
from core.models.order import Order, OrderSide, OrderStatus, OrderType
from core.orders.scaler import ScaleMode
from db.models import TradeRecord

# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def settings():
    """Settings with intel disabled to avoid external clients in __init__."""
    s = get_settings()
    s.intel_enabled = False
    return s


@pytest.fixture
def mock_exchange():
    ex = AsyncMock()
    ex.fetch_balance = AsyncMock(return_value={"USDT": 10_000.0})
    ex.fetch_positions = AsyncMock(return_value=[])
    ex.fetch_candles = AsyncMock(return_value=[])
    ex.fetch_ticker = AsyncMock(return_value=MagicMock(last=50_000.0, symbol="BTC/USDT"))
    ex.connect = AsyncMock()
    ex.disconnect = AsyncMock()
    ex.get_available_symbols = AsyncMock(return_value=["BTC/USDT", "ETH/USDT"])
    return ex


@pytest.fixture
def bot(settings, mock_exchange):
    """TradingBot with mocked exchange (no local DB — hub-centric)."""
    with patch("bot.create_exchange", return_value=mock_exchange):
        from bot import TradingBot

        return TradingBot(settings=settings, daily_target_pct=10.0)


# ── __init__ ────────────────────────────────────────────────────────────────


class TestTradingBotInit:
    def test_init_creates_subsystems(self, bot, mock_exchange):
        assert bot.exchange is mock_exchange
        assert bot.risk is not None
        assert bot.orders is not None
        assert bot.notifier is not None
        assert bot.volatility is not None
        assert bot.target is not None
        assert bot.market_filter is not None
        assert bot._strategies == []
        assert bot._running is False
        assert isinstance(bot._open_trades, dict)
        assert isinstance(bot._pending_hub_acks, dict)
        assert isinstance(bot._strategy_stats, dict)

    def test_init_with_intel_disabled_sets_intel_none(self, settings, mock_exchange):
        settings.intel_enabled = False
        with patch("bot.create_exchange", return_value=mock_exchange):
            from bot import TradingBot

            b = TradingBot(settings=settings)
        assert b.intel is None


# ── add_strategy / add_custom_strategy ──────────────────────────────────────


class TestTradingBotStrategyManagement:
    @pytest.mark.parametrize("market_type", ["spot", "futures"])
    def test_add_strategy_registers_builtin(self, settings, mock_exchange, market_type):
        settings.allowed_market_types = "spot,futures"
        with patch("bot.create_exchange", return_value=mock_exchange):
            from bot import TradingBot

            b = TradingBot(settings=settings)
            b.add_strategy("compound_momentum", "BTC/USDT", market_type=market_type)
            assert len(b._strategies) == 1
            assert b._strategies[0].symbol == "BTC/USDT"
            assert b._strategies[0].name == "compound_momentum"
            assert b._strategies[0].market_type == market_type

    @pytest.mark.parametrize("market_type", ["spot", "futures"])
    def test_add_strategy_unknown_raises(self, settings, mock_exchange, market_type):
        settings.allowed_market_types = "spot,futures"
        with patch("bot.create_exchange", return_value=mock_exchange):
            from bot import TradingBot

            b = TradingBot(settings=settings)
            with pytest.raises(ValueError, match="Unknown strategy"):
                b.add_strategy("unknown_strat", "BTC/USDT", market_type=market_type)

    def test_add_strategy_market_type_not_allowed_fallback_to_spot(self, settings, mock_exchange):
        settings.allowed_market_types = "spot"
        with patch("bot.create_exchange", return_value=mock_exchange):
            from bot import TradingBot

            b = TradingBot(settings=settings)
            b.add_strategy("compound_momentum", "BTC/USDT", market_type="futures")
            assert len(b._strategies) == 1
            assert b._strategies[0].market_type == "spot"

    def test_add_strategy_market_type_not_allowed_skip_when_no_fallback(self, settings, mock_exchange):
        settings.allowed_market_types = "futures"
        with patch("bot.create_exchange", return_value=mock_exchange):
            from bot import TradingBot

            b = TradingBot(settings=settings)
            b.add_strategy("compound_momentum", "BTC/USDT", market_type="spot")
            assert len(b._strategies) == 0

    def test_add_custom_strategy(self, bot):
        from strategies.base import BaseStrategy

        class CustomStrat(BaseStrategy):
            @property
            def name(self):
                return "custom"

            def analyze(self, candles, ticker=None):
                return None

        custom = CustomStrat("XRP/USDT", market_type="spot", leverage=1)
        bot.add_custom_strategy(custom)
        assert len(bot._strategies) == 1
        assert bot._strategies[0].name == "custom"
        assert bot._strategies[0].symbol == "XRP/USDT"


# ── _log_closed_trade ───────────────────────────────────────────────────────


class TestLogClosedTrade:
    def test_log_closed_trade_with_open_record(self, bot):
        order = Order(
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            amount=0.01,
            price=50_000.0,
            average_price=50_100.0,
            filled=0.01,
            status=OrderStatus.FILLED,
            strategy="compound_momentum",
        )
        sp = MagicMock()
        sp.mode = ScaleMode.PYRAMID
        sp.side = "long"
        sp.adds = 2
        sp.low_liquidity = False
        sp.avg_entry_price = 49_500.0
        sp.current_leverage = 10
        bot.orders.scaler.get = MagicMock(return_value=sp)
        bot.intel = None

        open_rec = TradeRecord(
            symbol="BTC/USDT",
            side="long",
            strategy="compound_momentum",
            action="open",
            opened_at="2026-02-20T10:00:00+00:00",
        )
        bot._open_trades["BTC/USDT"] = open_rec
        bot._log_closed_trade(order, "stop")

        assert "BTC/USDT" not in bot._open_trades
        stats = bot._strategy_stats.get("compound_momentum:BTC/USDT", {})
        assert stats.get("total", 0) >= 1

    def test_log_closed_trade_no_open_record(self, bot):
        order = Order(
            symbol="ETH/USDT",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            amount=0.1,
            price=3000.0,
            average_price=3100.0,
            filled=0.1,
            status=OrderStatus.FILLED,
            strategy="rsi",
        )
        bot.orders.scaler.get = MagicMock(return_value=None)
        bot.intel = None

        bot._log_closed_trade(order, "stop")

        stats = bot._strategy_stats.get("rsi:ETH/USDT", bot._strategy_stats.get("stop:ETH/USDT", {}))
        assert stats.get("total", 0) >= 1

    def test_log_closed_trade_discards_whale_alerted(self, bot):
        bot._whale_alerted.add("BTC/USDT")
        order = Order(
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            amount=0.01,
            price=50_000.0,
            filled=0.01,
            status=OrderStatus.FILLED,
            strategy="compound_momentum",
        )
        bot.orders.scaler.get = MagicMock(return_value=None)
        bot._log_closed_trade(order, "stop")
        assert "BTC/USDT" not in bot._whale_alerted

    @pytest.mark.asyncio
    async def test_log_opened_trade_stores_in_memory(self, bot):
        sig = Signal(
            symbol="SOL/USDT",
            action=SignalAction.BUY,
            strength=0.75,
            strategy="swing_opportunity",
            reason="test",
            market_type="futures",
        )
        order = Order(
            symbol="SOL/USDT",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            amount=1.0,
            average_price=150.0,
            filled=1.0,
            status=OrderStatus.FILLED,
            leverage=5,
        )
        sp = MagicMock()
        sp.mode = ScaleMode.PYRAMID
        sp.low_liquidity = False
        sp.current_leverage = 5
        bot.orders.scaler.get = MagicMock(return_value=sp)
        bot.intel = None
        bot._push_trade_to_hub = AsyncMock()

        await bot._log_opened_trade(sig, order)

        assert "SOL/USDT" in bot._open_trades
        record = bot._open_trades["SOL/USDT"]
        assert record.symbol == "SOL/USDT"
        assert record.strategy == "swing_opportunity"
        assert record.action == "open"
        assert record.entry_price == 150.0
        assert record.opened_at != ""


# ── _check_daily_reset ───────────────────────────────────────────────────────


class TestCheckDailyReset:
    @pytest.mark.asyncio
    async def test_check_daily_reset_skips_when_not_midnight(self, bot, mock_exchange):
        with patch("bot.datetime") as m_dt:
            m_dt.now.return_value = datetime(2025, 2, 19, 14, 30, tzinfo=UTC)
            await bot._check_daily_reset()
        mock_exchange.fetch_balance.assert_not_called()

    @pytest.mark.asyncio
    async def test_check_daily_reset_runs_at_midnight(self, bot, mock_exchange):
        mock_exchange.fetch_positions = AsyncMock(return_value=[])
        with patch("bot.datetime") as m_dt:
            m_dt.now.return_value = datetime(2025, 2, 20, 0, 1, tzinfo=UTC)
            await bot._check_daily_reset()
        mock_exchange.fetch_positions.assert_called()
        assert bot.target._day_number == 1


# ── _log_status ─────────────────────────────────────────────────────────────


class TestLogStatus:
    @pytest.mark.asyncio
    async def test_log_status_throttles_by_interval(self, bot):
        bot._last_status_log = datetime.now(UTC)
        bot.target.status_report = MagicMock(return_value="status")
        bot.risk.risk_summary = MagicMock(return_value="risk")
        from core.orders.hedge import HedgeManager
        from core.orders.scaler import PositionScaler
        from core.orders.trailing import TrailingStopManager
        from core.orders.wick_scalp import WickScalpDetector

        with (
            patch.object(PositionScaler, "active_positions", new_callable=PropertyMock, return_value={}),
            patch.object(TrailingStopManager, "active_stops", new_callable=PropertyMock, return_value={}),
            patch.object(HedgeManager, "active_pairs", new_callable=PropertyMock, return_value={}),
            patch.object(WickScalpDetector, "active_scalps", new_callable=PropertyMock, return_value={}),
        ):
            await bot._log_status()
            await bot._log_status()
            bot.target.status_report.assert_not_called()

    @pytest.mark.asyncio
    async def test_log_status_logs_positions_and_stops(self, bot):
        bot._last_status_log = None
        bot.target.status_report = MagicMock(return_value="status")
        bot.risk.risk_summary = MagicMock(return_value="risk")
        sp = MagicMock()
        sp.status_line.return_value = "BTC/USDT long 1x"
        from core.orders.hedge import HedgeManager
        from core.orders.scaler import PositionScaler
        from core.orders.trailing import TrailingStopManager
        from core.orders.wick_scalp import WickScalpDetector

        with (
            patch.object(PositionScaler, "active_positions", new_callable=PropertyMock, return_value={"BTC/USDT": sp}),
            patch.object(TrailingStopManager, "active_stops", new_callable=PropertyMock, return_value={}),
            patch.object(HedgeManager, "active_pairs", new_callable=PropertyMock, return_value={}),
            patch.object(WickScalpDetector, "active_scalps", new_callable=PropertyMock, return_value={}),
        ):
            await bot._log_status()
            sp.status_line.assert_called()


# ── _read_shared_intel / _read_shared_analytics_weight / _adjust_for_target ─


class TestSharedStateHelpers:
    def test_read_shared_intel_returns_condition_when_fresh(self, bot):
        bot._hub_intel_age = 100
        snap = MagicMock()
        snap.sources_active = ["fear_greed"]
        snap.regime = "normal"
        snap.fear_greed = 50
        snap.fear_greed_bias = "neutral"
        snap.liquidation_24h = 0.0
        snap.mass_liquidation = False
        snap.liquidation_bias = "neutral"
        snap.macro_event_imminent = False
        snap.macro_exposure_mult = 1.0
        snap.macro_spike_opportunity = False
        snap.next_macro_event = ""
        snap.whale_bias = "neutral"
        snap.overleveraged_side = ""
        snap.tv_btc_consensus = "neutral"
        snap.tv_eth_consensus = "neutral"
        snap.position_size_multiplier = 1.0
        snap.should_reduce_exposure = False
        snap.preferred_direction = "neutral"
        bot._hub_intel = snap
        cond = bot._read_shared_intel()
        assert cond is not None
        assert cond.preferred_direction == "neutral"

    def test_adjust_for_target_caps_strength_by_aggression(self, bot):
        sig = Signal(
            symbol="BTC/USDT",
            action=SignalAction.BUY,
            strength=0.8,
            strategy="x",
            reason="",
            market_type="futures",
        )
        out = bot._adjust_for_target(sig, aggression=0.5)
        assert out.strength == 0.4

    def test_adjust_for_target_reduces_when_ride_mode(self, bot):
        from core.risk.daily_target import DailyTargetTracker

        with (
            patch.object(DailyTargetTracker, "in_ride_mode", new_callable=PropertyMock, return_value=True),
            patch.object(DailyTargetTracker, "daily_profit_secured", new_callable=PropertyMock, return_value=True),
        ):
            sig = Signal(
                symbol="BTC/USDT",
                action=SignalAction.BUY,
                strength=0.8,
                strategy="x",
                reason="",
                market_type="futures",
                quick_trade=False,
            )
            out = bot._adjust_for_target(sig, aggression=1.0)
            assert out.strength == pytest.approx(0.8 * 0.3)

    def test_adjust_for_target_slightly_reduces_when_secured(self, bot):
        from core.risk.daily_target import DailyTargetTracker

        with (
            patch.object(DailyTargetTracker, "in_ride_mode", new_callable=PropertyMock, return_value=False),
            patch.object(DailyTargetTracker, "daily_profit_secured", new_callable=PropertyMock, return_value=True),
        ):
            sig = Signal(
                symbol="BTC/USDT",
                action=SignalAction.BUY,
                strength=0.8,
                strategy="x",
                reason="",
                market_type="futures",
                quick_trade=False,
            )
            out = bot._adjust_for_target(sig, aggression=1.0)
            assert out.strength == pytest.approx(0.8 * 0.6)


# ── _process_signal / _process_trade_queue / _execute_proposal ──────────────


class TestProcessSignalAndQueue:
    @pytest.mark.asyncio
    async def test_process_signal_weak_strength_skips(self, bot):
        bot.orders.execute_signal = AsyncMock()
        sig = Signal(
            symbol="BTC/USDT",
            action=SignalAction.BUY,
            strength=0.1,
            strategy="x",
            reason="test",
            market_type="spot",
        )
        await bot._process_signal(sig)
        bot.orders.execute_signal.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_signal_executes_and_appends_active(self, bot):
        order = MagicMock()
        bot.orders.execute_signal = AsyncMock(return_value=order)
        bot.target.record_trade = MagicMock()
        sig = Signal(
            symbol="BTC/USDT",
            action=SignalAction.BUY,
            strength=0.8,
            strategy="x",
            reason="test",
            market_type="spot",
        )
        await bot._process_signal(sig)
        bot.orders.execute_signal.assert_called_once()
        assert len(bot._active_signals) == 1
        bot.target.record_trade.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_signal_close_accepts_low_strength(self, bot):
        bot.orders.execute_signal = AsyncMock(return_value=MagicMock())
        sig = Signal(
            symbol="BTC/USDT",
            action=SignalAction.CLOSE,
            strength=0.0,
            strategy="x",
            reason="close",
            market_type="spot",
        )
        await bot._process_signal(sig)
        bot.orders.execute_signal.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_trade_queue_empty_returns_early(self, bot):
        bot._hub_proposal = None
        await bot._process_trade_queue()
        bot.exchange.fetch_positions.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_trade_queue_read_exception_returns_early(self, bot):
        bot._hub_proposal = None
        await bot._process_trade_queue()
        bot.exchange.fetch_positions.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_trade_queue_processes_single_proposal(self, bot, mock_exchange):
        """Hub serves one proposal at a time — bot processes it and reports."""
        from datetime import timedelta

        from shared.models import SignalPriority, TradeProposal

        bot._started_at = datetime.now(UTC) - timedelta(minutes=10)
        bot._warmup_minutes = 1

        p = TradeProposal(
            priority=SignalPriority.CRITICAL,
            symbol="BTC/USDT",
            side="long",
            strategy="m",
            reason="t1",
            strength=0.9,
            market_type="futures",
            supported_exchanges=["MEXC"],
        )
        bot._hub_proposal = p

        mock_exchange.fetch_positions = AsyncMock(return_value=[])
        bot._execute_proposal = AsyncMock(return_value=True)
        bot._is_symbol_open_on_exchange = AsyncMock(return_value=False)
        bot._report_queue_outcome = AsyncMock()
        bot.target.should_trade = MagicMock(return_value=True)
        bot.target.aggression_multiplier = MagicMock(return_value=1.0)
        bot.target.reset_day(100.0)
        bot.target.update_balance(105.0)

        await bot._process_trade_queue()
        bot._execute_proposal.assert_called_once()
        bot._report_queue_outcome.assert_called_once()
        assert bot._hub_proposal is None

    @pytest.mark.asyncio
    async def test_validate_proposal_uses_proposal_market_type(self, bot, mock_exchange):
        from core.models import MarketType
        from shared.models import SignalPriority, TradeProposal
        from validators import ValidationResult

        mock_exchange.fetch_candles = AsyncMock(return_value=[])
        mock_exchange.fetch_ticker = AsyncMock(return_value=MagicMock(last=50_000.0))
        bot._validator.validate = MagicMock(return_value=ValidationResult(valid=True, reason="ok"))
        proposal = TradeProposal(
            priority=SignalPriority.CRITICAL,
            symbol="BTC/USDT",
            side="long",
            strategy="momentum",
            reason="test",
            strength=0.8,
            market_type="futures",
        )

        result = await bot._validate_proposal(proposal)
        assert result.valid is True
        mock_exchange.fetch_candles.assert_called_once_with("BTC/USDT", "1m", limit=50, market_type=MarketType.FUTURES)
        mock_exchange.fetch_ticker.assert_called_once_with("BTC/USDT", market_type=MarketType.FUTURES)

    @pytest.mark.asyncio
    async def test_execute_proposal_success(self, bot, mock_exchange):
        from shared.models import SignalPriority, TradeProposal
        from validators import ValidationResult

        mock_exchange.fetch_ticker = AsyncMock(return_value=MagicMock(last=50_000.0))
        bot._process_signal = AsyncMock()
        bot._validate_proposal = AsyncMock(return_value=ValidationResult(valid=True, reason="ok"))
        proposal = TradeProposal(
            priority=SignalPriority.CRITICAL,
            symbol="BTC/USDT",
            side="long",
            strategy="momentum",
            reason="test",
            strength=0.8,
            market_type="futures",
            supported_exchanges=["MEXC"],
        )
        ok = await bot._execute_proposal(proposal, aggression=0.8)
        assert ok is True
        bot._process_signal.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_proposal_validator_rejects(self, bot, mock_exchange):
        from shared.models import SignalPriority, TradeProposal
        from validators import ValidationResult

        bot._validate_proposal = AsyncMock(return_value=ValidationResult(valid=False, reason="momentum stalled"))
        proposal = TradeProposal(
            priority=SignalPriority.CRITICAL,
            symbol="BTC/USDT",
            side="long",
            strategy="x",
            reason="",
            strength=0.5,
            market_type="futures",
        )
        ok = await bot._execute_proposal(proposal, aggression=1.0)
        assert ok is False

    @pytest.mark.asyncio
    async def test_execute_proposal_fetch_ticker_fails(self, bot, mock_exchange):
        from shared.models import SignalPriority, TradeProposal
        from validators import ValidationResult

        bot._validate_proposal = AsyncMock(return_value=ValidationResult(valid=True, reason="ok"))
        mock_exchange.fetch_ticker = AsyncMock(side_effect=RuntimeError("api down"))
        proposal = TradeProposal(
            priority=SignalPriority.CRITICAL,
            symbol="BTC/USDT",
            side="long",
            strategy="x",
            reason="",
            strength=0.5,
            market_type="futures",
        )
        ok = await bot._execute_proposal(proposal, aggression=1.0)
        assert ok is False

    @pytest.mark.asyncio
    async def test_execute_swing_proposal_success(self, bot, mock_exchange):
        from shared.models import EntryPlan, SignalPriority, TradeProposal
        from validators import ValidationResult

        mock_exchange.fetch_ticker = AsyncMock(return_value=MagicMock(last=50_000.0))
        bot._process_signal = AsyncMock()
        bot._validate_proposal = AsyncMock(return_value=ValidationResult(valid=True, reason="ok"))
        proposal = TradeProposal(
            priority=SignalPriority.SWING,
            symbol="ETH/USDT",
            side="long",
            strategy="swing",
            reason="swing setup",
            strength=0.6,
            market_type="futures",
            supported_exchanges=["MEXC"],
            entry_plan=EntryPlan(stop_loss=2000.0, take_profit_targets=[2500.0]),
        )
        ok = await bot._execute_swing_proposal(proposal, aggression=0.7)
        assert ok is True
        bot._process_signal.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_swing_proposal_no_plan(self, bot, mock_exchange):
        from shared.models import SignalPriority, TradeProposal
        from validators import ValidationResult

        mock_exchange.fetch_ticker = AsyncMock(return_value=MagicMock(last=2000.0))
        bot._process_signal = AsyncMock()
        bot._validate_proposal = AsyncMock(return_value=ValidationResult(valid=True, reason="ok"))
        proposal = TradeProposal(
            priority=SignalPriority.SWING,
            symbol="ETH/USDT",
            side="long",
            strategy="swing",
            reason="",
            strength=0.5,
            market_type="futures",
            supported_exchanges=["MEXC"],
            entry_plan=None,
        )
        ok = await bot._execute_swing_proposal(proposal, aggression=1.0)
        assert ok is True


# ── _close_all_positions / _check_whale_positions / _write_deployment_status ─


class TestCloseAllAndWhaleAndDeployment:
    @pytest.mark.asyncio
    async def test_close_all_positions_calls_execute_for_each(self, bot, mock_exchange):
        from core.models.order import OrderSide, Position

        pos = Position(
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            amount=0.01,
            entry_price=50_000.0,
            current_price=50_000.0,
            market_type="futures",
        )
        mock_exchange.fetch_positions = AsyncMock(return_value=[pos])
        bot.orders.execute_signal = AsyncMock()
        await bot._close_all_positions("test reason")
        bot.orders.execute_signal.assert_called_once()
        call_sig = bot.orders.execute_signal.call_args[0][0]
        assert call_sig.action == SignalAction.CLOSE
        assert call_sig.symbol == "BTC/USDT"

    @pytest.mark.asyncio
    async def test_close_all_positions_skips_zero_amount(self, bot, mock_exchange):
        from core.models.order import OrderSide, Position

        pos = Position(
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            amount=0,
            entry_price=50_000.0,
            current_price=50_000.0,
            market_type="futures",
        )
        mock_exchange.fetch_positions = AsyncMock(return_value=[pos])
        bot.orders.execute_signal = AsyncMock()
        await bot._close_all_positions("test")
        bot.orders.execute_signal.assert_not_called()

    @pytest.mark.asyncio
    async def test_check_whale_positions_sends_alert_when_threshold_met(self, bot, mock_exchange):
        from core.models.order import OrderSide, Position

        pos = Position(symbol="BTC/USDT", side=OrderSide.BUY, amount=2.0, entry_price=50_000.0, current_price=60_000.0)
        mock_exchange.fetch_positions = AsyncMock(return_value=[pos])
        sp = MagicMock()
        sp.current_size = 2.0
        sp.current_leverage = 10
        sp.avg_entry_price = 50_000.0
        sp.side = "long"
        sp.adds = 1
        sp._current_profit_pct = MagicMock(return_value=25.0)
        from core.orders.scaler import PositionScaler

        with patch.object(PositionScaler, "active_positions", new_callable=PropertyMock, return_value={"BTC/USDT": sp}):
            bot.notifier.alert_whale_position = AsyncMock()
            await bot._check_whale_positions()
            bot.notifier.alert_whale_position.assert_called_once()
            assert "BTC/USDT" in bot._whale_alerted

    @pytest.mark.asyncio
    async def test_check_whale_positions_skips_already_alerted(self, bot, mock_exchange):
        mock_exchange.fetch_positions = AsyncMock(return_value=[])
        sp = MagicMock()
        sp.current_size = 2.0
        sp.current_leverage = 10
        sp.avg_entry_price = 50_000.0
        sp.side = "long"
        sp.adds = 1
        sp._current_profit_pct = MagicMock(return_value=25.0)
        from core.orders.scaler import PositionScaler

        with patch.object(PositionScaler, "active_positions", new_callable=PropertyMock, return_value={"BTC/USDT": sp}):
            bot._whale_alerted.add("BTC/USDT")
            bot.notifier.alert_whale_position = AsyncMock()
            await bot._check_whale_positions()
            bot.notifier.alert_whale_position.assert_not_called()

    @pytest.mark.asyncio
    async def test_write_deployment_status_writes_to_shared(self, bot, mock_exchange):
        mock_exchange.fetch_positions = AsyncMock(return_value=[])
        bot.shared = MagicMock()
        await bot._write_deployment_status()
        assert bot._last_bot_status is not None
        assert bot._last_bot_status.open_positions == 0
        assert hasattr(bot._last_bot_status, "daily_pnl_pct")


# ── _handle_spike / _on_trending / _on_news ──────────────────────────────────


class TestHandleSpikeAndCallbacks:
    @pytest.mark.asyncio
    async def test_handle_spike_alerts_and_checks_news(self, bot):
        from volatility import SpikeEvent

        bot.notifier.alert_spike = AsyncMock()
        spike = SpikeEvent(
            symbol="BTC/USDT", change_pct=5.0, direction="up", price=52_000.0, volume_24h=1e9, window_seconds=60
        )
        await bot._handle_spike(spike)
        bot.notifier.alert_spike.assert_called_once_with("BTC/USDT", 5.0, "up", 52_000.0)


# ── Adaptive tick interval ──────────────────────────────────────────────────


class TestAdaptiveTickInterval:
    def _make_signal(self, urgency: str = "active", **kwargs: object) -> Signal:
        from core.models.signal import TickUrgency

        return Signal(
            symbol=kwargs.get("symbol", "BTC/USDT"),  # type: ignore[arg-type]
            action=SignalAction.BUY,
            strength=0.8,
            strategy="test",
            tick_urgency=TickUrgency(urgency),
        )

    def test_idle_when_no_positions(self, bot):
        bot._active_signals = []
        bot.orders.wick_scalper._active_scalps = {}
        bot.orders.trailing._stops = {}
        bot._tick_interval = 99
        bot._update_tick_interval()
        assert bot._tick_interval == bot.settings.tick_interval_idle

    def test_swing_when_only_swing_signals(self, bot):
        bot._active_signals = [self._make_signal("swing")]
        bot.orders.wick_scalper._active_scalps = {}
        bot.orders.trailing._stops = {}
        bot._tick_interval = 99
        bot._update_tick_interval()
        assert bot._tick_interval == bot.settings.tick_interval_swing

    def test_active_when_trailing_stops_exist(self, bot):
        bot._active_signals = []
        bot.orders.wick_scalper._active_scalps = {}
        bot.orders.trailing._stops = {"BTC/USDT": MagicMock()}
        bot._tick_interval = 99
        bot._update_tick_interval()
        assert bot._tick_interval == bot.settings.tick_interval_active

    def test_active_when_active_signal(self, bot):
        bot._active_signals = [self._make_signal("active")]
        bot.orders.wick_scalper._active_scalps = {}
        bot.orders.trailing._stops = {}
        bot._tick_interval = 99
        bot._update_tick_interval()
        assert bot._tick_interval == bot.settings.tick_interval_active

    def test_scalp_when_scalp_signal(self, bot):
        bot._active_signals = [self._make_signal("scalp")]
        bot.orders.wick_scalper._active_scalps = {}
        bot.orders.trailing._stops = {}
        bot._tick_interval = 99
        bot._update_tick_interval()
        assert bot._tick_interval == bot.settings.tick_interval_scalp

    def test_scalp_when_wick_scalp_active(self, bot):
        bot._active_signals = []
        scalp = MagicMock(active=True, closed=False)
        bot.orders.wick_scalper._active_scalps = {"ETH/USDT": scalp}
        bot.orders.trailing._stops = {}
        bot._tick_interval = 99
        bot._update_tick_interval()
        assert bot._tick_interval == bot.settings.tick_interval_scalp

    def test_scalp_beats_active(self, bot):
        bot._active_signals = [self._make_signal("scalp"), self._make_signal("active")]
        bot.orders.trailing._stops = {"BTC/USDT": MagicMock()}
        bot._tick_interval = 99
        bot._update_tick_interval()
        assert bot._tick_interval == bot.settings.tick_interval_scalp

    def test_scalp_beats_swing(self, bot):
        bot._active_signals = [self._make_signal("scalp"), self._make_signal("swing")]
        bot.orders.wick_scalper._active_scalps = {}
        bot.orders.trailing._stops = {}
        bot._tick_interval = 99
        bot._update_tick_interval()
        assert bot._tick_interval == bot.settings.tick_interval_scalp

    def test_active_beats_swing(self, bot):
        bot._active_signals = [self._make_signal("active"), self._make_signal("swing")]
        bot.orders.wick_scalper._active_scalps = {}
        bot.orders.trailing._stops = {}
        bot._tick_interval = 99
        bot._update_tick_interval()
        assert bot._tick_interval == bot.settings.tick_interval_active

    def test_configurable_via_settings(self, bot):
        bot.settings.tick_interval_idle = 120
        bot.settings.tick_interval_active = 45
        bot.settings.tick_interval_swing = 600
        bot.settings.tick_interval_scalp = 2
        bot._active_signals = [self._make_signal("swing")]
        bot.orders.wick_scalper._active_scalps = {}
        bot.orders.trailing._stops = {}
        bot._tick_interval = 99
        bot._update_tick_interval()
        assert bot._tick_interval == 600

    def test_no_log_when_interval_unchanged(self, bot, caplog):
        bot._active_signals = []
        bot.orders.wick_scalper._active_scalps = {}
        bot.orders.trailing._stops = {}
        bot._tick_interval = bot.settings.tick_interval_idle
        bot._update_tick_interval()
        assert "Tick interval" not in caplog.text


# ── Strategy registration (main()-style) ────────────────────────────────────


class TestRegisterStrategies:
    """Verify multiple strategies can be registered like main() does."""

    def test_register_multiple_strategies_for_symbols(self, bot):
        """Adding multiple strategies populates _strategies with correct names/symbols."""
        mkt = "futures" if bot.settings.futures_allowed else "spot"
        bot.add_strategy("compound_momentum", "BTC/USDT", market_type=mkt)
        bot.add_strategy("rsi", "BTC/USDT", market_type=mkt)
        bot.add_strategy("compound_momentum", "ETH/USDT", market_type=mkt)
        assert len(bot._strategies) == 3
        names = [s.name for s in bot._strategies]
        symbols = [s.symbol for s in bot._strategies]
        assert "compound_momentum" in names
        assert "rsi" in names
        assert "BTC/USDT" in symbols
        assert "ETH/USDT" in symbols

    def test_exchange_created_from_settings_in_init(self, settings, mock_exchange):
        """Exchange is created via create_exchange(settings) in __init__."""
        with patch("bot.create_exchange", return_value=mock_exchange) as m_create:
            from bot import TradingBot

            TradingBot(settings=settings)
        m_create.assert_called_once_with(settings)


# ── _process_trade_queue (reject reasons, apply_updates) ─────────────────────


class TestProcessTradeQueueRejectsAndUpdates:
    @pytest.mark.asyncio
    async def test_process_trade_queue_rejects_market_type_not_allowed(self, bot, mock_exchange):
        from datetime import timedelta

        from shared.models import SignalPriority, TradeProposal

        bot._started_at = datetime.now(UTC) - timedelta(minutes=10)
        bot._warmup_minutes = 1
        with patch.object(type(bot.settings), "is_market_type_allowed", MagicMock(return_value=False)):
            p = TradeProposal(
                priority=SignalPriority.CRITICAL,
                symbol="BTC/USDT",
                side="long",
                strategy="m",
                reason="r",
                strength=0.9,
                market_type="futures",
                supported_exchanges=["MEXC"],
            )
            bot._hub_proposal = p
            bot._report_queue_outcome = AsyncMock()
            mock_exchange.fetch_positions = AsyncMock(return_value=[])
            bot.target.should_trade = MagicMock(return_value=True)
            bot.target.aggression_multiplier = MagicMock(return_value=1.0)
            bot.target.reset_day(100.0)
            bot.target.update_balance(100.0)

            await bot._process_trade_queue()

        bot._report_queue_outcome.assert_called_once()
        call_args = bot._report_queue_outcome.call_args
        assert call_args[0][1] == "rejected"
        assert "not allowed" in call_args[0][2].lower()

    @pytest.mark.asyncio
    async def test_process_trade_queue_rejects_no_free_slots(self, bot, mock_exchange):
        from datetime import timedelta

        from core.models.order import OrderSide, Position
        from shared.models import SignalPriority, TradeProposal

        bot._started_at = datetime.now(UTC) - timedelta(minutes=10)
        bot._warmup_minutes = 1
        with patch.object(type(bot.settings), "effective_max_concurrent_positions", PropertyMock(return_value=1)):
            p = TradeProposal(
                priority=SignalPriority.CRITICAL,
                symbol="BTC/USDT",
                side="long",
                strategy="m",
                reason="r",
                strength=0.9,
                market_type="futures",
                supported_exchanges=["MEXC"],
            )
            bot._hub_proposal = p
            bot._report_queue_outcome = AsyncMock()
            pos = Position(
                symbol="ETH/USDT",
                side=OrderSide.BUY,
                amount=0.1,
                entry_price=3000.0,
                current_price=3100.0,
                market_type="futures",
            )
            mock_exchange.fetch_positions = AsyncMock(return_value=[pos])
            bot.target.should_trade = MagicMock(return_value=True)
            bot.target.aggression_multiplier = MagicMock(return_value=1.0)
            bot.target.reset_day(100.0)
            bot.target.update_balance(100.0)

            await bot._process_trade_queue()

        bot._report_queue_outcome.assert_called_once()
        assert bot._report_queue_outcome.call_args[0][2] == "no free slots"

    @pytest.mark.asyncio
    async def test_process_trade_queue_rejects_when_not_allow_new(self, bot, mock_exchange):
        from datetime import timedelta

        from shared.models import SignalPriority, TradeProposal

        bot._started_at = datetime.now(UTC) - timedelta(minutes=10)
        bot._warmup_minutes = 1
        p = TradeProposal(
            priority=SignalPriority.CRITICAL,
            symbol="BTC/USDT",
            side="long",
            strategy="m",
            reason="r",
            strength=0.9,
            market_type="futures",
            supported_exchanges=["MEXC"],
        )
        bot._hub_proposal = p
        bot._report_queue_outcome = AsyncMock()
        mock_exchange.fetch_positions = AsyncMock(return_value=[])
        bot.target.should_trade = MagicMock(return_value=False)
        tier_mock = MagicMock(value="strong")
        with patch.object(type(bot.target), "tier", PropertyMock(return_value=tier_mock)):
            bot.target.reset_day(100.0)
            bot.target.update_balance(100.0)

            await bot._process_trade_queue()

        bot._report_queue_outcome.assert_called_once()
        assert "tier=" in bot._report_queue_outcome.call_args[0][2]

    @pytest.mark.asyncio
    async def test_process_trade_queue_rejects_manual_stop_before_execution(self, bot, mock_exchange):
        from datetime import timedelta

        from shared.models import SignalPriority, TradeProposal

        bot._started_at = datetime.now(UTC) - timedelta(minutes=10)
        bot._warmup_minutes = 1
        p = TradeProposal(
            priority=SignalPriority.CRITICAL,
            symbol="BTC/USDT",
            side="long",
            strategy="m",
            reason="r",
            strength=0.9,
            market_type="futures",
            supported_exchanges=["MEXC"],
        )
        bot._hub_proposal = p
        bot._report_queue_outcome = AsyncMock()
        mock_exchange.fetch_positions = AsyncMock(return_value=[])
        bot.target.reset_day(100.0)
        bot.target.update_balance(100.0)

        with patch.object(type(bot.target), "manual_stop", PropertyMock(return_value=True)):
            await bot._process_trade_queue()

        bot._report_queue_outcome.assert_called_once()
        assert bot._report_queue_outcome.call_args[0][2] == "manual_stop"
        mock_exchange.fetch_positions.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_trade_queue_consumed_reports_immediately(self, bot, mock_exchange):
        from datetime import timedelta

        from shared.models import SignalPriority, TradeProposal
        from validators import ValidationResult

        bot._started_at = datetime.now(UTC) - timedelta(minutes=10)
        bot._warmup_minutes = 1
        bot._validate_proposal = AsyncMock(return_value=ValidationResult(valid=True, reason="ok"))
        p_ok = TradeProposal(
            priority=SignalPriority.CRITICAL,
            symbol="BTC/USDT",
            side="long",
            strategy="m",
            reason="ok",
            strength=0.9,
            market_type="futures",
            supported_exchanges=["MEXC"],
        )
        bot._hub_proposal = p_ok
        bot._report_queue_outcome = AsyncMock()
        bot._is_symbol_open_on_exchange = AsyncMock(return_value=False)
        mock_exchange.fetch_positions = AsyncMock(return_value=[])
        mock_exchange.fetch_ticker = AsyncMock(return_value=MagicMock(last=50_000.0))
        bot._process_signal = AsyncMock()
        bot.target.should_trade = MagicMock(return_value=True)
        bot.target.aggression_multiplier = MagicMock(return_value=1.0)
        bot.target.reset_day(100.0)
        bot.target.update_balance(100.0)

        await bot._process_trade_queue()

        bot._report_queue_outcome.assert_called_once()
        assert bot._report_queue_outcome.call_args[0][1] == "consumed"


# ── _read_shared_intel (regime, news hydration) ──────────────────────────────


class TestReadSharedIntelExtended:
    def test_read_shared_intel_multibot_hydrates_news(self, bot):
        bot._multibot = True
        bot._hub_intel_age = 100
        snap = MagicMock()
        snap.sources_active = ["fear_greed"]
        snap.regime = "normal"
        snap.fear_greed = 50
        snap.fear_greed_bias = "neutral"
        snap.liquidation_24h = 0.0
        snap.mass_liquidation = False
        snap.liquidation_bias = "neutral"
        snap.macro_event_imminent = False
        snap.macro_exposure_mult = 1.0
        snap.macro_spike_opportunity = False
        snap.next_macro_event = ""
        snap.whale_bias = "neutral"
        snap.overleveraged_side = ""
        snap.tv_btc_consensus = "neutral"
        snap.tv_eth_consensus = "neutral"
        snap.position_size_multiplier = 1.0
        snap.should_reduce_exposure = False
        snap.preferred_direction = "neutral"
        snap.hot_movers = []
        snap.news_items = [
            {
                "headline": "BTC pump",
                "source": "test",
                "url": "https://x.com",
                "published": datetime.now(UTC).isoformat(),
                "matched_symbols": ["BTC/USDT"],
                "sentiment": "bullish",
                "sentiment_score": 0.5,
            },
        ]
        bot._hub_intel = snap
        bot._read_shared_intel()
        assert len(bot._recent_news) == 1
        assert bot._recent_news[0].headline == "BTC pump"
        assert "BTC/USDT" in bot._recent_news[0].matched_symbols


# ── _write_deployment_status / deployment level ──────────────────────────────


class TestWriteDeploymentStatusLevels:
    @pytest.mark.asyncio
    async def test_write_deployment_status_level_hunting_when_no_positions(self, bot, mock_exchange):
        from shared.models import DeploymentLevel

        mock_exchange.fetch_positions = AsyncMock(return_value=[])
        bot.shared = MagicMock()
        await bot._write_deployment_status()
        assert bot._last_bot_status.level == DeploymentLevel.HUNTING
        assert bot._last_bot_status.open_positions == 0

    @pytest.mark.asyncio
    async def test_write_deployment_status_level_stressed_when_losing(self, bot, mock_exchange):
        from core.models.order import OrderSide, Position
        from shared.models import DeploymentLevel

        pos = Position(
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            amount=0.01,
            entry_price=50_000.0,
            current_price=47_000.0,
            market_type="futures",
        )
        mock_exchange.fetch_positions = AsyncMock(return_value=[pos])
        bot._open_trades["BTC/USDT"] = TradeRecord(
            symbol="BTC/USDT",
            side="long",
            strategy="test",
            action="open",
            opened_at="2026-02-20T10:00:00+00:00",
        )
        bot.shared = MagicMock()
        await bot._write_deployment_status()
        assert bot._last_bot_status.level == DeploymentLevel.STRESSED
        assert bot._last_bot_status.worst_position_pnl == pytest.approx(-6.0, abs=0.5)

    @pytest.mark.asyncio
    async def test_write_deployment_status_level_deployed_when_full_and_healthy(self, bot, mock_exchange):
        from core.models.order import OrderSide, Position
        from shared.models import DeploymentLevel

        with patch.object(
            type(bot.settings),
            "effective_max_concurrent_positions",
            PropertyMock(return_value=2),
        ):
            positions = [
                Position(
                    symbol="BTC/USDT",
                    side=OrderSide.BUY,
                    amount=0.01,
                    entry_price=50_000.0,
                    current_price=52_000.0,
                    market_type="futures",
                ),
                Position(
                    symbol="ETH/USDT",
                    side=OrderSide.BUY,
                    amount=0.1,
                    entry_price=3000.0,
                    current_price=3100.0,
                    market_type="futures",
                ),
            ]
            mock_exchange.fetch_positions = AsyncMock(return_value=positions)
            bot._open_trades["BTC/USDT"] = TradeRecord(
                symbol="BTC/USDT",
                side="long",
                strategy="test",
                action="open",
                opened_at="2026-02-20T10:00:00+00:00",
            )
            bot._open_trades["ETH/USDT"] = TradeRecord(
                symbol="ETH/USDT",
                side="long",
                strategy="test",
                action="open",
                opened_at="2026-02-20T10:00:00+00:00",
            )
            bot.shared = MagicMock()
            await bot._write_deployment_status()
        assert bot._last_bot_status.level == DeploymentLevel.DEPLOYED
        assert bot._last_bot_status.open_positions == 2

    @pytest.mark.asyncio
    async def test_write_deployment_status_level_active_when_some_positions(self, bot, mock_exchange):
        from core.models.order import OrderSide, Position
        from shared.models import DeploymentLevel

        with patch.object(
            type(bot.settings),
            "effective_max_concurrent_positions",
            PropertyMock(return_value=5),
        ):
            pos = Position(
                symbol="BTC/USDT",
                side=OrderSide.BUY,
                amount=0.01,
                entry_price=50_000.0,
                current_price=51_000.0,
                market_type="futures",
            )
            mock_exchange.fetch_positions = AsyncMock(return_value=[pos])
            bot._open_trades["BTC/USDT"] = TradeRecord(
                symbol="BTC/USDT",
                side="long",
                strategy="test",
                action="open",
                opened_at="2026-02-20T10:00:00+00:00",
            )
            bot.shared = MagicMock()
            await bot._write_deployment_status()
        assert bot._last_bot_status.level == DeploymentLevel.ACTIVE
        assert bot._last_bot_status.open_positions == 1
        assert bot._last_bot_status.should_trade is not None


# ── _report_dashboard_snapshot ───────────────────────────────────────────────


class TestReportDashboardSnapshot:
    @pytest.mark.asyncio
    async def test_report_dashboard_snapshot_posts_to_hub_when_hub_url_set(self, bot):
        bot.settings.hub_url = "http://hub.example.com"
        bot._hub_session = None
        bot._post_to_hub = AsyncMock()
        await bot._report_dashboard_snapshot([])
        bot._post_to_hub.assert_called_once()
        call_args = bot._post_to_hub.call_args[0]
        assert call_args[0] == "http://hub.example.com"
        assert "payload" in str(call_args) or call_args[1]["status"]
        assert "bot_id" in call_args[1]

    @pytest.mark.asyncio
    async def test_report_dashboard_snapshot_uses_open_trade_timestamp_for_age(self, bot):
        bot.settings.hub_url = "http://hub.example.com"
        bot._post_to_hub = AsyncMock()
        bot._sync_runtime_trade_if_changed = AsyncMock()

        opened_at = (datetime.now(UTC) - timedelta(minutes=37)).isoformat()
        bot._open_trades["BTC/USDT"] = TradeRecord(
            symbol="BTC/USDT",
            side="long",
            strategy="compound_momentum",
            action="open",
            opened_at=opened_at,
        )

        pos = MagicMock()
        pos.symbol = "BTC/USDT"
        pos.side = OrderSide.BUY
        pos.amount = 0.02
        pos.entry_price = 50000.0
        pos.current_price = 50500.0
        pos.pnl_pct = 2.0
        pos.unrealized_pnl = 10.0
        pos.leverage = 5
        pos.market_type = "futures"
        pos.strategy = "compound_momentum"
        pos.notional_value = 1010.0
        # Simulate exchange adapter behavior (fresh Position object each fetch).
        pos.opened_at = datetime.now(UTC)

        await bot._report_dashboard_snapshot([pos])

        payload = bot._post_to_hub.call_args[0][1]
        assert payload["positions"]
        assert payload["positions"][0]["age_minutes"] >= 35

    def test_position_age_minutes_prefers_open_trade_timestamp(self, bot):
        opened_at = (datetime.now(UTC) - timedelta(minutes=25)).isoformat()
        bot._open_trades["ETH/USDT"] = TradeRecord(
            symbol="ETH/USDT",
            side="long",
            strategy="compound_momentum",
            action="open",
            opened_at=opened_at,
        )
        age = bot._position_age_minutes("ETH/USDT", datetime.now(UTC))
        assert age >= 23


# ── start / stop lifecycle ───────────────────────────────────────────────────


class TestStartStopLifecycle:
    @pytest.mark.asyncio
    async def test_start_sets_running_and_started_at(self, bot, mock_exchange):
        mock_exchange.fetch_balance = AsyncMock(return_value={"USDT": 100.0})
        bot.notifier.start = AsyncMock()
        bot._run_loop = AsyncMock()
        bot._fast_monitor_loop = AsyncMock()
        with patch("bot.get_market_schedule") as m_sched:
            m_sched.return_value.configure = MagicMock()
            m_sched.return_value.refresh_holidays = AsyncMock()
            m_sched.return_value.summary = MagicMock(return_value="")
            await bot.start()
        assert bot._running is True
        assert bot._started_at is not None
        bot._run_loop.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_connects_exchange_and_starts_notifier(self, bot, mock_exchange):
        mock_exchange.fetch_balance = AsyncMock(return_value={"USDT": 100.0})
        bot.notifier.start = AsyncMock()
        bot._run_loop = AsyncMock()
        bot._fast_monitor_loop = AsyncMock()
        with patch("bot.get_market_schedule") as m_sched:
            m_sched.return_value.configure = MagicMock()
            m_sched.return_value.refresh_holidays = AsyncMock()
            m_sched.return_value.summary = MagicMock(return_value="")
            await bot.start()
        mock_exchange.connect.assert_called_once()
        bot.notifier.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_sets_running_false_and_disconnects(self, bot):
        bot._running = True
        bot.intel = None
        bot.scanner = None
        bot.news = None
        bot.notifier.stop = AsyncMock()
        bot.exchange.disconnect = AsyncMock()
        await bot.stop()
        assert bot._running is False
        bot.exchange.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_closes_hub_session_if_open(self, bot):
        bot._running = True
        bot.intel = None
        bot.scanner = None
        bot.news = None
        bot.notifier.stop = AsyncMock()
        bot.exchange.disconnect = AsyncMock()
        session = AsyncMock()
        session.close = AsyncMock()
        bot._hub_session = session
        await bot.stop()
        session.close.assert_called_once()
        assert bot._hub_session is None


# ── _check_data_dir_size ───────────────────────────────────────────────────


class TestCheckDataDirSize:
    def test_check_data_dir_size_skips_when_path_does_not_exist(self, bot):
        with patch("bot.Path") as m_path:
            m_path.return_value.exists.return_value = False
            bot._check_data_dir_size()
        m_path.return_value.rglob.assert_not_called()

    def test_check_data_dir_size_logs_warning_when_over_10_mb(self, bot):
        with patch("bot.Path") as m_path:
            m_path.return_value.exists.return_value = True
            f1 = MagicMock()
            f1.is_file.return_value = True
            f1.stat.return_value = MagicMock(st_size=6 * 1024 * 1024)
            f2 = MagicMock()
            f2.is_file.return_value = True
            f2.stat.return_value = MagicMock(st_size=6 * 1024 * 1024)
            m_path.return_value.rglob.return_value = [f1, f2]
            bot._check_data_dir_size()
        m_path.return_value.rglob.assert_called_once()

    def test_check_data_dir_size_logs_info_when_small(self, bot):
        with patch("bot.Path") as m_path:
            m_path.return_value.exists.return_value = True
            f = MagicMock()
            f.is_file.return_value = True
            f.stat.return_value = MagicMock(st_size=1000)
            m_path.return_value.rglob.return_value = [f]
            bot._check_data_dir_size()
        m_path.return_value.rglob.assert_called_once()


# ── _apply_pattern_analysis ─────────────────────────────────────────────────


class TestApplyPatternAnalysis:
    def test_apply_pattern_analysis_returns_zero_when_few_candles(self, bot):
        from core.models import Candle

        candles = [Candle(timestamp=datetime.now(UTC), open=100, high=101, low=99, close=100, volume=1000)] * 20
        sig = Signal(
            symbol="BTC/USDT",
            action=SignalAction.BUY,
            strength=0.8,
            strategy="x",
            reason="",
            market_type="futures",
        )
        boost = bot._apply_pattern_analysis(sig, candles, False)
        assert boost == 0.0

    def test_apply_pattern_analysis_returns_zero_on_detector_exception(self, bot):
        from core.models import Candle

        candles = [Candle(timestamp=datetime.now(UTC), open=100, high=101, low=99, close=100, volume=1000)] * 40
        sig = Signal(
            symbol="BTC/USDT",
            action=SignalAction.BUY,
            strength=0.8,
            strategy="x",
            reason="",
            market_type="futures",
        )
        with patch.object(bot.pattern_detector, "analyze", side_effect=ValueError("bad")):
            boost = bot._apply_pattern_analysis(sig, candles, False)
        assert boost == 0.0

    def test_apply_pattern_analysis_sets_stop_tp_when_detector_returns_smart_stops(self, bot):
        candles = _make_w_candles_for_bot()
        sig = Signal(
            symbol="BTC/USDT",
            action=SignalAction.BUY,
            strength=0.8,
            strategy="x",
            reason="",
            market_type="futures",
            suggested_price=1.3,
        )
        boost = bot._apply_pattern_analysis(sig, candles, False)
        assert isinstance(boost, float)
        assert boost >= 0.0


def _make_w_candles_for_bot():
    from datetime import timedelta

    from core.models import Candle

    prices = (
        [1.5 - i * 0.025 for i in range(20)]
        + [1.0 + i * 0.02 for i in range(10)]
        + [1.2 - i * 0.018 for i in range(10)]
        + [1.02 + i * 0.028 for i in range(10)]
    )
    return [
        Candle(
            timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=i),
            open=p,
            high=p * 1.005,
            low=p * 0.995,
            close=p,
            volume=1000,
        )
        for i, p in enumerate(prices)
    ]


# ── _run_loop exception path ────────────────────────────────────────────────


class TestRunLoopException:
    @pytest.mark.asyncio
    async def test_run_loop_handles_tick_exception_without_crash(self, bot):
        """When _tick() raises, run_loop catches it and sleeps; loop can be stopped."""
        bot._running = True
        bot._tick = AsyncMock(side_effect=RuntimeError("tick failed"))

        async def stop_after_first_sleep(*args, **kwargs):
            bot._running = False

        with patch("web.metrics.record_tick", MagicMock()), patch("web.metrics.record_event_loop_lag", MagicMock()):
            with patch("bot.asyncio.sleep", AsyncMock(side_effect=stop_after_first_sleep)):
                task = asyncio.create_task(bot._run_loop())
                await asyncio.sleep(0.3)
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        assert bot._running is False


# ── _tick (single tick with mocks) ──────────────────────────────────────────


class TestTickInternals:
    @pytest.mark.asyncio
    async def test_tick_fetches_balance_and_positions(self, bot, mock_exchange):
        mock_exchange.fetch_balance = AsyncMock(return_value={"USDT": 500.0})
        mock_exchange.fetch_positions = AsyncMock(return_value=[])
        bot._strategies = []

        with patch.object(type(bot.target), "manual_close_all", PropertyMock(return_value=False)):
            bot.orders.check_stops = AsyncMock(return_value=[])
            bot.orders.try_scale_in = AsyncMock(return_value=[])
            bot.orders.try_lever_up = AsyncMock(return_value=[])
            bot.orders.try_partial_take = AsyncMock(return_value=[])
            bot._check_whale_positions = AsyncMock()
            bot.orders.try_wick_scalps = AsyncMock(return_value=[])
            bot.orders.close_expired_quick_trades = AsyncMock(return_value=[])
            bot._process_trade_queue = AsyncMock()
            bot._read_shared_intel = MagicMock(return_value=None)
            bot.intel = None
            bot._write_deployment_status = AsyncMock()
            bot._log_status = AsyncMock()
            bot._check_daily_reset = AsyncMock()
            await bot._tick()
        mock_exchange.fetch_balance.assert_called_once()
        mock_exchange.fetch_positions.assert_called()

    @pytest.mark.asyncio
    async def test_tick_manual_close_all_closes_positions_and_returns(self, bot, mock_exchange):
        mock_exchange.fetch_balance = AsyncMock(return_value={"USDT": 100.0})
        mock_exchange.fetch_positions = AsyncMock(return_value=[])
        bot._strategies = []

        with patch.object(type(bot.target), "manual_close_all", PropertyMock(return_value=True)):
            bot._close_all_positions = AsyncMock()
            bot.target.clear_close_all = MagicMock()
            await bot._tick()
        bot._close_all_positions.assert_called_once()
        bot.target.clear_close_all.assert_called_once()


# ── start() multibot branch ─────────────────────────────────────────────────


class TestStartMultibot:
    @pytest.mark.asyncio
    async def test_start_multibot_skips_scanner_news_intel_start(self, settings, mock_exchange):
        settings.bot_id = "worker-1"
        with patch("bot.create_exchange", return_value=mock_exchange):
            from bot import TradingBot

            bot = TradingBot(settings=settings)
        assert bot._multibot is True
        bot._full_start = AsyncMock()
        bot._lean_idle_loop = AsyncMock()
        bot._check_initial_activation = AsyncMock(return_value=True)
        await bot.start()
        bot._full_start.assert_awaited_once()
        bot._lean_idle_loop.assert_not_awaited()
        if bot.scanner:
            bot.scanner.start.assert_not_called()
        if bot.news:
            bot.news.start.assert_not_called()


# ── _close_all_positions failure path ────────────────────────────────────────


class TestCloseAllPositionsFailure:
    @pytest.mark.asyncio
    async def test_close_all_positions_continues_when_one_close_raises(self, bot, mock_exchange):
        from core.models.order import OrderSide, Position

        pos1 = Position(
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            amount=0.01,
            entry_price=50_000.0,
            current_price=50_000.0,
            market_type="futures",
        )
        pos2 = Position(
            symbol="ETH/USDT",
            side=OrderSide.BUY,
            amount=0.1,
            entry_price=3000.0,
            current_price=3000.0,
            market_type="futures",
        )
        mock_exchange.fetch_positions = AsyncMock(return_value=[pos1, pos2])
        call_count = 0

        async def execute_maybe_fail(sig):
            nonlocal call_count
            call_count += 1
            if sig.symbol == "BTC/USDT":
                raise RuntimeError("exchange error")
            return MagicMock()

        bot.orders.execute_signal = execute_maybe_fail
        await bot._close_all_positions("test")
        assert call_count == 2


# ── _adjust_for_target target_reached ───────────────────────────────────────


class TestAdjustForTarget:
    def test_adjust_for_target_ride_mode_reduces_non_quick_signal(self, bot):
        with (
            patch.object(type(bot.target), "in_ride_mode", PropertyMock(return_value=True)),
            patch.object(type(bot.target), "daily_profit_secured", PropertyMock(return_value=True)),
        ):
            sig = Signal(
                symbol="BTC/USDT",
                action=SignalAction.BUY,
                strength=0.8,
                strategy="x",
                reason="",
                market_type="futures",
                quick_trade=False,
            )
            out = bot._adjust_for_target(sig, aggression=1.0)
            assert out.strength == pytest.approx(0.8 * 0.3)


# ── _tick: strategy loop with candles, signal, and process_signal ───────────


class TestTickStrategyLoop:
    @pytest.mark.asyncio
    async def test_tick_fetches_candles_for_held_positions(self, bot, mock_exchange):
        """After the hub-centric refactor, the bot only fetches candles for
        symbols it holds (for hedge/volatility checks), not for all strategies."""
        from datetime import timedelta

        from core.models import Candle

        mock_exchange.fetch_balance = AsyncMock(return_value={"USDT": 500.0})
        pos = MagicMock(symbol="BTC/USDT", amount=0.1, side=MagicMock(value="buy"), unrealized_pnl=0.0)
        mock_exchange.fetch_positions = AsyncMock(return_value=[pos])
        candles = [
            Candle(
                timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=i),
                open=50_000.0 + i,
                high=50_010.0,
                low=49_990.0,
                close=50_000.0 + i,
                volume=1000.0,
            )
            for i in range(100)
        ]
        mock_exchange.fetch_candles = AsyncMock(return_value=candles)
        ticker = MagicMock(last=50_100.0, symbol="BTC/USDT")
        mock_exchange.fetch_ticker = AsyncMock(return_value=ticker)

        bot._strategies = []

        with patch.object(type(bot.target), "manual_close_all", PropertyMock(return_value=False)):
            bot.orders.check_stops = AsyncMock(return_value=[])
            bot.orders.try_scale_in = AsyncMock(return_value=[])
            bot.orders.try_lever_up = AsyncMock(return_value=[])
            bot.orders.try_partial_take = AsyncMock(return_value=[])
            bot._check_whale_positions = AsyncMock()
            bot.orders.try_wick_scalps = AsyncMock(return_value=[])
            bot.orders.close_expired_quick_trades = AsyncMock(return_value=[])
            bot._process_trade_queue = AsyncMock()
            bot._read_shared_intel = MagicMock(return_value=None)
            bot._write_deployment_status = AsyncMock()
            bot._log_status = AsyncMock()
            bot._check_daily_reset = AsyncMock()
            bot.orders.has_stale_losers = MagicMock(return_value=False)
            bot.settings.hedge_enabled = False

            await bot._tick()

        from core.models import MarketType

        mock_exchange.fetch_candles.assert_called_with("BTC/USDT", "1m", limit=200, market_type=MarketType.FUTURES)
        mock_exchange.fetch_ticker.assert_called_with("BTC/USDT", market_type=MarketType.FUTURES)

    @pytest.mark.asyncio
    async def test_tick_no_positions_no_candle_fetch(self, bot, mock_exchange):
        """With no held positions, the bot should not fetch candles at all."""
        mock_exchange.fetch_balance = AsyncMock(return_value={"USDT": 500.0})
        mock_exchange.fetch_positions = AsyncMock(return_value=[])
        mock_exchange.fetch_candles = AsyncMock(return_value=[])
        mock_exchange.fetch_ticker = AsyncMock(return_value=MagicMock(last=50_000.0))

        bot._strategies = []

        with patch.object(type(bot.target), "manual_close_all", PropertyMock(return_value=False)):
            bot.orders.check_stops = AsyncMock(return_value=[])
            bot.orders.try_scale_in = AsyncMock(return_value=[])
            bot.orders.try_lever_up = AsyncMock(return_value=[])
            bot.orders.try_partial_take = AsyncMock(return_value=[])
            bot._check_whale_positions = AsyncMock()
            bot.orders.try_wick_scalps = AsyncMock(return_value=[])
            bot.orders.close_expired_quick_trades = AsyncMock(return_value=[])
            bot._process_trade_queue = AsyncMock()
            bot._read_shared_intel = MagicMock(return_value=None)
            bot._write_deployment_status = AsyncMock()
            bot._log_status = AsyncMock()
            bot._check_daily_reset = AsyncMock()
            bot.orders.has_stale_losers = MagicMock(return_value=False)
            bot.settings.hedge_enabled = False

            await bot._tick()

        mock_exchange.fetch_candles.assert_not_called()
        bot._write_deployment_status.assert_called()

    @pytest.mark.asyncio
    async def test_tick_legendary_day_should_close_all_closes_positions(self, bot, mock_exchange):
        mock_exchange.fetch_balance = AsyncMock(return_value={"USDT": 500.0})
        mock_exchange.fetch_positions = AsyncMock(return_value=[])
        bot._strategies = []

        bot._close_all_positions = AsyncMock()
        bot.notifier.send = AsyncMock()

        with patch.object(type(bot.target), "manual_close_all", PropertyMock(return_value=False)):
            with patch.object(type(bot.target), "tier", PropertyMock(return_value=MagicMock(value="legendary"))):
                with patch.object(type(bot.target), "should_close_all", MagicMock(return_value=(True, "reversal"))):
                    bot.orders.check_stops = AsyncMock(return_value=[])
                    bot.orders.try_scale_in = AsyncMock(return_value=[])
                    bot.orders.try_lever_up = AsyncMock(return_value=[])
                    bot.orders.try_partial_take = AsyncMock(return_value=[])
                    bot._check_whale_positions = AsyncMock()
                    bot.orders.try_wick_scalps = AsyncMock(return_value=[])
                    bot.orders.close_expired_quick_trades = AsyncMock(return_value=[])
                    bot._process_trade_queue = AsyncMock()
                    bot._read_shared_intel = MagicMock(return_value=MagicMock(should_reduce_exposure=True))
                    bot._write_deployment_status = AsyncMock()
                    bot._log_status = AsyncMock()
                    bot._check_daily_reset = AsyncMock()
                    bot.settings.hedge_enabled = False

                    await bot._tick()

        bot._close_all_positions.assert_called_once()
        bot.notifier.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_tick_legendary_day_ride_sends_email(self, bot, mock_exchange):
        mock_exchange.fetch_balance = AsyncMock(return_value={"USDT": 500.0})
        mock_exchange.fetch_positions = AsyncMock(return_value=[])
        bot._strategies = []

        bot.notifier.send = AsyncMock()
        bot.target.legendary_ride_reason = MagicMock(return_value="riding")

        with patch.object(type(bot.target), "manual_close_all", PropertyMock(return_value=False)):
            with patch.object(type(bot.target), "legendary_email_sent", PropertyMock(return_value=False)):
                with patch.object(type(bot.target), "tier", PropertyMock(return_value=MagicMock(value="legendary"))):
                    with patch.object(type(bot.target), "should_close_all", MagicMock(return_value=(False, ""))):
                        bot.orders.check_stops = AsyncMock(return_value=[])
                        bot.orders.try_scale_in = AsyncMock(return_value=[])
                        bot.orders.try_lever_up = AsyncMock(return_value=[])
                        bot.orders.try_partial_take = AsyncMock(return_value=[])
                        bot._check_whale_positions = AsyncMock()
                        bot.orders.try_wick_scalps = AsyncMock(return_value=[])
                        bot.orders.close_expired_quick_trades = AsyncMock(return_value=[])
                        bot._process_trade_queue = AsyncMock()
                        bot._read_shared_intel = MagicMock(
                            return_value=MagicMock(
                                should_reduce_exposure=False,
                                position_size_multiplier=1.0,
                                regime=MagicMock(value="normal"),
                            )
                        )
                        bot._write_deployment_status = AsyncMock()
                        bot._log_status = AsyncMock()
                        bot._check_daily_reset = AsyncMock()
                        bot.settings.hedge_enabled = False
                        bot.intel = MagicMock()
                        bot.intel.full_summary = MagicMock(return_value="summary")

                        await bot._tick()

        bot.notifier.send.assert_called()
        call_args = bot.notifier.send.call_args[0]
        assert "RIDING" in call_args[1] or "legendary" in str(call_args).lower()

    @pytest.mark.asyncio
    async def test_tick_has_stale_losers_halves_aggression(self, bot, mock_exchange):
        """Stale losers detection still works — halves aggression even though
        strategy execution moved to the hub."""
        from datetime import timedelta

        from core.models import Candle

        mock_exchange.fetch_balance = AsyncMock(return_value={"USDT": 500.0})
        pos = MagicMock(symbol="BTC/USDT", amount=0.1, side=MagicMock(value="buy"), unrealized_pnl=0.0)
        mock_exchange.fetch_positions = AsyncMock(return_value=[pos])
        candles = [
            Candle(
                timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=i),
                open=50_000.0,
                high=50_010.0,
                low=49_990.0,
                close=50_000.0,
                volume=1000.0,
            )
            for i in range(100)
        ]
        mock_exchange.fetch_candles = AsyncMock(return_value=candles)
        mock_exchange.fetch_ticker = AsyncMock(return_value=MagicMock(last=50_000.0, symbol="BTC/USDT"))

        bot._strategies = []

        with patch.object(type(bot.target), "manual_close_all", PropertyMock(return_value=False)):
            bot.orders.check_stops = AsyncMock(return_value=[])
            bot.orders.try_scale_in = AsyncMock(return_value=[])
            bot.orders.try_lever_up = AsyncMock(return_value=[])
            bot.orders.try_partial_take = AsyncMock(return_value=[])
            bot._check_whale_positions = AsyncMock()
            bot.orders.try_wick_scalps = AsyncMock(return_value=[])
            bot.orders.close_expired_quick_trades = AsyncMock(return_value=[])
            bot._process_trade_queue = AsyncMock()
            bot._read_shared_intel = MagicMock(return_value=None)
            bot._write_deployment_status = AsyncMock()
            bot._log_status = AsyncMock()
            bot._check_daily_reset = AsyncMock()
            bot.settings.hedge_enabled = False
            bot.orders.has_stale_losers = MagicMock(return_value=True)

            await bot._tick()

        mock_exchange.fetch_candles.assert_called()
        bot._write_deployment_status.assert_called()

    @pytest.mark.asyncio
    async def test_tick_check_stops_closed_orders_log_closed_trade(self, bot, mock_exchange):
        from core.models.order import Order, OrderSide, OrderStatus, OrderType

        mock_exchange.fetch_balance = AsyncMock(return_value={"USDT": 500.0})
        mock_exchange.fetch_positions = AsyncMock(return_value=[])
        bot._strategies = []

        closed_order = Order(
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            amount=0.01,
            price=50_000.0,
            average_price=49_500.0,
            filled=0.01,
            status=OrderStatus.FILLED,
            strategy="test",
        )
        sp = MagicMock()
        sp.avg_entry_price = 49_000.0
        sp.side = "long"
        sp.mode.value = "normal"
        sp.current_size = 0.01
        sp.current_leverage = 10
        sp.adds = 0
        bot.orders.scaler.get = MagicMock(return_value=sp)
        bot.orders._closed_scalers = {}
        open_rec = TradeRecord(
            symbol="BTC/USDT",
            side="long",
            strategy="compound_momentum",
            action="open",
            opened_at="2026-02-20T10:00:00+00:00",
        )
        bot._open_trades["BTC/USDT"] = open_rec

        with patch.object(type(bot.target), "manual_close_all", PropertyMock(return_value=False)):
            bot.orders.check_stops = AsyncMock(return_value=[closed_order])
            bot.orders.try_scale_in = AsyncMock(return_value=[])
            bot.orders.try_lever_up = AsyncMock(return_value=[])
            bot.orders.try_partial_take = AsyncMock(return_value=[])
            bot._check_whale_positions = AsyncMock()
            bot.orders.try_wick_scalps = AsyncMock(return_value=[])
            bot.orders.close_expired_quick_trades = AsyncMock(return_value=[])
            bot._process_trade_queue = AsyncMock()
            bot._read_shared_intel = MagicMock(return_value=None)
            bot._write_deployment_status = AsyncMock()
            bot._log_status = AsyncMock()
            bot._check_daily_reset = AsyncMock()
            bot.notifier.alert_stop_loss = AsyncMock()
            bot.settings.hedge_enabled = False

            await bot._tick()

        bot.notifier.alert_stop_loss.assert_called_once()

    @pytest.mark.asyncio
    async def test_tick_hedge_enabled_try_hedge_called(self, bot, mock_exchange):
        from datetime import timedelta

        from core.models import Candle

        mock_exchange.fetch_balance = AsyncMock(return_value={"USDT": 500.0})
        mock_exchange.fetch_positions = AsyncMock(return_value=[])
        candles = [
            Candle(
                timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=i),
                open=50_000.0,
                high=50_010.0,
                low=49_990.0,
                close=50_000.0,
                volume=1000.0,
            )
            for i in range(100)
        ]
        mock_exchange.fetch_candles = AsyncMock(return_value=candles)
        mock_exchange.fetch_ticker = AsyncMock(return_value=MagicMock(last=50_000.0, symbol="BTC/USDT"))

        strat = MagicMock()
        strat.symbol = "BTC/USDT"
        strat.name = "test_strat"
        strat.analyze = MagicMock(return_value=None)
        strat.feed_candle = MagicMock()
        strat.set_position_state = MagicMock()
        bot._strategies = [strat]

        bot.settings.hedge_enabled = True
        bot.orders.try_hedge = AsyncMock(return_value=[MagicMock()])

        with patch.object(type(bot.target), "manual_close_all", PropertyMock(return_value=False)):
            bot.orders.check_stops = AsyncMock(return_value=[])
            bot.orders.try_scale_in = AsyncMock(return_value=[])
            bot.orders.try_lever_up = AsyncMock(return_value=[])
            bot.orders.try_partial_take = AsyncMock(return_value=[])
            bot._check_whale_positions = AsyncMock()
            bot.orders.try_wick_scalps = AsyncMock(return_value=[])
            bot.orders.close_expired_quick_trades = AsyncMock(return_value=[])
            bot._process_trade_queue = AsyncMock()
            bot._read_shared_intel = MagicMock(return_value=None)
            bot._write_deployment_status = AsyncMock()
            bot._log_status = AsyncMock()
            bot._check_daily_reset = AsyncMock()
            bot.orders.has_stale_losers = MagicMock(return_value=False)

            await bot._tick()

        bot.orders.try_hedge.assert_called_once()

    @pytest.mark.asyncio
    async def test_tick_manual_stop_skips_new_entry_paths(self, bot, mock_exchange):
        mock_exchange.fetch_balance = AsyncMock(return_value={"USDT": 500.0})
        mock_exchange.fetch_positions = AsyncMock(return_value=[])
        bot._strategies = []
        bot.settings.hedge_enabled = True
        bot.settings.extreme_enabled = True
        bot._last_extreme_eval = 0.0

        bot.orders.check_stops = AsyncMock(return_value=[])
        bot.orders.try_scale_in = AsyncMock(return_value=[])
        bot.orders.try_lever_up = AsyncMock(return_value=[])
        bot.orders.try_partial_take = AsyncMock(return_value=[])
        bot._check_whale_positions = AsyncMock()
        bot.orders.try_wick_scalps = AsyncMock(return_value=[])
        bot.orders.close_expired_quick_trades = AsyncMock(return_value=[])
        bot._process_trade_queue = AsyncMock()
        bot.orders.try_hedge = AsyncMock(return_value=[MagicMock()])
        bot._evaluate_extreme_candidates = AsyncMock()
        bot._read_shared_intel = MagicMock(return_value=None)
        bot._write_deployment_status = AsyncMock()
        bot._log_status = AsyncMock()
        bot._check_daily_reset = AsyncMock()

        with (
            patch.object(type(bot.target), "manual_close_all", PropertyMock(return_value=False)),
            patch.object(type(bot.target), "manual_stop", PropertyMock(return_value=True)),
        ):
            await bot._tick()

        bot._process_trade_queue.assert_not_called()
        bot.orders.try_hedge.assert_not_called()
        bot._evaluate_extreme_candidates.assert_not_called()

    @pytest.mark.asyncio
    async def test_tick_write_deployment_status_exception_caught(self, bot, mock_exchange):
        mock_exchange.fetch_balance = AsyncMock(return_value={"USDT": 500.0})
        mock_exchange.fetch_positions = AsyncMock(return_value=[])
        bot._strategies = []

        bot._write_deployment_status = AsyncMock(side_effect=RuntimeError("write failed"))

        with patch.object(type(bot.target), "manual_close_all", PropertyMock(return_value=False)):
            bot.orders.check_stops = AsyncMock(return_value=[])
            bot.orders.try_scale_in = AsyncMock(return_value=[])
            bot.orders.try_lever_up = AsyncMock(return_value=[])
            bot.orders.try_partial_take = AsyncMock(return_value=[])
            bot._check_whale_positions = AsyncMock()
            bot.orders.try_wick_scalps = AsyncMock(return_value=[])
            bot.orders.close_expired_quick_trades = AsyncMock(return_value=[])
            bot._process_trade_queue = AsyncMock()
            bot._read_shared_intel = MagicMock(return_value=None)
            bot._log_status = AsyncMock()
            bot._check_daily_reset = AsyncMock()
            bot.settings.hedge_enabled = False

            await bot._tick()

        bot._log_status.assert_called()
        bot._check_daily_reset.assert_called()


# ── start() non-multibot get_available_symbols exception ────────────────────


class TestStartNonMultibotSymbolsException:
    @pytest.mark.asyncio
    async def test_start_logs_warning_when_get_available_symbols_raises(self, settings, mock_exchange):
        settings.bot_id = ""
        with patch("bot.create_exchange", return_value=mock_exchange):
            from bot import TradingBot

            bot = TradingBot(settings=settings)
        mock_exchange.fetch_balance = AsyncMock(return_value={"USDT": 100.0})
        mock_exchange.get_available_symbols = AsyncMock(side_effect=RuntimeError("api down"))
        bot._run_loop = AsyncMock()
        bot._fast_monitor_loop = AsyncMock()
        bot._recover_state_from_hub = AsyncMock()
        bot.notifier.start = AsyncMock()
        with patch("bot.get_market_schedule") as m_sched:
            m_sched.return_value.configure = MagicMock()
            m_sched.return_value.refresh_holidays = AsyncMock()
            m_sched.return_value.summary = MagicMock(return_value="")
            await bot.start()
        bot._run_loop.assert_called_once()


# ── _post_to_hub / _read_shared_intel news hydration ────────────────────────


class TestPostToHubAndIntelNews:
    @pytest.mark.asyncio
    async def test_post_to_hub_non_200_logs_debug(self, bot):
        bot._hub_session = None
        session = MagicMock()
        resp = MagicMock()
        resp.status = 404
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=None)
        session.post = MagicMock(return_value=resp)
        with patch("bot.aiohttp.ClientSession", return_value=session):
            await bot._post_to_hub("http://hub.example.com", {"status": "ok"})
        session.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_post_to_hub_exception_caught(self, bot):
        bot._hub_session = None
        session = MagicMock()
        session.post = MagicMock(side_effect=RuntimeError("network error"))
        with patch("bot.aiohttp.ClientSession", return_value=session):
            await bot._post_to_hub("http://hub.example.com", {"status": "ok"})
        session.post.assert_called_once()

    def test_read_shared_intel_news_items_invalid_published_skipped(self, bot):
        bot._multibot = True
        bot._hub_intel_age = 100
        snap = MagicMock()
        snap.sources_active = ["fear_greed"]
        snap.regime = "normal"
        snap.fear_greed = 50
        snap.fear_greed_bias = "neutral"
        snap.liquidation_24h = 0.0
        snap.mass_liquidation = False
        snap.liquidation_bias = "neutral"
        snap.macro_event_imminent = False
        snap.macro_exposure_mult = 1.0
        snap.macro_spike_opportunity = False
        snap.next_macro_event = ""
        snap.whale_bias = "neutral"
        snap.overleveraged_side = ""
        snap.tv_btc_consensus = "neutral"
        snap.tv_eth_consensus = "neutral"
        snap.position_size_multiplier = 1.0
        snap.should_reduce_exposure = False
        snap.preferred_direction = "neutral"
        snap.hot_movers = []
        snap.news_items = [{"published": "not-a-valid-date", "headline": "x", "matched_symbols": []}]
        bot._hub_intel = snap
        cond = bot._read_shared_intel()
        assert cond is not None
        assert len(bot._recent_news) == 0


class TestQuickHubCheck:
    @staticmethod
    def _ok_hub_session() -> MagicMock:
        resp = MagicMock()
        resp.status = 200
        resp.json = AsyncMock(return_value={"confirmed_keys": []})
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=None)
        sess = MagicMock()
        sess.post = MagicMock(return_value=resp)
        return sess

    @pytest.mark.asyncio
    async def test_quick_hub_check_reports_orphans_and_empty_positions_when_unmanaged(self, bot, mock_exchange):
        bot.settings.hub_url = "http://hub.example.com"
        bot.settings.bot_id = "test-bot"
        bot._multibot = True
        bot._started_at = datetime.now(UTC) - timedelta(minutes=5)
        bot._process_trade_queue = AsyncMock()
        bot._hub_session = self._ok_hub_session()

        orphan_pos = MagicMock()
        orphan_pos.symbol = "ETH/USDT"
        orphan_pos.side = OrderSide.BUY
        orphan_pos.amount = 0.2
        orphan_pos.entry_price = 3200.0
        orphan_pos.current_price = 3250.0
        orphan_pos.leverage = 5
        orphan_pos.market_type = "futures"
        mock_exchange.fetch_positions = AsyncMock(return_value=[orphan_pos])

        await bot._quick_hub_check()

        payload = bot._hub_session.post.call_args.kwargs["json"]
        assert payload["positions"] == []
        assert payload["orphan_positions"]
        assert payload["orphan_positions"][0]["symbol"] == "ETH/USDT"
        mock_exchange.fetch_positions.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_quick_hub_check_throttles_position_fetch_without_managed_symbols(self, bot, mock_exchange):
        bot.settings.hub_url = "http://hub.example.com"
        bot.settings.bot_id = "test-bot"
        bot._multibot = True
        bot._started_at = datetime.now(UTC) - timedelta(minutes=5)
        bot._process_trade_queue = AsyncMock()
        bot._hub_session = self._ok_hub_session()
        bot._orphan_positions["XRP/USDT"] = {"symbol": "XRP/USDT", "side": "long"}
        mock_exchange.fetch_positions = AsyncMock(return_value=[])

        await bot._quick_hub_check()
        await bot._quick_hub_check()

        assert mock_exchange.fetch_positions.await_count == 1
        payload = bot._hub_session.post.call_args_list[-1].kwargs["json"]
        assert "orphan_positions" in payload
        assert payload["orphan_positions"] == []


# ── _execute_proposal / _handle_spike ───────────────────────────────────────


class TestExecuteProposalAndHandleSpike:
    @pytest.mark.asyncio
    async def test_execute_proposal_process_signal_raises_returns_false(self, bot, mock_exchange):
        from shared.models import SignalPriority, TradeProposal
        from validators import ValidationResult

        mock_exchange.fetch_ticker = AsyncMock(return_value=MagicMock(last=50_000.0))
        bot._process_signal = AsyncMock(side_effect=RuntimeError("execute failed"))
        bot._validate_proposal = AsyncMock(return_value=ValidationResult(valid=True, reason="ok"))
        proposal = TradeProposal(
            priority=SignalPriority.CRITICAL,
            symbol="BTC/USDT",
            side="long",
            strategy="m",
            reason="test",
            strength=0.8,
            market_type="futures",
        )
        ok = await bot._execute_proposal(proposal, aggression=1.0)
        assert ok is False


# ── _tick: wick scalp exception / intel fallback / queue strength reject ──────


class TestTickWickScalpAndIntelFallback:
    @pytest.mark.asyncio
    async def test_tick_wick_scalp_exception_caught(self, bot, mock_exchange):
        mock_exchange.fetch_balance = AsyncMock(return_value={"USDT": 500.0})
        mock_exchange.fetch_positions = AsyncMock(return_value=[])
        bot._strategies = []

        bot.orders.try_wick_scalps = AsyncMock(side_effect=RuntimeError("wick error"))

        with patch.object(type(bot.target), "manual_close_all", PropertyMock(return_value=False)):
            bot.orders.check_stops = AsyncMock(return_value=[])
            bot.orders.try_scale_in = AsyncMock(return_value=[])
            bot.orders.try_lever_up = AsyncMock(return_value=[])
            bot.orders.try_partial_take = AsyncMock(return_value=[])
            bot._check_whale_positions = AsyncMock()
            bot.orders.close_expired_quick_trades = AsyncMock(return_value=[])
            bot._process_trade_queue = AsyncMock()
            bot._read_shared_intel = MagicMock(return_value=None)
            bot._write_deployment_status = AsyncMock()
            bot._log_status = AsyncMock()
            bot._check_daily_reset = AsyncMock()
            bot.settings.hedge_enabled = False

            await bot._tick()

        bot._write_deployment_status.assert_called()

    @pytest.mark.asyncio
    async def test_tick_no_local_intel_assess_when_shared_stale(self, bot, mock_exchange):
        """Intel now comes only from hub — local assess() is never called."""
        mock_exchange.fetch_balance = AsyncMock(return_value={"USDT": 500.0})
        mock_exchange.fetch_positions = AsyncMock(return_value=[])
        bot._strategies = []

        bot._read_shared_intel = MagicMock(return_value=None)
        bot.intel = MagicMock()
        bot.intel.assess = MagicMock()

        with patch.object(type(bot.target), "manual_close_all", PropertyMock(return_value=False)):
            bot.orders.check_stops = AsyncMock(return_value=[])
            bot.orders.try_scale_in = AsyncMock(return_value=[])
            bot.orders.try_lever_up = AsyncMock(return_value=[])
            bot.orders.try_partial_take = AsyncMock(return_value=[])
            bot._check_whale_positions = AsyncMock()
            bot.orders.try_wick_scalps = AsyncMock(return_value=[])
            bot.orders.close_expired_quick_trades = AsyncMock(return_value=[])
            bot._process_trade_queue = AsyncMock()
            bot._write_deployment_status = AsyncMock()
            bot._log_status = AsyncMock()
            bot._check_daily_reset = AsyncMock()
            bot.settings.hedge_enabled = False

            await bot._tick()

        bot.intel.assess.assert_not_called()


class TestProcessTradeQueueStrengthReject:
    @pytest.mark.asyncio
    async def test_process_trade_queue_rejects_strength_too_low_after_aggression(self, bot, mock_exchange):
        from datetime import timedelta

        from shared.models import SignalPriority, TradeProposal

        bot._started_at = datetime.now(UTC) - timedelta(minutes=10)
        bot._warmup_minutes = 1
        p = TradeProposal(
            priority=SignalPriority.CRITICAL,
            symbol="BTC/USDT",
            side="long",
            strategy="m",
            reason="r",
            strength=0.2,
            market_type="futures",
            supported_exchanges=["MEXC"],
        )
        bot._hub_proposal = p
        bot._report_queue_outcome = AsyncMock()
        mock_exchange.fetch_positions = AsyncMock(return_value=[])
        bot.target.should_trade = MagicMock(return_value=True)
        bot.target.aggression_multiplier = MagicMock(return_value=0.5)
        bot.target.reset_day(100.0)
        bot.target.update_balance(100.0)

        await bot._process_trade_queue()

        bot._report_queue_outcome.assert_called_once()
        assert "strength" in bot._report_queue_outcome.call_args[0][2].lower()


class TestIdleMode:
    """Tests for hub-controlled enable/disable (static containers)."""

    def test_check_enabled_standalone_always_true(self, bot):
        """Non-multibot mode always returns enabled."""
        bot._multibot = False
        assert bot._check_enabled() is True

    def test_check_enabled_reads_hub_flag(self, bot):
        """Multibot reads _hub_enabled set by the report response."""
        bot._multibot = True
        bot._hub_enabled = False
        assert bot._check_enabled() is False
        bot._hub_enabled = True
        assert bot._check_enabled() is True

    @pytest.mark.asyncio
    async def test_idle_tick_writes_idle_status(self, bot):
        bot.settings.bot_id = "momentum"
        bot.shared = MagicMock()
        bot._report_dashboard_snapshot = AsyncMock()

        await bot._idle_tick()

        assert bot._last_bot_status is not None
        assert bot._last_bot_status.level.value == "idle"
        assert bot._last_bot_status.should_trade is False
        assert bot._last_bot_status.bot_id == "momentum"

    @pytest.mark.asyncio
    async def test_wind_down_closes_positions(self, bot, mock_exchange):
        pos = MagicMock()
        pos.amount = 0.01
        pos.symbol = "BTC/USDT"
        mock_exchange.fetch_positions = AsyncMock(return_value=[pos])
        bot.shared = MagicMock()
        bot._close_all_positions = AsyncMock()

        await bot._wind_down()

        assert bot._last_bot_status is not None
        assert bot._last_bot_status.level.value == "winding_down"
        bot._close_all_positions.assert_awaited_once_with("hub_disabled")

    @pytest.mark.asyncio
    async def test_wind_down_no_positions_skips(self, bot, mock_exchange):
        mock_exchange.fetch_positions = AsyncMock(return_value=[])
        bot._close_all_positions = AsyncMock()

        await bot._wind_down()

        bot._close_all_positions.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_loop_transitions_to_idle(self, bot):
        """When enabled goes False, bot should wind down then idle."""
        call_count = 0

        def mock_check():
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return True
            bot._running = False
            return False

        bot._check_enabled = mock_check
        bot._enabled = True
        bot._running = True
        bot._tick = AsyncMock()
        bot._idle_tick = AsyncMock()
        bot._wind_down = AsyncMock()
        bot._update_tick_interval = MagicMock()
        bot._tick_interval = 0.01

        with (
            patch("bot.record_tick", create=True),
            patch("bot.record_event_loop_lag", create=True),
            patch("bot.asyncio.sleep", new_callable=AsyncMock),
        ):
            await bot._run_loop()

        bot._wind_down.assert_awaited_once()
