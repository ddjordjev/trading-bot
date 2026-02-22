"""Tests for per-bot-type lightweight validators."""

from __future__ import annotations

from datetime import UTC, datetime

from core.models import Candle, Ticker
from validators import (
    ExtremeValidator,
    IndicatorsValidator,
    MeanRevValidator,
    MomentumValidator,
    SwingValidator,
    get_validator,
)


def _make_candles(prices: list[float], volumes: list[float] | None = None) -> list[Candle]:
    now = datetime.now(UTC)
    vols = volumes or [1000.0] * len(prices)
    return [
        Candle(
            timestamp=now,
            open=p * 0.999,
            high=p * 1.001,
            low=p * 0.998,
            close=p,
            volume=v,
        )
        for p, v in zip(prices, vols, strict=True)
    ]


def _make_ticker(symbol: str = "BTC/USDT", last: float = 50000.0, spread_pct: float = 0.05) -> Ticker:
    half_spread = last * spread_pct / 100 / 2
    return Ticker(
        symbol=symbol,
        bid=last - half_spread,
        ask=last + half_spread,
        last=last,
        volume_24h=1e9,
        change_pct_24h=2.0,
        timestamp=datetime.now(UTC),
    )


class TestGetValidator:
    def test_momentum_style(self):
        v = get_validator("momentum")
        assert isinstance(v, MomentumValidator)

    def test_extreme_style(self):
        v = get_validator("extreme")
        assert isinstance(v, ExtremeValidator)

    def test_meanrev_style(self):
        v = get_validator("meanrev")
        assert isinstance(v, MeanRevValidator)

    def test_swing_style(self):
        v = get_validator("swing")
        assert isinstance(v, SwingValidator)

    def test_indicators_style(self):
        v = get_validator("indicators")
        assert isinstance(v, IndicatorsValidator)

    def test_unknown_defaults_to_momentum(self):
        v = get_validator("unknown_style")
        assert isinstance(v, MomentumValidator)


class TestExtremeValidator:
    def test_insufficient_candles_returns_invalid(self):
        v = ExtremeValidator()
        candles = _make_candles([100.0] * 5)
        result = v.validate(candles, None, "long", "compound_momentum")
        assert not result.valid
        assert "insufficient" in result.reason

    def test_valid_extreme_long_move(self):
        v = ExtremeValidator()
        prices = [100.0] * 15 + [100.0, 100.5, 101.0, 101.5, 102.0]
        volumes = [100.0] * 15 + [200.0, 250.0, 300.0, 350.0, 400.0]
        candles = _make_candles(prices, volumes)
        ticker = _make_ticker(last=102.0)
        result = v.validate(candles, ticker, "long", "compound_momentum")
        assert result.valid

    def test_stalled_long_momentum(self):
        v = ExtremeValidator()
        prices = [100.0] * 15 + [100.0, 100.0, 100.0, 100.0, 100.0]
        volumes = [100.0] * 15 + [200.0, 250.0, 300.0, 350.0, 400.0]
        candles = _make_candles(prices, volumes)
        result = v.validate(candles, None, "long", "compound_momentum")
        assert not result.valid
        assert "stalled" in result.reason

    def test_wide_spread_rejected(self):
        v = ExtremeValidator()
        prices = [100.0] * 15 + [100.0, 100.5, 101.0, 101.5, 102.0]
        volumes = [100.0] * 15 + [200.0, 250.0, 300.0, 350.0, 400.0]
        candles = _make_candles(prices, volumes)
        ticker = _make_ticker(last=102.0, spread_pct=0.5)
        result = v.validate(candles, ticker, "long", "compound_momentum")
        assert not result.valid
        assert "spread" in result.reason


