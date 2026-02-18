"""Tests for core/orders/hedge.py."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.models import Candle, OrderSide, Position
from core.orders.hedge import (
    HedgePair, HedgeState, ReversalDetector, HedgeManager,
)


# ── Helpers ─────────────────────────────────────────────────────────

def _make_candle(close: float, volume: float = 1000, high_off=2, low_off=2) -> Candle:
    return Candle(timestamp=datetime.now(timezone.utc), open=close,
                  high=close + high_off, low=close - low_off,
                  close=close, volume=volume)


def _pos(symbol="BTC/USDT", side=OrderSide.BUY, amount=1.0,
         entry=100, current=110, leverage=10) -> Position:
    return Position(symbol=symbol, side=side, amount=amount,
                    entry_price=entry, current_price=current, leverage=leverage)


# ── HedgePair ───────────────────────────────────────────────────────

class TestHedgePair:
    def test_hedge_notional(self):
        hp = HedgePair(symbol="BTC/USDT", main_side="long",
                       main_entry=100, main_size=10000, hedge_ratio=0.2)
        assert hp.hedge_notional == pytest.approx(2000.0)

    def test_should_hedge_watching_profitable_reversal(self):
        hp = HedgePair(symbol="BTC/USDT", main_side="long",
                       main_entry=100, main_size=10000,
                       main_pnl_pct=5.0, reversal_score=0.6)
        assert hp.should_hedge() is True

    def test_should_not_hedge_low_profit(self):
        hp = HedgePair(symbol="BTC/USDT", main_side="long",
                       main_entry=100, main_size=10000,
                       main_pnl_pct=1.0, reversal_score=0.6)
        assert hp.should_hedge() is False

    def test_should_not_hedge_low_reversal_score(self):
        hp = HedgePair(symbol="BTC/USDT", main_side="long",
                       main_entry=100, main_size=10000,
                       main_pnl_pct=5.0, reversal_score=0.3)
        assert hp.should_hedge() is False

    def test_should_not_hedge_already_active(self):
        hp = HedgePair(symbol="BTC/USDT", main_side="long",
                       main_entry=100, main_size=10000,
                       main_pnl_pct=5.0, reversal_score=0.6,
                       state=HedgeState.ACTIVE)
        assert hp.should_hedge() is False

    def test_activate_hedge_long_main(self):
        hp = HedgePair(symbol="BTC/USDT", main_side="long",
                       main_entry=100, main_size=10000)
        hp.activate_hedge(entry_price=110, amount=0.5, order_id="ABC")
        assert hp.hedge_side == "short"
        assert hp.state == HedgeState.ACTIVE
        assert hp.hedge_entry == 110
        assert hp.hedged_at is not None

    def test_activate_hedge_short_main(self):
        hp = HedgePair(symbol="BTC/USDT", main_side="short",
                       main_entry=100, main_size=10000)
        hp.activate_hedge(entry_price=90, amount=0.5, order_id="ABC")
        assert hp.hedge_side == "long"

    def test_close_hedge(self):
        hp = HedgePair(symbol="BTC/USDT", main_side="long",
                       main_entry=100, main_size=10000, state=HedgeState.ACTIVE)
        hp.close_hedge()
        assert hp.state == HedgeState.CLOSED

    def test_status_line_watching(self):
        hp = HedgePair(symbol="BTC/USDT", main_side="long",
                       main_entry=100, main_size=10000)
        line = hp.status_line()
        assert "WATCHING" in line

    def test_status_line_active(self):
        hp = HedgePair(symbol="BTC/USDT", main_side="long",
                       main_entry=100, main_size=10000, state=HedgeState.ACTIVE,
                       hedge_side="short", hedge_size=2000)
        line = hp.status_line()
        assert "active" in line


# ── ReversalDetector ────────────────────────────────────────────────

class TestReversalDetector:
    @pytest.fixture()
    def detector(self):
        return ReversalDetector()

    def test_insufficient_data(self, detector):
        candles = [_make_candle(100) for _ in range(10)]
        score, reasons = detector.assess(candles, "long")
        assert score == 0.0
        assert reasons == []

    def test_rsi_overextended_long(self, detector):
        candles = [_make_candle(100 + i * 0.5, volume=1000) for i in range(40)]
        score, reasons = detector.assess(candles, "long")
        assert score >= 0.0

    def test_simple_rsi_all_gains(self, detector):
        candles = [_make_candle(100 + i) for i in range(20)]
        rsi = detector._simple_rsi(candles, 14)
        assert rsi == 100.0

    def test_simple_rsi_insufficient(self, detector):
        candles = [_make_candle(100) for _ in range(5)]
        rsi = detector._simple_rsi(candles, 14)
        assert rsi == 50.0

    def test_volume_divergence_no_data(self, detector):
        candles = [_make_candle(100) for _ in range(3)]
        assert detector._volume_divergence(candles, "long") is False

    def test_momentum_fade_insufficient(self, detector):
        candles = [_make_candle(100) for _ in range(5)]
        assert detector._momentum_fade(candles, "long") is False

    def test_wick_rejection_empty(self, detector):
        assert detector._wick_rejection([], "long") is False

    def test_wick_rejection_long_upper_wicks(self, detector):
        candles = [
            Candle(timestamp=datetime.now(timezone.utc), open=100, high=120,
                   low=99, close=101, volume=1000),
            Candle(timestamp=datetime.now(timezone.utc), open=100, high=120,
                   low=99, close=101, volume=1000),
            Candle(timestamp=datetime.now(timezone.utc), open=100, high=120,
                   low=99, close=101, volume=1000),
        ]
        assert detector._wick_rejection(candles, "long") is True


# ── HedgeManager ────────────────────────────────────────────────────

class TestHedgeManager:
    @pytest.fixture()
    def mgr(self):
        return HedgeManager(hedge_ratio=0.2, min_main_profit_pct=3.0,
                            hedge_stop_pct=1.0, max_hedges=2)

    def test_track_position(self, mgr):
        pos = _pos()
        pair = mgr.track_position(pos)
        assert pair.symbol == "BTC/USDT"
        assert pair.main_side == "long"

    def test_get_hedge_params(self, mgr):
        pos = _pos(current=110)
        mgr.track_position(pos)
        pair = mgr.get("BTC/USDT")
        pair.state = HedgeState.WATCHING
        pair.reversal_score = 0.7
        pair.main_pnl_pct = 5.0
        params = mgr.get_hedge_params("BTC/USDT", 110.0, leverage=10)
        assert params is not None
        assert params["side"] == OrderSide.SELL
        assert params["amount"] > 0

    def test_get_hedge_params_no_pair(self, mgr):
        assert mgr.get_hedge_params("NOPE", 100.0) is None

    def test_get_hedge_params_zero_price(self, mgr):
        pos = _pos()
        mgr.track_position(pos)
        assert mgr.get_hedge_params("BTC/USDT", 0) is None

    def test_activate_and_close(self, mgr):
        pos = _pos()
        mgr.track_position(pos)
        mgr.activate("BTC/USDT", 110.0, 0.5, "order-1")
        assert mgr.has_active_hedge("BTC/USDT") is True
        mgr.close("BTC/USDT")
        assert mgr.has_active_hedge("BTC/USDT") is False

    def test_remove(self, mgr):
        pos = _pos()
        mgr.track_position(pos)
        mgr.remove("BTC/USDT")
        assert mgr.get("BTC/USDT") is None

    def test_update_removes_closed_positions(self, mgr):
        pos = _pos()
        mgr.track_position(pos)
        empty_pos = _pos(amount=0)
        ready = mgr.update([empty_pos], {})
        assert mgr.get("BTC/USDT") is None
        assert ready == []

    def test_max_hedges_respected(self, mgr):
        for sym in ["BTC/USDT", "ETH/USDT", "SOL/USDT"]:
            pos = _pos(symbol=sym)
            mgr.track_position(pos)
        mgr.activate("BTC/USDT", 110, 0.5, "o1")
        mgr.activate("ETH/USDT", 110, 0.5, "o2")
        assert mgr._active_hedge_count() == 2

    def test_active_pairs(self, mgr):
        pos = _pos()
        mgr.track_position(pos)
        assert "BTC/USDT" in mgr.active_pairs

    def test_get_hedge_params_short_main(self, mgr):
        pos = _pos(side=OrderSide.SELL)
        mgr.track_position(pos)
        params = mgr.get_hedge_params("BTC/USDT", 90.0, leverage=10)
        assert params["side"] == OrderSide.BUY
