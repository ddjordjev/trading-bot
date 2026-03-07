"""Tests for trailing stop mechanics.

A broken trailing stop means:
- Stop doesn't trigger → catastrophic loss / liquidation
- Stop moves backward → gives back profit unnecessarily
- Break-even doesn't lock → lose a confirmed winner
These are the highest-stakes bugs in the entire bot.
"""

import pytest

from core.models import OrderSide, Position
from core.orders.trailing import TrailingStop, TrailingStopManager


class TestLongStop:
    def _make(self, entry: float = 100.0, **kwargs) -> TrailingStop:
        defaults = dict(
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            entry_price=entry,
            initial_stop_pct=2.0,
            trail_pct=1.0,
            activation_pct=1.0,
            breakeven_trigger_pct=5.0,
        )
        defaults.update(kwargs)
        return TrailingStop(**defaults)

    def test_initial_stop_below_entry(self):
        ts = self._make(entry=100.0)
        assert ts.current_stop == pytest.approx(98.0)

    def test_stop_triggers_on_drop(self):
        ts = self._make(entry=100.0)
        assert ts.update(97.5) is True

    def test_stop_does_not_trigger_above(self):
        ts = self._make(entry=100.0)
        assert ts.update(99.0) is False

    def test_breakeven_locks_at_5pct(self):
        ts = self._make(entry=100.0)
        ts.update(105.1)
        assert ts.breakeven_locked is True
        assert ts.current_stop > 100.0  # offset covers fees

    def test_breakeven_offset_covers_fees(self):
        ts = self._make(entry=100.0, activation_pct=10.0)
        ts.update(105.1)  # triggers BE (5%) but not trailing (10%)
        assert ts.current_stop == pytest.approx(101.0)  # one tick above entry

    def test_breakeven_does_not_lock_below_threshold(self):
        ts = self._make(entry=100.0)
        ts.update(104.9)
        assert ts.breakeven_locked is False

    def test_trail_follows_price_up(self):
        ts = self._make(entry=100.0)
        ts.update(102.0)  # activate trailing
        stop_after_102 = ts.current_stop
        ts.update(105.0)
        assert ts.current_stop > stop_after_102

    def test_trail_never_moves_backward(self):
        ts = self._make(entry=100.0)
        ts.update(102.0)
        ts.update(110.0)
        high_stop = ts.current_stop
        ts.update(107.0)  # price drops
        assert ts.current_stop == high_stop

    def test_peak_tracks_highest(self):
        ts = self._make(entry=100.0)
        ts.update(110.0)
        ts.update(105.0)
        assert ts.peak_price == 110.0


class TestShortStop:
    def _make(self, entry: float = 100.0, **kwargs) -> TrailingStop:
        defaults = dict(
            symbol="BTC/USDT",
            side=OrderSide.SELL,
            entry_price=entry,
            initial_stop_pct=2.0,
            trail_pct=1.0,
            activation_pct=1.0,
            breakeven_trigger_pct=5.0,
        )
        defaults.update(kwargs)
        return TrailingStop(**defaults)

    def test_initial_stop_above_entry(self):
        ts = self._make(entry=100.0)
        assert ts.current_stop == pytest.approx(102.0)

    def test_stop_triggers_on_spike(self):
        ts = self._make(entry=100.0)
        assert ts.update(102.5) is True

    def test_breakeven_locks_on_short_profit(self):
        ts = self._make(entry=100.0)
        ts.update(94.9)
        assert ts.breakeven_locked is True
        assert ts.current_stop < 100.0  # offset below entry

    def test_short_breakeven_offset(self):
        ts = self._make(entry=100.0, activation_pct=10.0)
        ts.update(94.9)  # triggers BE (5%) but not trailing (10%)
        assert ts.current_stop == pytest.approx(99.0)  # one tick below entry

    def test_trail_follows_price_down(self):
        ts = self._make(entry=100.0)
        ts.update(98.0)
        stop_after = ts.current_stop
        ts.update(95.0)
        assert ts.current_stop < stop_after

    def test_trail_never_moves_up(self):
        ts = self._make(entry=100.0)
        ts.update(98.0)
        ts.update(92.0)
        low_stop = ts.current_stop
        ts.update(95.0)
        assert ts.current_stop == low_stop