class TestMomentumValidator:
    def test_insufficient_candles(self):
        v = MomentumValidator()
        candles = _make_candles([100.0] * 10)
        result = v.validate(candles, None, "long", "compound_momentum")
        assert not result.valid

    def test_valid_momentum_long(self):
        v = MomentumValidator()
        prices = [
            100.0,
            99.0,
            100.5,
            99.5,
            101.0,
            99.0,
            100.0,
            99.5,
            100.5,
            99.8,
            101.0,
            100.0,
            100.5,
            100.2,
            101.0,
            100.5,
            100.8,
            100.3,
            101.0,
            100.5,
            100.8,
            100.5,
            101.0,
            101.2,
            101.5,
        ]
        candles = _make_candles(prices)
        result = v.validate(candles, None, "long", "compound_momentum")
        assert result.valid

    def test_declining_volume_rejected(self):
        v = MomentumValidator()
        prices = [
            100.0,
            99.0,
            100.5,
            99.5,
            101.0,
            99.0,
            100.0,
            99.5,
            100.5,
            99.8,
            101.0,
            100.0,
            100.5,
            100.2,
            101.0,
            100.5,
            100.8,
            100.3,
            101.0,
            100.5,
            100.8,
            100.5,
            101.0,
            101.2,
            101.5,
        ]
        volumes = [1000.0] * 22 + [50.0, 50.0, 50.0]
        candles = _make_candles(prices, volumes)
        result = v.validate(candles, None, "long", "compound_momentum")
        assert not result.valid
        assert "volume" in result.reason


class TestIndicatorsValidator:
    def test_insufficient_candles(self):
        v = IndicatorsValidator()
        candles = _make_candles([100.0] * 20)
        result = v.validate(candles, None, "long", "rsi")
        assert not result.valid

    def test_valid_long_signal(self):
        v = IndicatorsValidator()
        prices = [
            100.0,
            98.0,
            101.0,
            99.0,
            102.0,
            98.5,
            100.0,
            99.0,
            101.0,
            99.5,
            100.5,
            99.0,
            101.0,
            99.5,
            100.5,
            99.5,
            101.0,
            100.0,
            101.5,
            100.5,
            101.0,
            100.0,
            101.5,
            100.5,
            101.0,
            100.0,
            101.5,
            100.5,
            101.0,
            100.5,
            101.5,
            101.0,
            101.5,
            101.0,
            101.5,
        ]
        candles = _make_candles(prices)
        result = v.validate(candles, None, "long", "rsi")
        assert result.valid


class TestMeanRevValidator:
    def test_insufficient_candles(self):
        v = MeanRevValidator()
        candles = _make_candles([100.0] * 10)
        result = v.validate(candles, None, "long", "mean_reversion")
        assert not result.valid

    def test_price_still_extended_below_mean(self):
        v = MeanRevValidator()
        prices = [100.0] * 18 + [95.0, 94.0]
        candles = _make_candles(prices)
        result = v.validate(candles, None, "long", "mean_reversion")
        assert result.valid

    def test_price_reverted_above_mean_rejected(self):
        v = MeanRevValidator()
        prices = [100.0] * 20
        candles = _make_candles(prices)
        result = v.validate(candles, None, "long", "mean_reversion")
        assert not result.valid


class TestSwingValidator:
    def test_insufficient_candles(self):
        v = SwingValidator()
        candles = _make_candles([100.0] * 20)
        result = v.validate(candles, None, "long", "swing_opportunity")
        assert not result.valid

    def test_valid_long_within_range(self):
        v = SwingValidator()
        prices = ([110.0] * 15) + (
            [
                100.0,
                101.0,
                102.0,
                103.0,
                104.0,
                105.0,
                104.0,
                103.0,
                104.0,
                105.0,
                104.0,
                103.0,
                104.0,
                105.0,
                106.0,
                105.0,
                104.0,
                105.0,
                104.0,
                105.0,
            ]
        )
        candles = _make_candles(prices)
        result = v.validate(candles, None, "long", "swing_opportunity")
        assert result.valid

    def test_price_near_resistance_rejected(self):
        v = SwingValidator()
        base = [100.0] * 15
        climb = [100.0 + i * 2.0 for i in range(20)]
        prices = base + climb
        candles = _make_candles(prices)
        result = v.validate(candles, None, "long", "swing_opportunity")
        assert not result.valid
        assert "resistance" in result.reason
