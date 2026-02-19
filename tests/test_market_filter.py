"""Tests for core/risk/market_filter.py."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.models import Candle, Ticker
from core.risk.market_filter import (
    LiquidityProfile,
    LiquidityTier,
    MarketQualityFilter,
)


def _make_candle(open_=100, high=110, low=90, close=105, volume=1000) -> Candle:
    return Candle(timestamp=datetime.now(UTC), open=open_, high=high, low=low, close=close, volume=volume)


def _make_ticker(bid=100, ask=100.1, volume=1e7) -> Ticker:
    return Ticker(
        symbol="BTC/USDT",
        bid=bid,
        ask=ask,
        last=100.05,
        volume_24h=volume,
        change_pct_24h=1.0,
        timestamp=datetime.now(UTC),
    )


class TestLiquidityProfile:
    def test_safe_for_stops_high(self):
        lp = LiquidityProfile(LiquidityTier.HIGH, 1e7, 0.05, 1000)
        assert lp.is_safe_for_stops is True
        assert lp.max_position_multiplier == 1.0

    def test_safe_for_stops_medium(self):
        lp = LiquidityProfile(LiquidityTier.MEDIUM, 5e6, 0.15, 500)
        assert lp.is_safe_for_stops is True
        assert lp.max_position_multiplier == 0.7

    def test_unsafe_for_stops_low(self):
        lp = LiquidityProfile(LiquidityTier.LOW, 5e5, 0.4, 100)
        assert lp.is_safe_for_stops is False
        assert lp.max_position_multiplier == 0.15

    def test_dead(self):
        lp = LiquidityProfile(LiquidityTier.DEAD, 0, 2.0, 0)
        assert lp.is_safe_for_stops is False
        assert lp.max_position_multiplier == 0.0


class TestMarketQualityFilter:
    @pytest.fixture()
    def filt(self):
        return MarketQualityFilter()

    def test_assess_liquidity_no_data(self, filt):
        ticker = _make_ticker()
        lp = filt.assess_liquidity([], ticker)
        assert lp.tier == LiquidityTier.DEAD

    def test_assess_liquidity_high(self, filt):
        candles = [_make_candle(volume=10000) for _ in range(50)]
        ticker = _make_ticker(bid=100, ask=100.05, volume=5e7)
        lp = filt.assess_liquidity(candles, ticker)
        assert lp.tier == LiquidityTier.HIGH

    def test_assess_liquidity_dead_spread(self, filt):
        candles = [_make_candle(volume=0) for _ in range(50)]
        ticker = _make_ticker(bid=100, ask=102)
        lp = filt.assess_liquidity(candles, ticker)
        assert lp.tier == LiquidityTier.DEAD

    def test_is_tradeable_insufficient_data(self, filt):
        candles = [_make_candle() for _ in range(10)]
        ticker = _make_ticker()
        ok, reason = filt.is_tradeable(candles, ticker)
        assert ok is False
        assert "insufficient" in reason

    def test_is_tradeable_dead(self, filt):
        candles = [_make_candle(volume=0) for _ in range(50)]
        ticker = _make_ticker(bid=100, ask=102)
        ok, reason = filt.is_tradeable(candles, ticker)
        assert ok is False
        assert "dead" in reason

    def test_is_tradeable_good_market(self, filt):
        candles = [
            _make_candle(
                open_=100 + i * 0.1,
                close=100 + i * 0.1 + 0.8,
                high=100 + i * 0.1 + 1,
                low=100 + i * 0.1 - 0.1,
                volume=10000,
            )
            for i in range(50)
        ]
        ticker = _make_ticker(bid=125, ask=125.02, volume=5e7)
        ok, _reason = filt.is_tradeable(candles, ticker)
        assert ok is True

    def test_is_low_liquidity(self, filt):
        candles = [_make_candle(volume=10) for _ in range(50)]
        ticker = _make_ticker(bid=100, ask=100.6, volume=5e5)
        assert filt.is_low_liquidity(candles, ticker) is True

    def test_choppiness_flat(self):
        candles = [Candle(timestamp=datetime.now(UTC), open=100, high=100, low=100, close=100, volume=1)]
        assert MarketQualityFilter._choppiness(candles) == 1.0

    def test_choppiness_empty(self):
        assert MarketQualityFilter._choppiness([]) == 1.0

    def test_atr_ratio_short_data(self):
        candles = [_make_candle() for _ in range(10)]
        assert MarketQualityFilter._atr_ratio(candles) == 1.0

    def test_atr_ratio_zero(self):
        candles = [
            Candle(timestamp=datetime.now(UTC), open=100, high=100, low=100, close=100, volume=1) for _ in range(25)
        ]
        assert MarketQualityFilter._atr_ratio(candles) == 0.0