class TestBeWithFeeOffset:
    """Verify _be_with_fee_offset nudges the stop past entry to cover fees."""

    def test_long_high_price(self):
        result = TrailingStop._be_with_fee_offset(100.0, long=True)
        assert result == pytest.approx(101.0)  # >= 100 → tick = 1.0

    def test_short_high_price(self):
        result = TrailingStop._be_with_fee_offset(100.0, long=False)
        assert result == pytest.approx(99.0)

    def test_long_10k_price(self):
        result = TrailingStop._be_with_fee_offset(10564.0, long=True)
        assert result == pytest.approx(10574.0)  # >= 10000 → tick = 10

    def test_short_10k_price(self):
        result = TrailingStop._be_with_fee_offset(10564.0, long=False)
        assert result == pytest.approx(10554.0)

    def test_long_mid_price(self):
        result = TrailingStop._be_with_fee_offset(1.4545, long=True)
        assert result == pytest.approx(1.4555)  # >= 1 → tick = 0.001

    def test_short_mid_price(self):
        result = TrailingStop._be_with_fee_offset(1.4545, long=False)
        assert result == pytest.approx(1.4535)

    def test_long_sub_dollar(self):
        result = TrailingStop._be_with_fee_offset(0.43, long=True)
        assert result == pytest.approx(0.43043)  # < 1 → tick = 0.1% of entry

    def test_short_sub_dollar(self):
        result = TrailingStop._be_with_fee_offset(0.43, long=False)
        assert result == pytest.approx(0.42957)

    def test_long_micro_price(self):
        result = TrailingStop._be_with_fee_offset(0.005, long=True)
        assert result == pytest.approx(0.005005)  # < 1 → tick = 0.1% of entry

    def test_short_micro_price(self):
        result = TrailingStop._be_with_fee_offset(0.005, long=False)
        assert result == pytest.approx(0.004995)

    def test_very_small_price(self):
        result = TrailingStop._be_with_fee_offset(0.00025, long=True)
        assert result > 0.00025

    def test_zero_entry(self):
        assert TrailingStop._be_with_fee_offset(0, long=True) == 0


class TestPnlFromStop:
    def test_long_positive_pnl(self):
        ts = TrailingStop(
            symbol="X",
            side=OrderSide.BUY,
            entry_price=100.0,
            initial_stop_pct=2.0,
            trail_pct=1.0,
        )
        ts.update(110.0)  # should raise stop via trail
        assert ts.pnl_from_stop > 0

    def test_long_initial_pnl_is_negative(self):
        ts = TrailingStop(
            symbol="X",
            side=OrderSide.BUY,
            entry_price=100.0,
            initial_stop_pct=2.0,
            trail_pct=1.0,
        )
        assert ts.pnl_from_stop < 0  # initial stop is below entry


class TestTrailingStopManagerKeys:
    """Verify that keyed stops (hedge/wick) don't overwrite main stops."""

    def _pos(self, symbol: str, side: OrderSide = OrderSide.BUY, price: float = 100.0) -> Position:
        return Position(
            symbol=symbol,
            side=side,
            amount=1.0,
            entry_price=price,
            current_price=price,
            leverage=10,
            market_type="futures",
        )

    def test_register_with_key(self):
        mgr = TrailingStopManager()
        pos = self._pos("BTC/USDT")
        mgr.register(pos)
        mgr.register(pos, initial_stop_pct=1.0, key="BTC/USDT:hedge")
        assert mgr.get("BTC/USDT") is not None
        assert mgr.get("BTC/USDT:hedge") is not None
        assert mgr.get("BTC/USDT") is not mgr.get("BTC/USDT:hedge")

    def test_keyed_stops_dont_overwrite_main(self):
        mgr = TrailingStopManager()
        pos = self._pos("BTC/USDT")
        main_ts = mgr.register(pos, initial_stop_pct=5.0)
        mgr.register(pos, initial_stop_pct=1.0, key="BTC/USDT:hedge")
        assert mgr.get("BTC/USDT") is main_ts
        assert mgr.get("BTC/USDT").initial_stop_pct == 5.0

    def test_update_all_checks_all_keyed_stops(self):
        mgr = TrailingStopManager()
        pos = self._pos("BTC/USDT", price=100.0)
        mgr.register(pos, initial_stop_pct=2.0)
        mgr.register(pos, initial_stop_pct=0.5, key="BTC/USDT:hedge")

        low_price_pos = Position(
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            amount=1.0,
            entry_price=100.0,
            current_price=90.0,
            leverage=10,
            market_type="futures",
        )
        stopped = mgr.update_all([low_price_pos])
        assert "BTC/USDT" in stopped
        assert "BTC/USDT:hedge" in stopped

    def test_remove_by_key(self):
        mgr = TrailingStopManager()
        pos = self._pos("BTC/USDT")
        mgr.register(pos)
        mgr.register(pos, key="BTC/USDT:hedge")
        mgr.remove("BTC/USDT:hedge")
        assert mgr.get("BTC/USDT") is not None
        assert mgr.get("BTC/USDT:hedge") is None

    def test_update_all_returns_keys_not_symbols(self):
        mgr = TrailingStopManager()
        pos = self._pos("BTC/USDT", price=100.0)
        mgr.register(pos, initial_stop_pct=0.1, key="BTC/USDT:wick")
        hit_pos = Position(
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            amount=1.0,
            entry_price=100.0,
            current_price=99.0,
            leverage=10,
            market_type="futures",
        )
        stopped = mgr.update_all([hit_pos])
        assert "BTC/USDT:wick" in stopped


