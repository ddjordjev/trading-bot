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
        assert ts.current_stop >= 100.0

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
        assert ts.current_stop <= 100.0

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
