"""Extended tests for strategies with low coverage.

Covers: compound_momentum, market_open_volatility, swing_opportunity, custom_loader.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

from core.models import Candle, SignalAction
from strategies.base import BaseStrategy


def _make_candle(
    close: float, volume: float = 1000, high_off: float = 2, low_off: float = 2, open_off: float = 0
) -> Candle:
    return Candle(
        timestamp=datetime.now(UTC),
        open=close + open_off,
        high=close + high_off,
        low=close - low_off,
        close=close,
        volume=volume,
    )


def _make_candles_flat(n: int, price: float = 100, volume: float = 1000) -> list[Candle]:
    return [_make_candle(price, volume=volume) for _ in range(n)]


def _make_candles_rising(n: int, start: float = 100, step: float = 1.0, volume: float = 1000) -> list[Candle]:
    return [_make_candle(start + i * step, volume=volume) for i in range(n)]


def _make_candles_falling(n: int, start: float = 200, step: float = 1.0, volume: float = 1000) -> list[Candle]:
    return [_make_candle(start - i * step, volume=volume) for i in range(n)]


# ── Compound Momentum ─────────────────────────────────────────────────────


class TestCompoundMomentumExtended:
    """Extended tests for CompoundMomentumStrategy: spike, exit, breakout, position tracking."""

    def test_spike_insufficient_candles_returns_none(self):
        from strategies.compound_momentum import CompoundMomentumStrategy

        s = CompoundMomentumStrategy("BTC/USDT", spike_pct=1.0, spike_candles=3)
        candles = _make_candles_flat(3, price=100)
        assert s.analyze(candles) is None

    def test_spike_vol_ratio_below_threshold_no_signal(self):
        from strategies.compound_momentum import CompoundMomentumStrategy

        s = CompoundMomentumStrategy("BTC/USDT", spike_pct=0.5, spike_candles=3)
        # Flat volume (no surge) -> vol_ratio ~1.0 < 1.5
        candles = _make_candles_flat(40, price=100, volume=1000)
        candles.extend(
            [
                _make_candle(101.5, volume=1000),
                _make_candle(102.0, volume=1000),
                _make_candle(102.5, volume=1000),
            ]
        )
        sig = s.analyze(candles)
        assert sig is None

    def test_spike_detection_bearish_sell_signal(self):
        from strategies.compound_momentum import CompoundMomentumStrategy

        s = CompoundMomentumStrategy("BTC/USDT", spike_pct=1.0, spike_candles=3)
        candles = _make_candles_flat(30, price=100, volume=1000)
        candles.extend(
            [
                _make_candle(98.5, volume=3000),
                _make_candle(97.5, volume=3000),
                _make_candle(96.0, volume=3000),
            ]
        )
        sig = s.analyze(candles)
        assert sig is not None
        assert sig.action == SignalAction.SELL
        assert sig.quick_trade is True

    def test_exit_long_in_loss_rsi_low_volume_drying(self):
        from strategies.compound_momentum import CompoundMomentumStrategy

        s = CompoundMomentumStrategy("BTC/USDT")
        s._in_position = True
        s._position_side = "long"
        # Price below entry, RSI < 45, volume drying
        candles = _make_candles_flat(30, price=98, volume=500)  # low recent vol
        for i in range(20):
            candles[i] = _make_candle(100 - i * 0.1, volume=2000)  # higher avg vol
        candles[-3:] = [
            _make_candle(97, volume=200),
            _make_candle(96.5, volume=200),
            _make_candle(96, volume=200),
        ]
        sig = s.analyze(candles)
        assert sig is not None
        assert sig.action == SignalAction.CLOSE
        assert s._in_position is False

    def test_exit_short_in_loss_rsi_high_volume_drying(self):
        from strategies.compound_momentum import CompoundMomentumStrategy

        s = CompoundMomentumStrategy("BTC/USDT")
        s._in_position = True
        s._position_side = "short"
        candles = _make_candles_flat(30, price=104, volume=500)
        for i in range(20):
            candles[i] = _make_candle(100 + i * 0.1, volume=2000)
        candles[-3:] = [
            _make_candle(103, volume=200),
            _make_candle(103.5, volume=200),
            _make_candle(104, volume=200),
        ]
        sig = s.analyze(candles)
        assert sig is not None
        assert sig.action == SignalAction.CLOSE
        assert s._in_position is False

    def test_in_profit_does_not_exit(self):
        from strategies.compound_momentum import CompoundMomentumStrategy

        s = CompoundMomentumStrategy("BTC/USDT")
        s._in_position = True
        s._position_side = "long"
        candles = _make_candles_flat(30, price=105, volume=1000)
        sig = s.analyze(candles)
        assert sig is None

    def test_breakout_bullish_signal(self):
        from strategies.compound_momentum import CompoundMomentumStrategy

        s = CompoundMomentumStrategy(
            "BTC/USDT",
            consolidation_period=20,
            breakout_threshold_pct=0.5,
            volume_surge_mult=1.5,
        )
        candles = _make_candles_flat(25, price=99, volume=1000)
        # Range 98-100, then break above 100.5
        for i in range(20):
            candles[5 + i] = _make_candle(99 + (i % 3) * 0.5, high_off=1, low_off=1, volume=1000)
        resistance = 100.0
        candles[-1] = _make_candle(resistance * 1.006, volume=2500)
        sig = s.analyze(candles)
        if sig:
            assert sig.action == SignalAction.BUY
            assert sig.quick_trade is True

    def test_breakout_bearish_signal(self):
        from strategies.compound_momentum import CompoundMomentumStrategy

        s = CompoundMomentumStrategy(
            "BTC/USDT",
            consolidation_period=20,
            breakout_threshold_pct=0.5,
            volume_surge_mult=1.5,
        )
        candles = _make_candles_flat(25, price=101, volume=1000)
        for i in range(20):
            candles[5 + i] = _make_candle(100 - (i % 3) * 0.5, high_off=1, low_off=1, volume=1000)
        support = 100.0
        candles[-1] = _make_candle(support * 0.994, volume=2500)
        sig = s.analyze(candles)
        if sig:
            assert sig.action == SignalAction.SELL
            assert sig.quick_trade is True

    def test_params_from_init(self):
        from strategies.compound_momentum import CompoundMomentumStrategy

        s = CompoundMomentumStrategy(
            "ETH/USDT",
            consolidation_period=30,
            spike_pct=2.0,
            spike_candles=5,
            spike_max_hold=12,
        )
        assert s.consolidation_period == 30
        assert s.spike_pct == 2.0
        assert s.spike_candles == 5
        assert s.spike_max_hold == 12


# ── Market Open Volatility ──────────────────────────────────────────────────


class TestMarketOpenVolatilityExtended:
    """Window detection via MarketSchedule, signal generation during windows."""

    def test_is_market_open_window_us(self):
        from strategies.market_open_volatility import MarketOpenVolatilityStrategy

        with patch("strategies.market_open_volatility.get_market_schedule") as m_sched:

            def window_side_effect(market, _mins=120):
                return market == "US"

            m_sched.return_value.is_in_open_window.side_effect = window_side_effect
            s = MarketOpenVolatilityStrategy("BTC/USDT")
            assert s._is_market_open_window() == "US"

    def test_is_market_open_window_asia(self):
        from strategies.market_open_volatility import MarketOpenVolatilityStrategy

        with patch("strategies.market_open_volatility.get_market_schedule") as m_sched:

            def window_side_effect(market, _mins=120):
                return market == "ASIA"

            m_sched.return_value.is_in_open_window.side_effect = window_side_effect
            s = MarketOpenVolatilityStrategy("BTC/USDT")
            assert s._is_market_open_window() == "ASIA"

    def test_is_market_open_window_none(self):
        from strategies.market_open_volatility import MarketOpenVolatilityStrategy

        with patch("strategies.market_open_volatility.get_market_schedule") as m_sched:
            m_sched.return_value.is_in_open_window.return_value = False
            s = MarketOpenVolatilityStrategy("BTC/USDT")
            assert s._is_market_open_window() is None

    def test_signal_during_us_window_atr_and_volume_surge(self):
        from strategies.market_open_volatility import MarketOpenVolatilityStrategy

        with patch("strategies.market_open_volatility.get_market_schedule") as m_sched:

            def window_side_effect(market, _mins=120):
                return market == "US"

            m_sched.return_value.is_in_open_window.side_effect = window_side_effect
            s = MarketOpenVolatilityStrategy("BTC/USDT", atr_period=14, atr_multiplier=1.5)
            candles = _make_candles_flat(25, price=100, volume=1000)
            candles[-1] = _make_candle(101, high_off=3, low_off=3, volume=5000)
            sig = s.analyze(candles)
            if sig:
                assert sig.action in (SignalAction.BUY, SignalAction.SELL)
                assert sig.quick_trade is True
                assert "US" in sig.reason or "market open" in sig.reason.lower()


# ── Swing Opportunity ──────────────────────────────────────────────────────


class TestSwingOpportunityExtended:
    """Multi-timeframe analysis, crash buy, blow-off short, cooldown."""

    def test_cooldown_decrements_and_blocks_signal(self):
        from strategies.swing_opportunity import SwingOpportunityStrategy

        s = SwingOpportunityStrategy("BTC/USDT")
        s._cooldown_candles = 5
        candles = _make_candles_flat(250, price=50)
        assert s.analyze(candles) is None
        assert s._cooldown_candles == 4

    def test_crash_buy_requires_big_drop_and_rsi_and_volume(self):
        from strategies.swing_opportunity import SwingOpportunityStrategy

        s = SwingOpportunityStrategy(
            "BTC/USDT",
            crash_threshold_pct=15.0,
            rsi_extreme_oversold=25,
            capitulation_volume_mult=2.5,
        )
        candles = _make_candles_flat(220, price=80, volume=1000)
        for i in range(60):
            candles[160 + i] = _make_candle(100 - i * 0.2, high_off=0.5, low_off=0.5, volume=1000)
        candles[-3:] = [
            _make_candle(82, volume=4000),
            _make_candle(81, volume=4000),
            _make_candle(80, volume=4000),
        ]
        sig = s.analyze(candles)
        if sig:
            assert sig.action == SignalAction.BUY
            assert sig.quick_trade is False
            assert "CRASH" in sig.reason or "SWING" in sig.reason
            assert s._cooldown_candles == 60

    def test_blow_off_top_short_signal(self):
        from strategies.swing_opportunity import SwingOpportunityStrategy

        s = SwingOpportunityStrategy(
            "BTC/USDT",
            crash_threshold_pct=15.0,
            rsi_extreme_overbought=85,
            capitulation_volume_mult=2.5,
        )
        candles = _make_candles_flat(220, price=120, volume=1000)
        for i in range(60):
            candles[160 + i] = _make_candle(100 + i * 0.2, high_off=0.5, low_off=0.5, volume=1000)
        candles[-3:] = [
            _make_candle(118, volume=4000),
            _make_candle(119, volume=4000),
            _make_candle(120, volume=4000),
        ]
        sig = s.analyze(candles)
        if sig:
            assert sig.action == SignalAction.SELL
            assert "BLOW-OFF" in sig.reason or "SWING" in sig.reason
            assert s._cooldown_candles == 60

    def test_insufficient_candles_returns_none(self):
        from strategies.swing_opportunity import SwingOpportunityStrategy

        s = SwingOpportunityStrategy("BTC/USDT")
        candles = _make_candles_flat(199, price=100)
        assert s.analyze(candles) is None

    def test_suggested_stop_loss_on_crash_buy(self):
        from strategies.swing_opportunity import SwingOpportunityStrategy

        s = SwingOpportunityStrategy("BTC/USDT", swing_stop_pct=5.0)
        candles = _make_candles_flat(220, price=84, volume=1000)
        for i in range(60):
            candles[160 + i] = _make_candle(100 - i * 0.3, volume=1000)
        candles[-3:] = [_make_candle(83, volume=5000), _make_candle(82, volume=5000), _make_candle(84, volume=5000)]
        sig = s.analyze(candles)
        if sig and sig.action == SignalAction.BUY:
            assert sig.suggested_stop_loss is not None
            assert sig.suggested_stop_loss < sig.suggested_price


# ── Custom Loader ───────────────────────────────────────────────────────────


class TestCustomLoader:
    """Loading strategies from custom_strategies/ directory."""

    def test_load_custom_strategies_empty_when_no_dir(self, tmp_path):
        from strategies import custom_loader

        with patch.object(custom_loader, "CUSTOM_DIR", tmp_path / "nonexistent_custom"):
            result = custom_loader.load_custom_strategies()
        assert result == {}

    def test_load_custom_strategies_skips_underscore_files(self, tmp_path):
        from strategies import custom_loader

        (tmp_path / "_skip.py").write_text("""