class TestTrailingBehaviorGuards:
    def test_pullback_mode_trails_without_structure_guard_long(self):
        ts = TrailingStop(
            symbol="ALT/USDT",
            side=OrderSide.BUY,
            entry_price=100.0,
            initial_stop_pct=2.0,
            trail_pct=1.0,
            activation_pct=1.0,
            trailing_mode="pullback",
            pullback_buffer_pct=4.0,
            structure_guard=0.0,
        )
        ts.update(105.0)
        assert ts.current_stop > 100.0

    def test_pullback_mode_trails_without_structure_guard_short(self):
        ts = TrailingStop(
            symbol="ALT/USDT",
            side=OrderSide.SELL,
            entry_price=100.0,
            initial_stop_pct=2.0,
            trail_pct=1.0,
            activation_pct=1.0,
            trailing_mode="pullback",
            pullback_buffer_pct=4.0,
            structure_guard=0.0,
        )
        ts.update(95.0)
        assert ts.current_stop < 100.0

    def test_wick_tighten_requires_touch_then_reclaim(self):
        ts = TrailingStop(
            symbol="ALT/USDT",
            side=OrderSide.BUY,
            entry_price=100.0,
            initial_stop_pct=2.0,
            trail_pct=1.0,
            activation_pct=10.0,
            tightened_stop=99.0,
            wick_tighten_enabled=True,
        )
        original = ts.current_stop

        # No touch -> no tighten.
        ts.update(101.0)
        assert ts.current_stop == pytest.approx(original)
        assert ts.wick_touched is False

        # Touch below tightened stop, then reclaim above tightened stop.
        ts.update(98.9)
        assert ts.wick_touched is True
        ts.update(99.2)
        assert ts.wick_bounced is True
        assert ts.current_stop > 99.0

    def test_structure_guard_caps_long_stop(self):
        ts = TrailingStop(
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            entry_price=100.0,
            initial_stop_pct=2.0,
            trail_pct=1.0,
            activation_pct=1.0,
            structure_guard=103.0,
            trailing_mode="pullback",
        )
        ts.update(104.0)
        ts.update(106.0)
        assert ts.current_stop <= 103.0

    def test_structure_guard_caps_short_stop(self):
        ts = TrailingStop(
            symbol="BTC/USDT",
            side=OrderSide.SELL,
            entry_price=100.0,
            initial_stop_pct=2.0,
            trail_pct=1.0,
            activation_pct=1.0,
            structure_guard=97.0,
            trailing_mode="pullback",
        )
        ts.update(98.0)
        ts.update(94.0)
        assert ts.current_stop >= 97.0

    def test_fast_mode_ignores_structure_guard(self):
        ts = TrailingStop(
            symbol="POWER/USDT",
            side=OrderSide.BUY,
            entry_price=0.17852,
            initial_stop_pct=1.5,
            trail_pct=0.4,
            activation_pct=0.5,
            breakeven_trigger_pct=5.0,
            structure_guard=0.1759,
            trailing_mode="fast",
        )
        ts.update(0.19)
        assert ts.current_stop > 0.17852


class TestTrailingProfitTakingMode:
    def test_profit_mode_does_not_change_breakeven_trigger(self):
        mgr = TrailingStopManager(default_trail_pct=0.5, breakeven_pct=5.0)
        mgr.set_profit_taking_mode(1.3)
        assert mgr.breakeven_pct == pytest.approx(5.0)
        mgr.set_profit_taking_mode(0.8)
        assert mgr.breakeven_pct == pytest.approx(5.0)
