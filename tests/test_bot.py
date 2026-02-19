"""Tests for bot.py — TradingBot orchestrator."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from config.settings import get_settings
from core.models import Signal, SignalAction
from core.models.order import Order, OrderSide, OrderStatus, OrderType
from core.orders.scaler import ScaleMode
from intel import MarketCondition

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
def mock_trade_db():
    db = MagicMock()
    db.connect = MagicMock()
    db.close = MagicMock()
    db.log_trade = MagicMock()
    db.trade_count = MagicMock(return_value=0)
    return db


@pytest.fixture
def bot(settings, mock_exchange, mock_trade_db):
    """TradingBot with mocked exchange and DB."""
    with patch("bot.create_exchange", return_value=mock_exchange), patch("bot.TradeDB", return_value=mock_trade_db):
        from bot import TradingBot

        return TradingBot(settings=settings, daily_target_pct=10.0)


# ── __init__ ────────────────────────────────────────────────────────────────


class TestTradingBotInit:
    def test_init_creates_subsystems(self, bot, mock_exchange, mock_trade_db):
        assert bot.exchange is mock_exchange
        assert bot.trade_db is mock_trade_db
        assert bot.risk is not None
        assert bot.orders is not None
        assert bot.notifier is not None
        assert bot.volatility is not None
        assert bot.news is not None
        assert bot.target is not None
        assert bot.market_filter is not None
        assert bot.scanner is not None
        assert bot.analytics is not None
        assert bot.shared is not None
        assert bot._strategies == []
        assert bot._dynamic_strategies == {}
        assert bot._running is False
        mock_trade_db.connect.assert_called_once()

    def test_init_with_intel_disabled_sets_intel_none(self, settings, mock_exchange, mock_trade_db):
        settings.intel_enabled = False
        with patch("bot.create_exchange", return_value=mock_exchange):
            with patch("bot.TradeDB", return_value=mock_trade_db):
                from bot import TradingBot

                b = TradingBot(settings=settings)
        assert b.intel is None

    def test_init_with_intel_enabled_creates_intel(self, mock_exchange, mock_trade_db):
        with patch("bot.create_exchange", return_value=mock_exchange):
            with patch("bot.TradeDB", return_value=mock_trade_db):
                with patch("bot.MarketIntel") as m_intel:
                    m_intel.return_value.start = AsyncMock()
                    m_intel.return_value.stop = AsyncMock()
                    m_intel.return_value.assess = MagicMock(return_value=None)
                    m_intel.return_value.full_summary = MagicMock(return_value="")
                    m_intel.return_value.tradingview = MagicMock()
                    m_intel.return_value.tradingview.analyze_multi = AsyncMock()
                    s = get_settings()
                    s.intel_enabled = True
                    from bot import TradingBot

                    b = TradingBot(settings=s)
        assert b.intel is not None
        m_intel.assert_called_once()


# ── add_strategy / add_custom_strategy ──────────────────────────────────────


class TestTradingBotStrategyManagement:
    def test_add_strategy_registers_builtin(self, bot):
        bot.add_strategy("compound_momentum", "BTC/USDT", market_type="spot")
        assert len(bot._strategies) == 1
        assert bot._strategies[0].symbol == "BTC/USDT"
        assert bot._strategies[0].name == "compound_momentum"

    def test_add_strategy_unknown_raises(self, bot):
        with pytest.raises(ValueError, match="Unknown strategy"):
            bot.add_strategy("unknown_strat", "BTC/USDT")

    def test_add_strategy_market_type_not_allowed_fallback_to_spot(self, settings, mock_exchange, mock_trade_db):
        settings.allowed_market_types = "spot"
        with patch("bot.create_exchange", return_value=mock_exchange):
            with patch("bot.TradeDB", return_value=mock_trade_db):
                from bot import TradingBot

                b = TradingBot(settings=settings)
                b.add_strategy("compound_momentum", "BTC/USDT", market_type="futures")
                assert len(b._strategies) == 1
                assert b._strategies[0].market_type == "spot"

    def test_add_strategy_market_type_not_allowed_skip_when_no_fallback(self, settings, mock_exchange, mock_trade_db):
        settings.allowed_market_types = "futures"
        with patch("bot.create_exchange", return_value=mock_exchange):
            with patch("bot.TradeDB", return_value=mock_trade_db):
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


# ── _apply_intel_to_signal ───────────────────────────────────────────────────


class TestApplyIntelToSignal:
    def test_neutral_preferred_direction_returns_unchanged(self, bot):
        cond = MarketCondition(preferred_direction="neutral")
        sig = Signal(
            symbol="BTC/USDT",
            action=SignalAction.BUY,
            strength=0.8,
            strategy="compound_momentum",
            reason="test",
            market_type="futures",
        )
        out = bot._apply_intel_to_signal(sig, cond)
        assert out.strength == 0.8

    def test_mass_liquidation_against_bias_blocks_signal(self, bot):
        cond = MarketCondition(
            preferred_direction="long",
            mass_liquidation=True,
        )
        sig = Signal(
            symbol="BTC/USDT",
            action=SignalAction.SELL,
            strength=0.8,
            strategy="compound_momentum",
            reason="test",
            market_type="futures",
        )
        out = bot._apply_intel_to_signal(sig, cond)
        assert out.strength == 0

    def test_fear_greed_extreme_reduces_strength_short_when_fear(self, bot):
        cond = MarketCondition(preferred_direction="long", fear_greed=20)
        sig = Signal(
            symbol="BTC/USDT",
            action=SignalAction.SELL,
            strength=0.8,
            strategy="compound_momentum",
            reason="test",
            market_type="futures",
        )
        out = bot._apply_intel_to_signal(sig, cond)
        assert out.strength == 0.4  # 0.8 * 0.5

    def test_fear_greed_extreme_reduces_strength_long_when_greed(self, bot):
        cond = MarketCondition(preferred_direction="short", fear_greed=80)
        sig = Signal(
            symbol="BTC/USDT",
            action=SignalAction.BUY,
            strength=0.8,
            strategy="compound_momentum",
            reason="test",
            market_type="futures",
        )
        out = bot._apply_intel_to_signal(sig, cond)
        assert out.strength == 0.4

    def test_overleveraged_longs_caution_on_long_signal(self, bot):
        cond = MarketCondition(overleveraged_side="longs", preferred_direction="short")
        sig = Signal(
            symbol="BTC/USDT",
            action=SignalAction.BUY,
            strength=0.8,
            strategy="compound_momentum",
            reason="test",
            market_type="futures",
        )
        out = bot._apply_intel_to_signal(sig, cond)
        assert out.strength == pytest.approx(0.8 * 0.7)

    def test_overleveraged_shorts_caution_on_short_signal(self, bot):
        cond = MarketCondition(overleveraged_side="shorts", preferred_direction="long")
        sig = Signal(
            symbol="BTC/USDT",
            action=SignalAction.SELL,
            strength=0.8,
            strategy="compound_momentum",
            reason="test",
            market_type="futures",
        )
        out = bot._apply_intel_to_signal(sig, cond)
        assert out.strength == pytest.approx(0.8 * 0.7)

    def test_aligned_with_intel_boosts_strength(self, bot):
        cond = MarketCondition(preferred_direction="long")
        sig = Signal(
            symbol="BTC/USDT",
            action=SignalAction.BUY,
            strength=0.8,
            strategy="compound_momentum",
            reason="test",
            market_type="futures",
        )
        out = bot._apply_intel_to_signal(sig, cond)
        assert out.strength == pytest.approx(min(1.0, 0.8 * 1.15))


# ── _log_closed_trade ───────────────────────────────────────────────────────


class TestLogClosedTrade:
    def test_log_closed_trade_writes_record(self, bot, mock_trade_db):
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
        sp.adds = 2
        sp.low_liquidity = False
        sp.avg_entry_price = 49_500.0
        sp.current_leverage = 10
        bot.orders.scaler.get = MagicMock(return_value=sp)
        bot.intel = None

        bot._log_closed_trade(order, "stop")

        mock_trade_db.log_trade.assert_called_once()
        record = mock_trade_db.log_trade.call_args[0][0]
        assert record.symbol == "BTC/USDT"
        assert record.strategy == "compound_momentum"
        assert record.action == "close"
        assert record.scale_mode == "pyramid"
        assert record.dca_count == 2
        assert record.entry_price == 49_500.0
        assert record.exit_price == 50_100.0
        assert record.pnl_usd > 0
        assert record.is_winner is True

    def test_log_closed_trade_discards_whale_alerted(self, bot, mock_trade_db):
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
        bot.notifier.send_daily_summary = AsyncMock()
        with patch("bot.datetime") as m_dt:
            m_dt.now.return_value = datetime(2025, 2, 20, 0, 1, tzinfo=UTC)
            await bot._check_daily_reset()
        mock_exchange.fetch_balance.assert_called()
        bot.notifier.send_daily_summary.assert_called_once()


# ── _log_status ─────────────────────────────────────────────────────────────


class TestLogStatus:
    @pytest.mark.asyncio
    async def test_log_status_throttles_by_interval(self, bot):
        bot._last_status_log = datetime.now(UTC)
        bot.target.status_report = MagicMock(return_value="status")
        bot.risk.risk_summary = MagicMock(return_value="risk")
        bot.scanner.scan_summary = MagicMock(return_value="scan")
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
        bot.scanner.scan_summary = MagicMock(return_value="scan")
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
    def test_read_shared_intel_returns_none_when_stale(self, bot):
        bot.shared.intel_age_seconds = MagicMock(return_value=700)
        assert bot._read_shared_intel() is None

    def test_read_shared_intel_returns_none_when_sources_inactive(self, bot):
        bot.shared.intel_age_seconds = MagicMock(return_value=100)
        snap = MagicMock()
        snap.sources_active = []
        bot.shared.read_intel = MagicMock(return_value=snap)
        assert bot._read_shared_intel() is None

    def test_read_shared_intel_returns_condition_when_fresh(self, bot):
        bot.shared.intel_age_seconds = MagicMock(return_value=100)
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
        bot.shared.read_intel = MagicMock(return_value=snap)
        cond = bot._read_shared_intel()
        assert cond is not None
        assert cond.preferred_direction == "neutral"

    def test_read_shared_analytics_weight_fallback_to_engine(self, bot):
        bot.shared.read_analytics = MagicMock(return_value=MagicMock(weights=[]))
        bot.analytics.get_weight = MagicMock(return_value=0.7)
        w = bot._read_shared_analytics_weight("compound_momentum")
        assert w == 0.7

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

    def test_adjust_for_target_reduces_when_target_reached(self, bot):
        from core.risk.daily_target import DailyTargetTracker

        with patch.object(DailyTargetTracker, "target_reached", new_callable=PropertyMock, return_value=True):
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


# ── _get_tv_boost ───────────────────────────────────────────────────────────


class TestGetTVBoost:
    def test_get_tv_boost_from_shared_state(self, bot):
        tv = MagicMock()
        tv.symbol = "BTC/USDT"
        tv.interval = "1h"
        tv.signal_boost_long = 1.2
        tv.signal_boost_short = 0.8
        snap = MagicMock()
        snap.tv_analyses = [tv]
        bot.shared.read_intel = MagicMock(return_value=snap)
        assert bot._get_tv_boost("BTC/USDT", "long") == 1.2
        assert bot._get_tv_boost("BTC/USDT", "short") == 0.8

    def test_get_tv_boost_returns_one_when_no_match(self, bot):
        bot.shared.read_intel = MagicMock(return_value=MagicMock(tv_analyses=[]))
        bot.intel = None
        assert bot._get_tv_boost("BTC/USDT", "long") == 1.0


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
        bot.shared.read_trade_queue = MagicMock(return_value=MagicMock(pending_count=0))
        await bot._process_trade_queue()
        bot.exchange.fetch_positions.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_trade_queue_read_exception_returns_early(self, bot):
        bot.shared.read_trade_queue = MagicMock(side_effect=RuntimeError("read failed"))
        await bot._process_trade_queue()
        # no exception, early return
        bot.exchange.fetch_positions.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_proposal_success(self, bot, mock_exchange):
        from shared.models import SignalPriority, TradeProposal

        mock_exchange.fetch_ticker = AsyncMock(return_value=MagicMock(last=50_000.0))
        bot._process_signal = AsyncMock()
        proposal = TradeProposal(
            priority=SignalPriority.CRITICAL,
            symbol="BTC/USDT",
            side="long",
            strategy="momentum",
            reason="test",
            strength=0.8,
            market_type="futures",
        )
        ok = await bot._execute_proposal(proposal, aggression=0.8)
        assert ok is True
        bot._process_signal.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_proposal_fetch_ticker_fails(self, bot, mock_exchange):
        from shared.models import SignalPriority, TradeProposal

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

        mock_exchange.fetch_ticker = AsyncMock(return_value=MagicMock(last=50_000.0))
        bot._process_signal = AsyncMock()
        proposal = TradeProposal(
            priority=SignalPriority.SWING,
            symbol="ETH/USDT",
            side="long",
            strategy="swing",
            reason="swing setup",
            strength=0.6,
            market_type="futures",
            entry_plan=EntryPlan(stop_loss=2000.0, take_profit_targets=[2500.0]),
        )
        ok = await bot._execute_swing_proposal(proposal, aggression=0.7)
        assert ok is True
        bot._process_signal.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_swing_proposal_no_plan(self, bot, mock_exchange):
        from shared.models import SignalPriority, TradeProposal

        mock_exchange.fetch_ticker = AsyncMock(return_value=MagicMock(last=2000.0))
        bot._process_signal = AsyncMock()
        proposal = TradeProposal(
            priority=SignalPriority.SWING,
            symbol="ETH/USDT",
            side="long",
            strategy="swing",
            reason="",
            strength=0.5,
            market_type="futures",
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
        bot.shared.write_bot_status = MagicMock()
        await bot._write_deployment_status()
        bot.shared.write_bot_status.assert_called_once()
        status = bot.shared.write_bot_status.call_args[0][0]
        assert status.open_positions == 0
        assert hasattr(status, "daily_pnl_pct")


# ── _handle_spike / _on_trending / _on_news ──────────────────────────────────


class TestHandleSpikeAndCallbacks:
    @pytest.mark.asyncio
    async def test_handle_spike_alerts_and_checks_news(self, bot):
        from volatility import SpikeEvent

        bot.notifier.alert_spike = AsyncMock()
        bot.news.correlate_spike = MagicMock(return_value=None)
        spike = SpikeEvent(
            symbol="BTC/USDT", change_pct=5.0, direction="up", price=52_000.0, volume_24h=1e9, window_seconds=60
        )
        await bot._handle_spike(spike)
        bot.notifier.alert_spike.assert_called_once_with("BTC/USDT", 5.0, "up", 52_000.0)
        bot.news.correlate_spike.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_trending_removes_dynamic_strategy_when_not_in_list(self, bot, mock_exchange):
        mock_exchange.get_available_symbols = AsyncMock(return_value=["BTC/USDT", "ETH/USDT"])
        bot._dynamic_strategies["XRP/USDT"] = MagicMock()
        from scanner import TrendingCoin

        movers = [TrendingCoin(symbol="BTC/USDT", change_1h=1.0, change_24h=2.0, volume_24h=10e6, market_cap=100e6)]
        await bot._on_trending(movers)
        assert "XRP/USDT" not in bot._dynamic_strategies

    @pytest.mark.asyncio
    async def test_on_news_appends_and_alerts_when_matched(self, bot):
        from news import NewsItem

        bot.notifier.alert_news = AsyncMock()
        item = NewsItem(
            headline="BTC surge",
            source="test",
            url="",
            published=datetime.now(UTC),
            matched_symbols=["BTC/USDT"],
            sentiment_score=0.5,
        )
        await bot._on_news(item)
        assert len(bot._recent_news) == 1
        bot.notifier.alert_news.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_news_caps_recent_news_list(self, bot):
        from news import NewsItem

        bot.notifier.alert_news = AsyncMock()
        for i in range(250):
            await bot._on_news(
                NewsItem(
                    headline=f"News {i}",
                    source="test",
                    url="",
                    published=datetime.now(UTC),
                    matched_symbols=[],
                    sentiment_score=0.0,
                )
            )
        assert len(bot._recent_news) <= 200


# ── _process_trade_queue with queue content / _read_shared_analytics ────────


class TestProcessTradeQueueWithProposals:
    def test_read_shared_analytics_weight_from_shared_snapshot(self, bot):
        from shared.models import StrategyWeightEntry

        snap = MagicMock()
        snap.weights = [StrategyWeightEntry(strategy="compound_momentum", weight=0.6)]
        bot.shared.read_analytics = MagicMock(return_value=snap)
        w = bot._read_shared_analytics_weight("compound_momentum")
        assert w == 0.6

    def test_read_shared_analytics_weight_strategy_not_in_snapshot_returns_one(self, bot):
        from shared.models import StrategyWeightEntry

        snap = MagicMock()
        snap.weights = [StrategyWeightEntry(strategy="other", weight=0.5)]
        bot.shared.read_analytics = MagicMock(return_value=snap)
        w = bot._read_shared_analytics_weight("compound_momentum")
        assert w == 1.0
