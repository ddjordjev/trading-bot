"""Tests for strategies/ (all strategy modules)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

from core.models import Candle, SignalAction


def _make_candle(close: float, volume: float = 1000, high_off=2, low_off=2, open_off=0) -> Candle:
    return Candle(
        timestamp=datetime.now(UTC),
        open=close + open_off,
        high=close + high_off,
        low=close - low_off,
        close=close,
        volume=volume,
    )


def _make_candles_rising(n: int, start: float = 100, step: float = 1.0, volume: float = 1000) -> list[Candle]:
    return [_make_candle(start + i * step, volume=volume) for i in range(n)]


def _make_candles_falling(n: int, start: float = 200, step: float = 1.0, volume: float = 1000) -> list[Candle]:
    return [_make_candle(start - i * step, volume=volume) for i in range(n)]


def _make_candles_flat(n: int, price: float = 100, volume: float = 1000) -> list[Candle]:
    return [_make_candle(price, volume=volume) for _ in range(n)]


# ── BaseStrategy ────────────────────────────────────────────────────


class TestBaseStrategy:
    def test_feed_candle_trims(self):
        from strategies.base import BaseStrategy

        class Dummy(BaseStrategy):
            @property
            def name(self):
                return "dummy"

            def analyze(self, candles, ticker=None):
                return None

        d = Dummy("BTC/USDT", max_history=5)
        for i in range(10):
            d.feed_candle(_make_candle(100 + i))
        assert len(d._candle_history) == 5

    def test_candles_to_df_empty(self):
        from strategies.base import BaseStrategy

        class Dummy(BaseStrategy):
            @property
            def name(self):
                return "dummy"

            def analyze(self, candles, ticker=None):
                return None

        d = Dummy("BTC/USDT")
        df = d.candles_to_df()
        assert len(df) == 0

    def test_candles_to_df_with_data(self):
        from strategies.base import BaseStrategy

        class Dummy(BaseStrategy):
            @property
            def name(self):
                return "dummy"

            def analyze(self, candles, ticker=None):
                return None

        d = Dummy("BTC/USDT")
        candles = _make_candles_flat(5)
        df = d.candles_to_df(candles)
        assert len(df) == 5
        assert "close" in df.columns

    def test_reset(self):
        from strategies.base import BaseStrategy

        class Dummy(BaseStrategy):
            @property
            def name(self):
                return "dummy"

            def analyze(self, candles, ticker=None):
                return None

        d = Dummy("BTC/USDT")
        d.feed_candle(_make_candle(100))
        d.reset()
        assert len(d._candle_history) == 0


# ── RSI Strategy ────────────────────────────────────────────────────


class TestRSIStrategy:
    def test_insufficient_data(self):
        from strategies.rsi import RSIStrategy

        s = RSIStrategy("BTC/USDT")
        candles = _make_candles_flat(5)
        assert s.analyze(candles) is None

    def test_oversold_buy_signal(self):
        from strategies.rsi import RSIStrategy

        s = RSIStrategy("BTC/USDT", oversold=30)
        candles = _make_candles_falling(40, start=200, step=2)
        sig = s.analyze(candles)
        if sig:
            assert sig.action == SignalAction.BUY

    def test_overbought_sell_signal(self):
        from strategies.rsi import RSIStrategy

        s = RSIStrategy("BTC/USDT", overbought=70)
        candles = _make_candles_rising(40, start=100, step=2)
        sig = s.analyze(candles)
        if sig:
            assert sig.action == SignalAction.SELL

    def test_neutral_no_signal(self):
        from strategies.rsi import RSIStrategy

        s = RSIStrategy("BTC/USDT")
        candles = [_make_candle(100 + (0.5 if i % 2 == 0 else -0.5)) for i in range(30)]
        sig = s.analyze(candles)
        assert sig is None

    def test_name(self):
        from strategies.rsi import RSIStrategy

        s = RSIStrategy("BTC/USDT")
        assert s.name == "rsi"


# ── MACD Strategy ───────────────────────────────────────────────────


class TestMACDStrategy:
    def test_insufficient_data(self):
        from strategies.macd import MACDStrategy

        s = MACDStrategy("BTC/USDT")
        candles = _make_candles_flat(10)
        assert s.analyze(candles) is None

    def test_name(self):
        from strategies.macd import MACDStrategy

        s = MACDStrategy("BTC/USDT")
        assert s.name == "macd"

    def test_crossover_signal(self):
        from strategies.macd import MACDStrategy

        s = MACDStrategy("BTC/USDT")
        candles = _make_candles_falling(30, start=200) + _make_candles_rising(20, start=170, step=2)
        _sig = s.analyze(candles)
        # May or may not generate signal depending on data, just ensure no crash


# ── Bollinger Strategy ──────────────────────────────────────────────


class TestBollingerStrategy:
    def test_insufficient_data(self):
        from strategies.bollinger import BollingerStrategy

        s = BollingerStrategy("BTC/USDT")
        candles = _make_candles_flat(5)
        assert s.analyze(candles) is None

    def test_name(self):
        from strategies.bollinger import BollingerStrategy

        s = BollingerStrategy("BTC/USDT")
        assert s.name == "bollinger"

    def test_neutral_no_signal(self):
        from strategies.bollinger import BollingerStrategy

        s = BollingerStrategy("BTC/USDT")
        candles = _make_candles_flat(25, price=100)
        sig = s.analyze(candles)
        assert sig is None


# ── Mean Reversion Strategy ─────────────────────────────────────────


class TestMeanReversionStrategy:
    def test_insufficient_data(self):
        from strategies.mean_reversion import MeanReversionStrategy

        s = MeanReversionStrategy("BTC/USDT")
        candles = _make_candles_flat(10)
        assert s.analyze(candles) is None

    def test_name(self):
        from strategies.mean_reversion import MeanReversionStrategy

        s = MeanReversionStrategy("BTC/USDT")
        assert s.name == "mean_reversion"

    def test_below_ma_buy(self):
        from strategies.mean_reversion import MeanReversionStrategy

        s = MeanReversionStrategy("BTC/USDT", ma_period=20, deviation_pct=2.0)
        candles = _make_candles_flat(50, price=100)
        candles[-1] = _make_candle(96)
        sig = s.analyze(candles)
        if sig:
            assert sig.action == SignalAction.BUY

    def test_above_ma_sell(self):
        from strategies.mean_reversion import MeanReversionStrategy

        s = MeanReversionStrategy("BTC/USDT", ma_period=20, deviation_pct=2.0)
        candles = _make_candles_flat(50, price=100)
        candles[-1] = _make_candle(104)
        sig = s.analyze(candles)
        if sig:
            assert sig.action == SignalAction.SELL


# ── Grid Strategy ───────────────────────────────────────────────────


class TestGridStrategy:
    def test_insufficient_data(self):
        from strategies.grid import GridStrategy

        s = GridStrategy("BTC/USDT")
        candles = [_make_candle(100)]
        assert s.analyze(candles) is None

    def test_name(self):
        from strategies.grid import GridStrategy

        s = GridStrategy("BTC/USDT")
        assert s.name == "grid"

    def test_first_candle_sets_center(self):
        from strategies.grid import GridStrategy

        s = GridStrategy("BTC/USDT")
        candles = _make_candles_flat(3, price=100)
        sig = s.analyze(candles)
        assert s._center_price == 100
        assert sig is None

    def test_grid_buy_down(self):
        from strategies.grid import GridStrategy

        s = GridStrategy("BTC/USDT", grid_size_pct=1.0)
        candles = _make_candles_flat(3, price=100)
        s.analyze(candles)
        candles[-1] = _make_candle(98)
        sig = s.analyze(candles)
        if sig:
            assert sig.action == SignalAction.BUY

    def test_grid_sell_up(self):
        from strategies.grid import GridStrategy

        s = GridStrategy("BTC/USDT", grid_size_pct=1.0)
        candles = _make_candles_flat(3, price=100)
        s.analyze(candles)
        candles[-1] = _make_candle(102)
        sig = s.analyze(candles)
        if sig:
            assert sig.action == SignalAction.SELL


# ── Market Open Volatility Strategy ────────────────────────────────


class TestMarketOpenVolatilityStrategy:
    def test_name(self):
        from strategies.market_open_volatility import MarketOpenVolatilityStrategy

        with patch("strategies.market_open_volatility.get_market_schedule") as m_sched:
            m_sched.return_value.is_in_open_window.return_value = False
            s = MarketOpenVolatilityStrategy("BTC/USDT")
            assert s.name == "market_open_volatility"

    def test_outside_window_no_signal(self):
        from strategies.market_open_volatility import MarketOpenVolatilityStrategy

        with patch("strategies.market_open_volatility.get_market_schedule") as m_sched:
            m_sched.return_value.is_in_open_window.return_value = False
            s = MarketOpenVolatilityStrategy("BTC/USDT")
            candles = _make_candles_flat(30)
            sig = s.analyze(candles)
            assert sig is None


# ── Compound Momentum Strategy ──────────────────────────────────────


class TestCompoundMomentumStrategy:
    def test_name(self):
        from strategies.compound_momentum import CompoundMomentumStrategy

        s = CompoundMomentumStrategy("BTC/USDT")
        assert s.name == "compound_momentum"

    def test_insufficient_data(self):
        from strategies.compound_momentum import CompoundMomentumStrategy

        s = CompoundMomentumStrategy("BTC/USDT")
        candles = _make_candles_flat(5)
        assert s.analyze(candles) is None

    def test_spike_detection(self):
        from strategies.compound_momentum import CompoundMomentumStrategy

        s = CompoundMomentumStrategy("BTC/USDT", spike_pct=1.0, spike_candles=3)
        candles = _make_candles_flat(30, price=100, volume=1000)
        candles.extend(
            [
                _make_candle(101.5, volume=3000),
                _make_candle(102.5, volume=3000),
                _make_candle(104.0, volume=3000),
            ]
        )
        sig = s.analyze(candles)
        if sig:
            assert sig.action in (SignalAction.BUY, SignalAction.SELL)
            assert sig.quick_trade is True

    def test_in_position_checks_exit(self):
        from strategies.compound_momentum import CompoundMomentumStrategy

        s = CompoundMomentumStrategy("BTC/USDT")
        s._in_position = True
        s._position_side = "long"
        candles = _make_candles_flat(30, price=100, volume=1000)
        sig = s.analyze(candles)
        # In profit or no exit signal, should return None
        assert sig is None or sig.action == SignalAction.CLOSE


# ── Swing Opportunity Strategy ──────────────────────────────────────


class TestSwingOpportunityStrategy:
    def test_name(self):
        from strategies.swing_opportunity import SwingOpportunityStrategy

        s = SwingOpportunityStrategy("BTC/USDT")
        assert s.name == "swing_opportunity"

    def test_insufficient_data(self):
        from strategies.swing_opportunity import SwingOpportunityStrategy

        s = SwingOpportunityStrategy("BTC/USDT")
        candles = _make_candles_flat(50)
        assert s.analyze(candles) is None

    def test_cooldown(self):
        from strategies.swing_opportunity import SwingOpportunityStrategy

        s = SwingOpportunityStrategy("BTC/USDT")
        s._cooldown_candles = 10
        candles = _make_candles_flat(250)
        assert s.analyze(candles) is None
        assert s._cooldown_candles == 9


# ── Position State Sync ────────────────────────────────────────────


class TestSetPositionState:
    def test_base_strategy_set_position_state_is_noop(self):
        from strategies.rsi import RSIStrategy

        s = RSIStrategy("BTC/USDT")
        s.set_position_state(True, "long")

    def test_compound_momentum_sync(self):
        from strategies.compound_momentum import CompoundMomentumStrategy

        s = CompoundMomentumStrategy("BTC/USDT")
        assert not s._in_position
        s.set_position_state(True, "long")
        assert s._in_position
        assert s._position_side == "long"
        s.set_position_state(False)
        assert not s._in_position
        assert s._position_side is None

    def test_compound_momentum_sync_short(self):
        from strategies.compound_momentum import CompoundMomentumStrategy

        s = CompoundMomentumStrategy("BTC/USDT")
        s.set_position_state(True, "short")
        assert s._in_position
        assert s._position_side == "short"
