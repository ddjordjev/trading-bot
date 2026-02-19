"""Tests for core/models (market, order, signal)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.models.market import Candle, MarketType, OrderBook, Ticker
from core.models.order import Order, OrderSide, OrderStatus, OrderType, Position
from core.models.signal import Signal, SignalAction

# ── Candle ──────────────────────────────────────────────────────────


class TestCandle:
    def _make(self, **kw) -> Candle:
        defaults = dict(timestamp=datetime.now(UTC), open=100.0, high=110.0, low=90.0, close=105.0, volume=1000.0)
        defaults.update(kw)
        return Candle(**defaults)

    def test_body_pct(self):
        c = self._make(open=100, close=105)
        assert c.body_pct == pytest.approx(5.0)

    def test_body_pct_zero_open(self):
        c = self._make(open=0, close=105)
        assert c.body_pct == 0.0

    def test_range_pct(self):
        c = self._make(low=100, high=110)
        assert c.range_pct == pytest.approx(10.0)

    def test_range_pct_zero_low(self):
        c = self._make(low=0, high=110)
        assert c.range_pct == 0.0


# ── Ticker ──────────────────────────────────────────────────────────


class TestTicker:
    def _make(self, **kw) -> Ticker:
        defaults = dict(
            symbol="BTC/USDT",
            bid=100.0,
            ask=101.0,
            last=100.5,
            volume_24h=1e6,
            change_pct_24h=2.5,
            timestamp=datetime.now(UTC),
        )
        defaults.update(kw)
        return Ticker(**defaults)

    def test_mid(self):
        t = self._make(bid=100, ask=102)
        assert t.mid == pytest.approx(101.0)

    def test_spread_pct(self):
        t = self._make(bid=100, ask=101)
        assert t.spread_pct == pytest.approx(1.0)

    def test_spread_pct_zero_bid(self):
        t = self._make(bid=0, ask=101)
        assert t.spread_pct == 0.0


# ── OrderBook ───────────────────────────────────────────────────────


class TestOrderBook:
    def test_creation(self):
        ob = OrderBook(symbol="BTC/USDT", bids=[(100, 1)], asks=[(101, 1)], timestamp=datetime.now(UTC))
        assert ob.symbol == "BTC/USDT"
        assert len(ob.bids) == 1


# ── Order ───────────────────────────────────────────────────────────


class TestOrder:
    def test_remaining(self):
        o = Order(symbol="BTC/USDT", side=OrderSide.BUY, order_type=OrderType.MARKET, amount=10.0, filled=3.0)
        assert o.remaining == pytest.approx(7.0)

    def test_is_complete_filled(self):
        o = Order(
            symbol="BTC/USDT", side=OrderSide.BUY, order_type=OrderType.MARKET, amount=10.0, status=OrderStatus.FILLED
        )
        assert o.is_complete is True

    def test_is_complete_cancelled(self):
        o = Order(
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            amount=10.0,
            status=OrderStatus.CANCELLED,
        )
        assert o.is_complete is True

    def test_is_not_complete_pending(self):
        o = Order(
            symbol="BTC/USDT", side=OrderSide.BUY, order_type=OrderType.MARKET, amount=10.0, status=OrderStatus.PENDING
        )
        assert o.is_complete is False


# ── Position ────────────────────────────────────────────────────────


class TestPosition:
    def test_notional_value(self):
        p = Position(symbol="BTC/USDT", side=OrderSide.BUY, amount=2.0, entry_price=100, current_price=110)
        assert p.notional_value == pytest.approx(220.0)

    def test_pnl_pct_long(self):
        p = Position(symbol="BTC/USDT", side=OrderSide.BUY, amount=1.0, entry_price=100, current_price=110, leverage=10)
        assert p.pnl_pct == pytest.approx(100.0)

    def test_pnl_pct_short(self):
        p = Position(symbol="BTC/USDT", side=OrderSide.SELL, amount=1.0, entry_price=100, current_price=90, leverage=10)
        assert p.pnl_pct == pytest.approx(100.0)

    def test_pnl_pct_zero_entry(self):
        p = Position(symbol="BTC/USDT", side=OrderSide.BUY, amount=1.0, entry_price=0, current_price=110)
        assert p.pnl_pct == 0.0


# ── Signal ──────────────────────────────────────────────────────────


class TestSignal:
    def test_defaults(self):
        s = Signal(symbol="BTC/USDT", action=SignalAction.BUY)
        assert s.strength == 0.0
        assert s.quick_trade is False
        assert s.max_hold_minutes is None

    def test_quick_trade(self):
        s = Signal(symbol="BTC/USDT", action=SignalAction.BUY, quick_trade=True, max_hold_minutes=5)
        assert s.quick_trade is True
        assert s.max_hold_minutes == 5


# ── Enums ───────────────────────────────────────────────────────────


class TestEnums:
    def test_market_type_values(self):
        assert MarketType.SPOT == "spot"
        assert MarketType.FUTURES == "futures"

    def test_signal_action_values(self):
        assert SignalAction.BUY == "buy"
        assert SignalAction.CLOSE == "close"
        assert SignalAction.HOLD == "hold"

    def test_order_status_values(self):
        assert OrderStatus.PENDING == "pending"
        assert OrderStatus.FAILED == "failed"
