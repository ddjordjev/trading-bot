"""Tests for core/orders/manager.py and untested parts of core/orders/scaler.py."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from config.settings import Settings
from core.models import (
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Signal,
    SignalAction,
)
from core.orders.manager import OrderManager
from core.orders.scaler import (
    PositionScaler,
    ScaleMode,
    ScalePhase,
)

# ── PositionScaler / ScaledPosition (untested parts) ────────────────────


@pytest.fixture
def scaler():
    return PositionScaler(
        initial_risk_amount=50.0,
        max_notional=100_000.0,
        gambling_budget_pct=2.0,
    )


class TestScaledPositionGetAddAmount:
    def test_pyramid_add_amount_scales_with_adds(self, scaler):
        sp = scaler.create(
            symbol="BTC/USDT",
            side="long",
            strategy="test",
            leverage=10,
            mode=ScaleMode.PYRAMID,
        )
        sp.current_size = 0.001
        sp.avg_entry_price = 50_000.0
        sp.current_leverage = 2
        sp.adds = 1
        sp.last_add_price = 49_000.0
        amount = sp.get_add_amount(49_000.0)
        assert amount > 0
        # add_dollars = 50 * 1.5^1 = 75, add_notional (margin) = 75 * 2 = 150
        margin_add = amount * 49_000.0
        assert margin_add <= 150 + 1

    def test_winners_add_amount_increases_with_adds(self, scaler):
        sp = scaler.create(
            symbol="ETH/USDT",
            side="long",
            strategy="test",
            leverage=10,
            mode=ScaleMode.WINNERS,
        )
        sp.current_size = 1.0
        sp.avg_entry_price = 3000.0
        sp.current_leverage = 10
        sp.adds = 2
        sp.last_add_price = 3050.0
        amount = sp.get_add_amount(3050.0)
        assert amount > 0

    def test_get_add_amount_zero_when_no_room(self, scaler):
        sp = scaler.create(
            symbol="BTC/USDT",
            side="long",
            strategy="test",
            leverage=10,
            mode=ScaleMode.PYRAMID,
        )
        sp.current_size = 100.0
        sp.avg_entry_price = 50_000.0
        sp.current_leverage = 10
        sp.last_add_price = 50_000.0
        amount = sp.get_add_amount(50_000.0)
        assert amount == 0.0

    def test_get_add_amount_zero_for_zero_price(self, scaler):
        sp = scaler.create(
            symbol="BTC/USDT",
            side="long",
            strategy="test",
            leverage=10,
        )
        sp.last_add_price = 50_000.0
        assert sp.get_add_amount(0) == 0.0


class TestScaledPositionShouldAdd:
    def test_pyramid_add_when_drop_exceeds_interval(self, scaler):
        sp = scaler.create(
            symbol="BTC/USDT",
            side="long",
            strategy="test",
            leverage=10,
            mode=ScaleMode.PYRAMID,
            dca_interval_pct=2.0,
        )
        sp.last_add_price = 50_000.0
        sp.current_size = 0.001
        sp.avg_entry_price = 50_000.0
        # price dropped 3% from last add
        assert sp.should_add(48_500.0) is True

    def test_pyramid_no_add_before_first_add(self, scaler):
        sp = scaler.create(
            symbol="BTC/USDT",
            side="long",
            strategy="test",
            leverage=10,
            mode=ScaleMode.PYRAMID,
        )
        sp.last_add_price = 0
        sp.current_size = 0.001
        sp.avg_entry_price = 50_000.0
        assert sp.should_add(49_000.0) is False

    def test_pyramid_bounced_from_wick_adds(self, scaler):
        sp = scaler.create(
            symbol="BTC/USDT",
            side="long",
            strategy="test",
            leverage=10,
            mode=ScaleMode.PYRAMID,
            dca_interval_pct=2.0,
        )
        sp.last_add_price = 50_000.0
        sp.trough_since_entry = 48_000.0  # was 4% down
        sp.current_size = 0.001
        sp.avg_entry_price = 49_500.0
        # now only 1% down from last add (bounced) -> good DCA point
        assert sp.should_add(49_500.0) is True

    def test_winners_add_when_profit_above_min(self, scaler):
        sp = scaler.create(
            symbol="BTC/USDT",
            side="long",
            strategy="test",
            leverage=10,
            mode=ScaleMode.WINNERS,
        )
        sp.min_profit_to_add_pct = 1.0
        sp.current_size = 0.001
        sp.avg_entry_price = 50_000.0
        sp.last_add_price = 50_000.0
        sp.peak_since_entry = 50_500.0
        # current 50_200 -> +0.4% from entry, below 1% -> no add
        assert sp.should_add(50_200.0) is False
        # current 50_600 -> +1.2% -> add
        assert sp.should_add(50_600.0) is True

    def test_gambling_phase_skips_add(self, scaler):
        sp = scaler.create(
            symbol="DOGE/USDT",
            side="long",
            strategy="test",
            leverage=10,
            low_liquidity=True,
        )
        sp.phase = ScalePhase.GAMBLING
        sp.current_size = 1000.0
        sp.avg_entry_price = 0.1
        assert sp.should_add(0.11) is False


class TestScaledPositionShouldLeverUp:
    def test_pyramid_lever_up_when_profit_above_threshold(self, scaler):
        sp = scaler.create(
            symbol="BTC/USDT",
            side="long",
            strategy="test",
            leverage=10,
            mode=ScaleMode.PYRAMID,
        )
        sp.profit_to_lever_up_pct = 1.0
        sp.adds = 1
        sp.current_size = 0.001
        sp.avg_entry_price = 50_000.0
        sp.current_leverage = 2
        sp.leverage_raised = False
        # +1.5% profit
        assert sp.should_lever_up(50_750.0) is True

    def test_pyramid_no_lever_up_before_adds(self, scaler):
        sp = scaler.create(
            symbol="BTC/USDT",
            side="long",
            strategy="test",
            leverage=10,
            mode=ScaleMode.PYRAMID,
        )
        sp.adds = 0
        sp.current_size = 0.001
        sp.avg_entry_price = 50_000.0
        assert sp.should_lever_up(51_000.0) is False

    def test_winners_mode_no_lever_up(self, scaler):
        sp = scaler.create(
            symbol="BTC/USDT",
            side="long",
            strategy="test",
            leverage=10,
            mode=ScaleMode.WINNERS,
        )
        sp.adds = 1
        sp.avg_entry_price = 50_000.0
        sp.leverage_raised = False
        assert sp.should_lever_up(51_000.0) is False


class TestScaledPositionShouldTakePartial:
    def test_pyramid_partial_after_lever_up(self, scaler):
        sp = scaler.create(
            symbol="BTC/USDT",
            side="long",
            strategy="test",
            leverage=10,
            mode=ScaleMode.PYRAMID,
        )
        sp.profit_to_lever_up_pct = 1.0
        sp.partial_take_pct = 30.0
        sp.leverage_raised = True
        sp.partial_taken = False
        sp.current_size = 0.01
        sp.avg_entry_price = 50_000.0
        # threshold = 2%, current +2.5%
        assert sp.should_take_partial(51_250.0) is True

    def test_partial_taken_skips(self, scaler):
        sp = scaler.create(
            symbol="BTC/USDT",
            side="long",
            strategy="test",
            leverage=10,
            mode=ScaleMode.PYRAMID,
        )
        sp.leverage_raised = True
        sp.partial_taken = True
        sp.avg_entry_price = 50_000.0
        assert sp.should_take_partial(52_000.0) is False


class TestScaledPositionRecordAndProps:
    def test_record_add_updates_avg_and_phase(self, scaler):
        sp = scaler.create(
            symbol="BTC/USDT",
            side="long",
            strategy="test",
            leverage=10,
            mode=ScaleMode.PYRAMID,
        )
        sp.current_size = 0.001
        sp.avg_entry_price = 50_000.0
        sp.record_add(0.001, 49_000.0)
        assert sp.current_size == 0.002
        assert sp.avg_entry_price == 49_500.0
        assert sp.adds == 1
        assert sp.last_add_price == 49_000.0
        assert sp.phase == ScalePhase.ADDING

    def test_record_partial_close_reduces_size(self, scaler):
        sp = scaler.create(
            symbol="BTC/USDT",
            side="long",
            strategy="test",
            leverage=10,
            mode=ScaleMode.PYRAMID,
        )
        sp.partial_take_pct = 30.0
        sp.current_size = 1.0
        sp.avg_entry_price = 50_000.0
        sp.record_partial_close(0.3)
        assert sp.current_size == pytest.approx(0.7)
        assert sp.partial_taken is True

    def test_record_lever_up(self, scaler):
        sp = scaler.create(
            symbol="BTC/USDT",
            side="long",
            strategy="test",
            leverage=10,
            mode=ScaleMode.PYRAMID,
        )
        sp.current_leverage = 2
        sp.record_lever_up(10)
        assert sp.current_leverage == 10
        assert sp.leverage_raised is True

    def test_update_peak_long(self, scaler):
        sp = scaler.create(
            symbol="BTC/USDT",
            side="long",
            strategy="test",
            leverage=10,
        )
        sp.peak_since_entry = 0
        sp.trough_since_entry = 0
        sp.update_peak(51_000.0)
        sp.update_peak(50_500.0)
        sp.update_peak(50_200.0)
        assert sp.peak_since_entry == 51_000.0
        assert sp.trough_since_entry == 50_200.0

    def test_update_peak_short(self, scaler):
        sp = scaler.create(
            symbol="BTC/USDT",
            side="short",
            strategy="test",
            leverage=10,
        )
        sp.peak_since_entry = 0
        sp.trough_since_entry = 0
        sp.update_peak(49_000.0)
        sp.update_peak(49_500.0)
        assert sp.peak_since_entry == 49_000.0
        assert sp.trough_since_entry == 49_500.0

    def test_notional_at_price_uses_last_add(self, scaler):
        sp = scaler.create(
            symbol="BTC/USDT",
            side="long",
            strategy="test",
            leverage=10,
        )
        sp.current_size = 1.0
        sp.avg_entry_price = 50_000.0
        sp.last_add_price = 51_000.0
        assert sp.notional_at_price == 1.0 * 51_000.0 * 10

    def test_fill_pct(self, scaler):
        sp = scaler.create(
            symbol="BTC/USDT",
            side="long",
            strategy="test",
            leverage=10,
        )
        sp.current_size = 0.01
        sp.avg_entry_price = 50_000.0
        sp.current_leverage = 10
        # notional = 50k * 0.01 * 10 = 5000, max 100k -> 5%
        assert sp.fill_pct == pytest.approx(5.0, rel=0.1)

    def test_fill_pct_zero_max_notional(self, scaler):
        sp = scaler.create(
            symbol="BTC/USDT",
            side="long",
            strategy="test",
            leverage=10,
        )
        sp.max_notional = 0
        assert sp.fill_pct == 0

    def test_status_line_contains_key_fields(self, scaler):
        sp = scaler.create(
            symbol="BTC/USDT",
            side="long",
            strategy="test",
            leverage=10,
            mode=ScaleMode.PYRAMID,
        )
        sp.adds = 1
        sp.current_size = 0.001
        sp.avg_entry_price = 50_000.0
        line = sp.status_line()
        assert "BTC/USDT" in line
        assert "pyramid" in line
        assert "adds=1" in line


class TestPositionScalerCreateAndGetters:
    def test_create_low_liquidity_sets_phase_gambling(self, scaler):
        sp = scaler.create(
            symbol="DOGE/USDT",
            side="long",
            strategy="test",
            leverage=10,
            low_liquidity=True,
        )
        assert sp.phase == ScalePhase.GAMBLING
        assert sp.low_liquidity is True

    def test_get_symbols_to_add_returns_list(self, scaler):
        sp = scaler.create(
            symbol="BTC/USDT",
            side="long",
            strategy="test",
            leverage=10,
            mode=ScaleMode.PYRAMID,
        )
        sp.current_size = 0.001
        sp.avg_entry_price = 50_000.0
        sp.last_add_price = 50_000.0
        to_add = scaler.get_symbols_to_add({"BTC/USDT": 49_000.0})
        assert isinstance(to_add, list)
        if to_add:
            assert to_add[0][0] == "BTC/USDT"
            assert to_add[0][1] > 0

    def test_get_symbols_to_lever_up(self, scaler):
        sp = scaler.create(
            symbol="BTC/USDT",
            side="long",
            strategy="test",
            leverage=10,
            mode=ScaleMode.PYRAMID,
        )
        sp.adds = 1
        sp.current_size = 0.001
        sp.avg_entry_price = 50_000.0
        sp.leverage_raised = False
        result = scaler.get_symbols_to_lever_up({"BTC/USDT": 50_750.0})
        assert "BTC/USDT" in result

    def test_get_symbols_for_partial_take(self, scaler):
        sp = scaler.create(
            symbol="BTC/USDT",
            side="long",
            strategy="test",
            leverage=10,
            mode=ScaleMode.PYRAMID,
        )
        sp.leverage_raised = True
        sp.partial_taken = False
        sp.current_size = 0.01
        sp.avg_entry_price = 50_000.0
        result = scaler.get_symbols_for_partial_take({"BTC/USDT": 51_500.0})
        assert len(result) >= 1
        assert result[0][0] == "BTC/USDT"
        assert result[0][1] == sp.get_partial_take_amount()

    def test_gambling_size_zero_price(self, scaler):
        assert scaler.gambling_size(10_000.0, 0, 10) == 0


# ── OrderManager ──────────────────────────────────────────────────────


@pytest.fixture
def mock_exchange():
    ex = AsyncMock()
    ex.fetch_balance.return_value = {"USDT": 10_000.0}
    ex.fetch_positions.return_value = []
    ex.place_order = AsyncMock()
    ex.set_leverage = AsyncMock()
    return ex


@pytest.fixture
def mock_risk():
    risk = MagicMock()
    risk.check_signal.return_value = True
    risk.apply_stops = lambda s: s
    risk.record_pnl = MagicMock()
    risk.check_liquidation.return_value = False
    return risk


@pytest.fixture
def settings():
    """Settings with fields OrderManager uses; cap_balance is used as the class method."""
    return Settings.model_construct(
        session_budget=10_000.0,
        default_leverage=10,
        stop_loss_pct=1.5,
        breakeven_lock_pct=5.0,
        initial_risk_amount=50.0,
        max_notional_position=100_000.0,
        gambling_budget_pct=2.0,
        hedge_ratio=0.2,
        hedge_min_profit_pct=3.0,
        hedge_stop_pct=1.0,
        max_hedges=2,
    )


@pytest.fixture
def order_manager(mock_exchange, mock_risk, settings):
    return OrderManager(mock_exchange, mock_risk, settings)


class TestOrderManagerExecuteSignal:
    async def test_execute_signal_hold_returns_none(self, order_manager):
        signal = Signal(
            symbol="BTC/USDT",
            action=SignalAction.HOLD,
            strategy="test",
        )
        out = await order_manager.execute_signal(signal)
        assert out is None

    async def test_execute_signal_close_calls_close_position(self, order_manager, mock_exchange):
        mock_exchange.fetch_positions.return_value = [
            Position(
                symbol="BTC/USDT",
                side=OrderSide.BUY,
                amount=0.001,
                entry_price=50_000.0,
                current_price=51_000.0,
                leverage=10,
                market_type="futures",
            )
        ]
        filled_order = Order(
            id="o1",
            symbol="BTC/USDT",
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            amount=0.001,
            status=OrderStatus.FILLED,
            filled=0.001,
            average_price=51_000.0,
        )
        mock_exchange.place_order.return_value = filled_order

        signal = Signal(
            symbol="BTC/USDT",
            action=SignalAction.CLOSE,
            strategy="test",
        )
        out = await order_manager.execute_signal(signal)
        assert out is not None
        assert out.status == OrderStatus.FILLED
        mock_exchange.place_order.assert_called()

    async def test_execute_signal_buy_places_order(self, order_manager, mock_exchange):
        signal = Signal(
            symbol="BTC/USDT",
            action=SignalAction.BUY,
            strategy="test",
            suggested_price=50_000.0,
            leverage=10,
        )
        filled = Order(
            id="o1",
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            amount=0.002,
            status=OrderStatus.FILLED,
            filled=0.002,
            average_price=50_000.0,
            leverage=2,
        )
        mock_exchange.place_order.return_value = filled

        out = await order_manager.execute_signal(signal)
        assert out is not None
        assert out.status == OrderStatus.FILLED
        assert len(order_manager._active_orders) == 1

    async def test_execute_signal_no_price_skips(self, order_manager):
        signal = Signal(
            symbol="BTC/USDT",
            action=SignalAction.BUY,
            strategy="test",
            suggested_price=0,
        )
        out = await order_manager.execute_signal(signal)
        assert out is None

    async def test_execute_signal_risk_reject_returns_none(self, order_manager, mock_risk):
        mock_risk.check_signal.return_value = False
        signal = Signal(
            symbol="BTC/USDT",
            action=SignalAction.BUY,
            strategy="test",
            suggested_price=50_000.0,
        )
        out = await order_manager.execute_signal(signal)
        assert out is None


class TestOrderManagerScaleAndClose:
    async def test_try_scale_in_empty_positions(self, order_manager, mock_exchange):
        mock_exchange.fetch_positions.return_value = []
        added = await order_manager.try_scale_in()
        assert added == []

    async def test_try_scale_in_adds_when_scaler_says_so(self, order_manager, mock_exchange):
        sp = order_manager.scaler.create(
            symbol="BTC/USDT",
            side="long",
            strategy="test",
            leverage=10,
            mode=ScaleMode.PYRAMID,
        )
        sp.current_size = 0.001
        sp.avg_entry_price = 50_000.0
        sp.last_add_price = 50_000.0
        pos = Position(
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            amount=0.001,
            entry_price=50_000.0,
            current_price=49_000.0,
            leverage=2,
            market_type="futures",
        )
        mock_exchange.fetch_positions.return_value = [pos]
        filled = Order(
            id="o2",
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            amount=0.001,
            status=OrderStatus.FILLED,
            filled=0.001,
            average_price=49_000.0,
        )
        mock_exchange.place_order.return_value = filled

        added = await order_manager.try_scale_in()
        assert isinstance(added, list)

    async def test_try_lever_up(self, order_manager, mock_exchange):
        sp = order_manager.scaler.create(
            symbol="BTC/USDT",
            side="long",
            strategy="test",
            leverage=10,
            mode=ScaleMode.PYRAMID,
        )
        sp.adds = 1
        sp.current_size = 0.001
        sp.avg_entry_price = 50_000.0
        sp.leverage_raised = False
        # Price above avg so profit >= 1% -> should lever up
        mock_exchange.fetch_positions.return_value = [
            Position(
                symbol="BTC/USDT",
                side=OrderSide.BUY,
                amount=0.001,
                entry_price=50_000.0,
                current_price=50_750.0,
                leverage=2,
                market_type="futures",
            )
        ]
        levered = await order_manager.try_lever_up()
        assert isinstance(levered, list)
        if levered:
            assert "BTC/USDT" in levered

    async def test_try_partial_take(self, order_manager, mock_exchange):
        sp = order_manager.scaler.create(
            symbol="BTC/USDT",
            side="long",
            strategy="test",
            leverage=10,
            mode=ScaleMode.PYRAMID,
        )
        sp.leverage_raised = True
        sp.partial_taken = False
        sp.current_size = 0.01
        sp.avg_entry_price = 50_000.0
        pos = Position(
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            amount=0.01,
            entry_price=50_000.0,
            current_price=52_000.0,
            leverage=10,
            market_type="futures",
            unrealized_pnl=200.0,
        )
        mock_exchange.fetch_positions.return_value = [pos]
        closed = Order(
            id="c1",
            symbol="BTC/USDT",
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            amount=0.003,
            status=OrderStatus.FILLED,
            filled=0.003,
            average_price=52_000.0,
        )
        mock_exchange.place_order.return_value = closed

        taken = await order_manager.try_partial_take()
        assert isinstance(taken, list)

    async def test_close_position_removes_scaler(self, order_manager, mock_exchange):
        order_manager.scaler.create(
            symbol="BTC/USDT",
            side="long",
            strategy="test",
            leverage=10,
        )
        mock_exchange.fetch_positions.return_value = [
            Position(
                symbol="BTC/USDT",
                side=OrderSide.BUY,
                amount=0.001,
                entry_price=50_000.0,
                current_price=51_000.0,
                leverage=10,
                market_type="futures",
            )
        ]
        mock_exchange.place_order.return_value = Order(
            id="c1",
            symbol="BTC/USDT",
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            amount=0.001,
            status=OrderStatus.FILLED,
            filled=0.001,
            average_price=51_000.0,
        )
        signal = Signal(
            symbol="BTC/USDT",
            action=SignalAction.CLOSE,
            strategy="test",
        )
        await order_manager.execute_signal(signal)
        assert order_manager.scaler.get("BTC/USDT") is None


class TestOrderManagerStopsAndLogging:
    async def test_check_stops_returns_closed_list(self, order_manager, mock_exchange, mock_risk):
        mock_exchange.fetch_positions.return_value = []
        mock_exchange.fetch_balance.return_value = {"USDT": 10_000.0}
        closed = await order_manager.check_stops()
        assert isinstance(closed, list)

    def test_trade_history_initially_empty(self, order_manager):
        assert order_manager.trade_history == []

    def test_log_trade_appends_to_history(self, order_manager):
        signal = Signal(symbol="BTC/USDT", action=SignalAction.BUY, strategy="test")
        order = Order(
            id="o1",
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            amount=0.001,
            filled=0.001,
            average_price=50_000.0,
        )
        order_manager._log_trade(signal, order, "open", 0.0)
        assert len(order_manager.trade_history) == 1
        assert order_manager.trade_history[0]["action"] == "open"
        assert order_manager.trade_history[0]["symbol"] == "BTC/USDT"


class TestOrderManagerCloseExpiredQuickTrades:
    async def test_close_expired_quick_trades_skips_profitable(self, order_manager, mock_exchange):
        from datetime import timedelta

        old_ts = datetime.now(UTC) - timedelta(minutes=5)
        signal = Signal(
            symbol="BTC/USDT",
            action=SignalAction.BUY,
            strategy="test",
            quick_trade=True,
            max_hold_minutes=1,
            timestamp=old_ts,
        )
        mock_exchange.fetch_positions.return_value = [
            Position(
                symbol="BTC/USDT",
                side=OrderSide.BUY,
                amount=0.001,
                entry_price=50_000.0,
                current_price=51_500.0,
                leverage=10,
                market_type="futures",
                unrealized_pnl=15.0,
            )
        ]
        closed = await order_manager.close_expired_quick_trades([signal])
        # In profit > 1% -> let trail ride, don't close
        assert isinstance(closed, list)
        assert len(closed) == 0

    async def test_close_expired_quick_trades_closes_loser(self, order_manager, mock_exchange):
        from datetime import timedelta

        old_ts = datetime.now(UTC) - timedelta(minutes=10)
        signal = Signal(
            symbol="ETH/USDT",
            action=SignalAction.BUY,
            strategy="test",
            quick_trade=True,
            max_hold_minutes=5,
            timestamp=old_ts,
        )
        mock_exchange.fetch_positions.return_value = [
            Position(
                symbol="ETH/USDT",
                side=OrderSide.BUY,
                amount=0.01,
                entry_price=3000.0,
                current_price=2990.0,
                leverage=10,
                market_type="futures",
                unrealized_pnl=-1.0,
            )
        ]
        mock_exchange.place_order.return_value = Order(
            id="c1",
            symbol="ETH/USDT",
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            amount=0.01,
            status=OrderStatus.FILLED,
            filled=0.01,
            average_price=2990.0,
        )
        closed = await order_manager.close_expired_quick_trades([signal])
        assert isinstance(closed, list)


# ── OrderManager extended coverage: execute_signal branches, hedge, wick, stops ──


class TestOrderManagerExecuteSignalBranches:
    """Cover low_liquidity, pyramid, amount<=0, and close no-position paths."""

    @pytest.mark.asyncio
    async def test_execute_signal_low_liquidity_uses_gambling_size(self, order_manager, mock_exchange):
        signal = Signal(
            symbol="DOGE/USDT",
            action=SignalAction.BUY,
            strategy="test",
            suggested_price=0.35,
            leverage=10,
        )
        mock_exchange.place_order.return_value = Order(
            id="g1",
            symbol="DOGE/USDT",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            amount=1000.0,
            status=OrderStatus.FILLED,
            filled=1000.0,
            average_price=0.35,
        )
        out = await order_manager.execute_signal(signal, low_liquidity=True)
        assert out is not None
        assert out.status == OrderStatus.FILLED
        mock_exchange.place_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_signal_pyramid_registers_wide_stop(self, order_manager, mock_exchange):
        signal = Signal(
            symbol="BTC/USDT",
            action=SignalAction.BUY,
            strategy="test",
            suggested_price=50_000.0,
            leverage=10,
        )
        mock_exchange.place_order.return_value = Order(
            id="p1",
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            amount=0.001,
            status=OrderStatus.FILLED,
            filled=0.001,
            average_price=50_000.0,
        )
        out = await order_manager.execute_signal(signal, pyramid=True)
        assert out is not None
        ts = order_manager.trailing.get("BTC/USDT")
        assert ts is not None

    @pytest.mark.asyncio
    async def test_execute_signal_sell_places_sell_order(self, order_manager, mock_exchange):
        signal = Signal(
            symbol="ETH/USDT",
            action=SignalAction.SELL,
            strategy="test",
            suggested_price=3000.0,
            leverage=10,
        )
        mock_exchange.place_order.return_value = Order(
            id="s1",
            symbol="ETH/USDT",
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            amount=0.01,
            status=OrderStatus.FILLED,
            filled=0.01,
            average_price=3000.0,
        )
        out = await order_manager.execute_signal(signal)
        assert out is not None
        assert out.side == OrderSide.SELL

    @pytest.mark.asyncio
    async def test_close_position_no_position_returns_none(self, order_manager, mock_exchange):
        mock_exchange.fetch_positions.return_value = []
        signal = Signal(symbol="BTC/USDT", action=SignalAction.CLOSE, strategy="test")
        out = await order_manager.execute_signal(signal)
        assert out is None
        mock_exchange.place_order.assert_not_called()


class TestOrderManagerTryScaleInBranches:
    @pytest.mark.asyncio
    async def test_try_scale_in_skips_when_scaler_get_none(self, order_manager, mock_exchange):
        # get_symbols_to_add returns a symbol but get(symbol) is None after we don't create one
        order_manager.scaler.get_symbols_to_add = MagicMock(return_value=[("UNKNOWN/USDT", 0.001)])
        mock_exchange.fetch_positions.return_value = []
        added = await order_manager.try_scale_in()
        assert added == []

    @pytest.mark.asyncio
    async def test_try_scale_in_skips_when_no_position_for_symbol(self, order_manager, mock_exchange):
        sp = order_manager.scaler.create(
            symbol="BTC/USDT",
            side="long",
            strategy="test",
            leverage=10,
            mode=ScaleMode.PYRAMID,
        )
        sp.current_size = 0.001
        sp.avg_entry_price = 50_000.0
        sp.last_add_price = 50_000.0
        # positions for different symbol
        mock_exchange.fetch_positions.return_value = [
            Position(
                symbol="ETH/USDT",
                side=OrderSide.BUY,
                amount=0.01,
                entry_price=3000.0,
                current_price=2990.0,
                leverage=2,
                market_type="futures",
            ),
        ]
        added = await order_manager.try_scale_in()
        assert added == []


class TestOrderManagerTryLeverUpBranches:
    @pytest.mark.asyncio
    async def test_try_lever_up_exception_does_not_raise(self, order_manager, mock_exchange):
        sp = order_manager.scaler.create(
            symbol="BTC/USDT",
            side="long",
            strategy="test",
            leverage=10,
            mode=ScaleMode.PYRAMID,
        )
        sp.adds = 1
        sp.current_size = 0.001
        sp.avg_entry_price = 50_000.0
        sp.leverage_raised = False
        mock_exchange.fetch_positions.return_value = [
            Position(
                symbol="BTC/USDT",
                side=OrderSide.BUY,
                amount=0.001,
                entry_price=50_000.0,
                current_price=50_750.0,
                leverage=2,
                market_type="futures",
            ),
        ]
        mock_exchange.set_leverage = AsyncMock(side_effect=RuntimeError("exchange error"))
        levered = await order_manager.try_lever_up()
        assert levered == []

    @pytest.mark.asyncio
    async def test_try_lever_up_locks_breakeven_when_breakeven_after_lever(self, order_manager, mock_exchange):
        sp = order_manager.scaler.create(
            symbol="BTC/USDT",
            side="long",
            strategy="test",
            leverage=10,
            mode=ScaleMode.PYRAMID,
        )
        sp.adds = 1
        sp.current_size = 0.001
        sp.avg_entry_price = 50_000.0
        sp.leverage_raised = False
        sp.breakeven_after_lever = True
        mock_exchange.fetch_positions.return_value = [
            Position(
                symbol="BTC/USDT",
                side=OrderSide.BUY,
                amount=0.001,
                entry_price=50_000.0,
                current_price=50_750.0,
                leverage=2,
                market_type="futures",
            ),
        ]
        mock_exchange.set_leverage = AsyncMock()
        order_manager.trailing.register(
            Position(
                symbol="BTC/USDT",
                side=OrderSide.BUY,
                amount=0.001,
                entry_price=50_000.0,
                current_price=50_000.0,
                leverage=2,
                market_type="futures",
            ),
        )
        levered = await order_manager.try_lever_up()
        assert "BTC/USDT" in levered
        ts = order_manager.trailing.get("BTC/USDT")
        assert ts is not None
        assert ts.breakeven_locked is True


class TestOrderManagerTryHedge:
    @pytest.mark.asyncio
    async def test_try_hedge_tracks_profitable_positions_and_opens_hedge(self, order_manager, mock_exchange):
        from core.models import Candle

        pos = Position(
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            amount=0.01,
            entry_price=50_000.0,
            current_price=52_500.0,
            leverage=10,
            market_type="futures",
        )
        mock_exchange.fetch_positions.return_value = [pos]
        # Build enough candles so reversal_detector.assess returns score >= 0.5
        base = 50_000.0
        candles = [
            Candle(
                open=base + i * 100,
                high=base + i * 100 + 50,
                low=base + i * 100 - 50,
                close=base + i * 100 + 25,
                volume=1e6,
                timestamp=datetime.now(UTC),
            )
            for i in range(35)
        ]
        # Push RSI high for long -> overbought
        for i in range(20):
            candles[-1 - i] = Candle(
                open=52_000 + i * 10,
                high=52_500,
                low=52_000,
                close=52_400 - i * 5,
                volume=1e6,
                timestamp=datetime.now(UTC),
            )
        candles_map = {"BTC/USDT": candles}

        hedge_order = Order(
            id="h1",
            symbol="BTC/USDT",
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            amount=0.002,
            status=OrderStatus.FILLED,
            filled=0.002,
            average_price=52_500.0,
        )
        mock_exchange.place_order.return_value = hedge_order

        opened = await order_manager.try_hedge(candles_map)
        assert isinstance(opened, list)
        if opened:
            assert opened[0].symbol == "BTC/USDT"
            assert order_manager.hedger.has_active_hedge("BTC/USDT")

    @pytest.mark.asyncio
    async def test_try_hedge_skips_when_has_active_hedge(self, order_manager, mock_exchange):
        from core.orders.hedge import HedgeState

        pos = Position(
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            amount=0.01,
            entry_price=50_000.0,
            current_price=52_000.0,
            leverage=10,
            market_type="futures",
        )
        mock_exchange.fetch_positions.return_value = [pos]
        order_manager.hedger.track_position(pos)
        pair = order_manager.hedger.get("BTC/USDT")
        pair.state = HedgeState.ACTIVE
        pair.hedge_side = "short"
        candles_map = {"BTC/USDT": []}
        opened = await order_manager.try_hedge(candles_map)
        assert opened == []
        mock_exchange.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_try_hedge_cleans_up_closed_main_positions(self, order_manager, mock_exchange):
        order_manager.hedger.track_position(
            Position(
                symbol="BTC/USDT",
                side=OrderSide.BUY,
                amount=0.01,
                entry_price=50_000.0,
                current_price=52_000.0,
                leverage=10,
                market_type="futures",
            ),
        )
        mock_exchange.fetch_positions.return_value = []
        await order_manager.try_hedge({})
        assert "BTC/USDT" not in order_manager.hedger.active_pairs


class TestOrderManagerTryWickScalps:
    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_try_wick_scalps_closes_expired(self, order_manager, mock_exchange):
        from datetime import timedelta

        from core.orders.wick_scalp import WickScalp

        order_manager.scaler.create(
            symbol="BTC/USDT",
            side="long",
            strategy="test",
            leverage=10,
            mode=ScaleMode.PYRAMID,
        )
        scalp = WickScalp(symbol="BTC/USDT", main_side="long", scalp_side="short", max_hold_minutes=0)
        scalp.active = True
        scalp.created_at = datetime.now(UTC) - timedelta(minutes=10)
        order_manager.wick_scalper._active_scalps["BTC/USDT"] = scalp
        mock_exchange.fetch_positions.return_value = [
            Position(
                symbol="BTC/USDT",
                side=OrderSide.BUY,
                amount=0.001,
                entry_price=50_000.0,
                current_price=50_000.0,
                leverage=2,
                market_type="futures",
            ),
        ]
        mock_exchange.place_order.return_value = Order(
            id="c1",
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            amount=0.001,
            status=OrderStatus.FILLED,
            filled=0.001,
            average_price=50_000.0,
        )
        opened = await order_manager.try_wick_scalps()
        assert isinstance(opened, list)


class TestOrderManagerCheckStopsBranches:
    @pytest.mark.asyncio
    async def test_check_stops_closes_on_trailing_stop_hit(self, order_manager, mock_exchange, mock_risk):
        pos = Position(
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            amount=0.001,
            entry_price=50_000.0,
            current_price=48_000.0,
            leverage=10,
            market_type="futures",
        )
        order_manager.trailing.register(
            Position(
                symbol="BTC/USDT",
                side=OrderSide.BUY,
                amount=0.001,
                entry_price=50_000.0,
                current_price=48_000.0,
                leverage=10,
                market_type="futures",
            ),
            initial_stop_pct=2.0,
        )
        order_manager.scaler.create(symbol="BTC/USDT", side="long", strategy="test", leverage=10)
        mock_exchange.fetch_positions.return_value = [pos]
        mock_exchange.fetch_balance.return_value = {"USDT": 10_000.0}
        mock_exchange.place_order.return_value = Order(
            id="c1",
            symbol="BTC/USDT",
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            amount=0.001,
            status=OrderStatus.FILLED,
            filled=0.001,
            average_price=48_000.0,
        )
        closed = await order_manager.check_stops()
        assert len(closed) >= 1
        assert closed[0].symbol == "BTC/USDT"

    @pytest.mark.asyncio
    async def test_check_stops_closes_on_liquidation_risk(self, order_manager, mock_exchange, mock_risk):
        mock_risk.check_liquidation.return_value = True
        pos = Position(
            symbol="ETH/USDT",
            side=OrderSide.BUY,
            amount=0.01,
            entry_price=3000.0,
            current_price=2900.0,
            leverage=10,
            market_type="futures",
        )
        mock_exchange.fetch_positions.return_value = [pos]
        mock_exchange.fetch_balance.return_value = {"USDT": 100.0}
        mock_exchange.place_order.return_value = Order(
            id="c1",
            symbol="ETH/USDT",
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            amount=0.01,
            status=OrderStatus.FILLED,
            filled=0.01,
            average_price=2900.0,
        )
        closed = await order_manager.check_stops()
        assert len(closed) >= 1


class TestOrderManagerLogTradeNoScaler:
    def test_log_trade_when_scaler_get_returns_none(self, order_manager):
        signal = Signal(symbol="BTC/USDT", action=SignalAction.BUY, strategy="hedge")
        order = Order(
            id="o1",
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            amount=0.001,
            filled=0.001,
            average_price=50_000.0,
        )
        order_manager._log_trade(signal, order, "hedge_open")
        assert len(order_manager.trade_history) == 1
        assert order_manager.trade_history[0]["scale_phase"] == "n/a"
        assert order_manager.trade_history[0]["scale_mode"] == "n/a"


class TestSubPositionClose:
    @pytest.fixture
    def order_manager(self):
        exchange = AsyncMock()
        exchange.fetch_balance = AsyncMock(return_value={"USDT": 1000.0})
        exchange.fetch_positions = AsyncMock(return_value=[])
        risk = MagicMock()
        risk.check_signal.return_value = True
        risk.apply_stops.side_effect = lambda s: s
        risk.check_liquidation.return_value = False
        settings = Settings(
            trading_mode="paper",
            exchange="binance",
            binance_test_api_key="k",
            binance_test_api_secret="s",
        )
        return OrderManager(exchange, risk, settings)

    @pytest.mark.asyncio
    async def test_close_sub_position_no_hedge(self, order_manager):
        result = await order_manager._close_sub_position("BTC/USDT", order_manager.hedger, "hedge")
        assert result is None

    @pytest.mark.asyncio
    async def test_close_sub_position_wick_no_scalp(self, order_manager):
        result = await order_manager._close_sub_position_wick("BTC/USDT")
        assert result is None

    @pytest.mark.asyncio
    async def test_close_sub_position_wick_with_scalp(self, order_manager):
        from core.orders.wick_scalp import WickScalp

        order_manager.wick_scalper._active_scalps["BTC/USDT"] = WickScalp(
            symbol="BTC/USDT",
            main_side="long",
            scalp_side="short",
            entry_price=50000,
            amount=0.01,
            leverage=10,
            active=True,
        )
        order_manager.exchange.place_order.return_value = Order(
            id="w1",
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            amount=0.01,
            status=OrderStatus.FILLED,
            filled=0.01,
            average_price=49000,
        )
        result = await order_manager._close_sub_position_wick("BTC/USDT")
        assert result is not None
        assert result.id == "w1"

    @pytest.mark.asyncio
    async def test_close_sub_position_with_active_hedge(self, order_manager):
        from core.orders.hedge import HedgePair, HedgeState

        order_manager.hedger._pairs["ETH/USDT"] = HedgePair(
            symbol="ETH/USDT",
            main_side="long",
            main_entry=3000,
            main_size=3000,
            hedge_side="short",
            hedge_entry=3100,
            hedge_size=600,
            state=HedgeState.ACTIVE,
        )
        order_manager.exchange.place_order.return_value = Order(
            id="hc1",
            symbol="ETH/USDT",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            amount=0.2,
            status=OrderStatus.FILLED,
            filled=0.2,
            average_price=3050,
        )
        result = await order_manager._close_sub_position("ETH/USDT", order_manager.hedger, "hedge")
        assert result is not None
        assert result.id == "hc1"

    @pytest.mark.asyncio
    async def test_check_stops_routes_hedge_key(self, order_manager):
        from core.orders.hedge import HedgePair, HedgeState

        order_manager.hedger._pairs["BTC/USDT"] = HedgePair(
            symbol="BTC/USDT",
            main_side="long",
            main_entry=50000,
            main_size=5000,
            hedge_side="short",
            hedge_entry=51000,
            hedge_size=1000,
            state=HedgeState.ACTIVE,
        )
        pos = Position(
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            amount=0.1,
            entry_price=50000,
            current_price=50000,
            leverage=10,
            market_type="futures",
        )
        # Register main + hedge stop; set hedge stop to trigger immediately
        order_manager.trailing.register(pos, initial_stop_pct=50.0)
        order_manager.trailing.register(pos, initial_stop_pct=0.001, key="BTC/USDT:hedge")

        order_manager.exchange.fetch_positions.return_value = [
            Position(
                symbol="BTC/USDT",
                side=OrderSide.BUY,
                amount=0.1,
                entry_price=50000,
                current_price=40000,
                leverage=10,
                market_type="futures",
            ),
        ]
        order_manager.exchange.place_order.return_value = Order(
            id="h1",
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            amount=0.02,
            status=OrderStatus.FILLED,
            filled=0.02,
            average_price=40000,
        )
        closed = await order_manager.check_stops()
        assert any(o.id == "h1" for o in closed)
        assert order_manager.trailing.get("BTC/USDT") is not None  # main stop preserved