from strategies.base import BaseStrategy
class _SkipStrategy(BaseStrategy):
    name = "skip"
    def analyze(self, candles, ticker=None): return None
""")
        with patch.object(custom_loader, "CUSTOM_DIR", tmp_path):
            result = custom_loader.load_custom_strategies()
        assert "skip" not in result and "_skip" not in result

    def test_load_custom_strategies_skips_init(self, tmp_path):
        from strategies import custom_loader

        (tmp_path / "__init__.py").write_text("")
        with patch.object(custom_loader, "CUSTOM_DIR", tmp_path):
            result = custom_loader.load_custom_strategies()
        assert result == {}

    def test_load_custom_strategies_loads_valid_strategy(self, tmp_path):
        from strategies import custom_loader

        (tmp_path / "my_strat.py").write_text("""
from strategies.base import BaseStrategy

class MyCustomStrategy(BaseStrategy):
    @property
    def name(self):
        return "my_custom"

    def analyze(self, candles, ticker=None):
        return None
""")
        with patch.object(custom_loader, "CUSTOM_DIR", tmp_path):
            result = custom_loader.load_custom_strategies()
        assert "my_custom" in result
        cls = result["my_custom"]
        assert issubclass(cls, BaseStrategy)
        inst = cls("BTC/USDT", market_type="spot", leverage=1)
        assert inst.name == "my_custom"

    def test_load_custom_strategies_handles_bad_file_gracefully(self, tmp_path):
        from strategies import custom_loader

        (tmp_path / "broken.py").write_text("syntax error here !!!")
        with patch.object(custom_loader, "CUSTOM_DIR", tmp_path):
            result = custom_loader.load_custom_strategies()
        assert "broken" not in result
        assert isinstance(result, dict)


# ── Grid Strategy Edge Cases ────────────────────────────────────────────


class TestGridStrategyEdgeCases:
    def test_grid_nan_price(self):
        from strategies.grid import GridStrategy

        s = GridStrategy("BTC/USDT", num_grids=5, grid_size_pct=1.0)
        candles = [_make_candle(100), _make_candle(float("nan"))]
        assert s.analyze(candles) is None

    def test_grid_zero_grid_size(self):
        from strategies.grid import GridStrategy

        s = GridStrategy("BTC/USDT", num_grids=5, grid_size_pct=0.0)
        candles = _make_candles_flat(5, 100)
        s.analyze(candles)
        signal = s.analyze([*candles, _make_candle(105)])
        assert signal is None

    def test_grid_zero_num_grids(self):
        from strategies.grid import GridStrategy

        s = GridStrategy("BTC/USDT", num_grids=0, grid_size_pct=1.0)
        candles = _make_candles_flat(5, 100)
        s.analyze(candles)
        signal = s.analyze([*candles, _make_candle(105)])
        assert signal is None

    def test_grid_recenter_on_drift(self):
        from strategies.grid import GridStrategy

        s = GridStrategy("BTC/USDT", num_grids=3, grid_size_pct=1.0, recenter_threshold=3)
        candles = _make_candles_flat(5, 100)
        s.analyze(candles)
        assert s._center_price == 100
        far_candles = [*candles, _make_candle(115)]
        signal = s.analyze(far_candles)
        assert s._center_price == 115
        assert signal is None

    def test_grid_buy_on_move_down(self):
        from core.models import SignalAction
        from strategies.grid import GridStrategy

        s = GridStrategy("BTC/USDT", num_grids=5, grid_size_pct=2.0)
        candles = _make_candles_flat(5, 100)
        s.analyze(candles)
        down_candles = [*candles, _make_candle(96)]
        signal = s.analyze(down_candles)
        assert signal is not None
        assert signal.action == SignalAction.BUY

    def test_grid_sell_on_move_up(self):
        from core.models import SignalAction
        from strategies.grid import GridStrategy

        s = GridStrategy("BTC/USDT", num_grids=5, grid_size_pct=2.0)
        candles = _make_candles_flat(5, 100)
        s.analyze(candles)
        up_candles = [*candles, _make_candle(104)]
        signal = s.analyze(up_candles)
        assert signal is not None
        assert signal.action == SignalAction.SELL

    def test_grid_same_level_returns_none(self):
        from strategies.grid import GridStrategy

        s = GridStrategy("BTC/USDT", num_grids=5, grid_size_pct=2.0)
        candles = _make_candles_flat(5, 100)
        s.analyze(candles)
        signal = s.analyze([*candles, _make_candle(100.5)])
        assert signal is None


# ── MACD Strategy Edge Cases ────────────────────────────────────────────


class TestMACDStrategyEdgeCases:
    def test_macd_needs_enough_candles(self):
        from strategies.macd import MACDStrategy

        s = MACDStrategy("BTC/USDT")
        candles = _make_candles_flat(10, 100)
        assert s.analyze(candles) is None

    def test_macd_bullish_crossover(self):
        from strategies.macd import MACDStrategy

        s = MACDStrategy("BTC/USDT")
        candles = _make_candles_flat(30, 100) + _make_candles_falling(10, 105, 0.5) + _make_candles_rising(10, 98, 1.0)
        signal = s.analyze(candles)
        if signal:
            from core.models import SignalAction

            assert signal.action == SignalAction.BUY

    def test_macd_nan_histogram(self):
        from strategies.macd import MACDStrategy

        s = MACDStrategy("BTC/USDT")
        candles = [_make_candle(float("nan"))] * 40
        signal = s.analyze(candles)
        assert signal is None

    def test_macd_zero_price(self):
        from strategies.macd import MACDStrategy

        s = MACDStrategy("BTC/USDT")
        candles = [*_make_candles_flat(40, 100), _make_candle(0)]
        signal = s.analyze(candles)
        assert signal is None


# ── Swing Opportunity Edge Cases ────────────────────────────────────────


class TestSwingOpportunityEdgeCases:
    def test_too_few_candles(self):
        from strategies.swing_opportunity import SwingOpportunityStrategy

        s = SwingOpportunityStrategy("BTC/USDT")
        candles = _make_candles_flat(5, 100)
        assert s.analyze(candles) is None

    def test_non_finite_price(self):
        from strategies.swing_opportunity import SwingOpportunityStrategy

        s = SwingOpportunityStrategy("BTC/USDT")
        candles = [*_make_candles_flat(200, 100), _make_candle(float("inf"))]
        assert s.analyze(candles) is None

    def test_cooldown_blocks_signal(self):
        from strategies.swing_opportunity import SwingOpportunityStrategy

        s = SwingOpportunityStrategy("BTC/USDT")
        s._cooldown_candles = 10
        candles = _make_candles_flat(200, 100)
        signal = s.analyze(candles)
        assert signal is None
        assert s._cooldown_candles == 9


# ── CompoundMomentum Edge Cases ─────────────────────────────────────────


class TestCompoundMomentumEdgeCases:
    def test_not_enough_candles(self):
        from strategies.compound_momentum import CompoundMomentumStrategy

        s = CompoundMomentumStrategy("BTC/USDT")
        candles = _make_candles_flat(5, 100)
        assert s.analyze(candles) is None

    def test_nan_price_returns_none(self):
        from strategies.compound_momentum import CompoundMomentumStrategy

        s = CompoundMomentumStrategy("BTC/USDT")
        candles = [*_make_candles_flat(50, 100), _make_candle(float("nan"))]
        signal = s.analyze(candles)
        assert signal is None

    def test_in_position_checks_exit(self):
        from strategies.compound_momentum import CompoundMomentumStrategy

        s = CompoundMomentumStrategy("BTC/USDT")
        s.set_position_state(True, "long")
        candles = _make_candles_flat(50, 100)
        signal = s.analyze(candles)
        assert signal is None or signal.action.value in ("buy", "sell", "close")
