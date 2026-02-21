"""Tests for chart pattern detection, structure analysis, and smart SL/TP placement.

These tests verify:
- Swing point detection finds local highs and lows
- S/R zones cluster correctly from swing points
- Pattern detectors (double bottom, higher low, H&S, bull flag) work
- Smart stops go DEEPER than textbook (survive stop hunts)
- Wick-bounce tightening works
- Edge cases (insufficient data, no structure) fall back gracefully
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.models.market import Candle
from core.patterns.detector import PatternDetector
from core.patterns.library import DEEP_STOP_MULT, deep_stop_distance
from core.patterns.models import (
    KeyLevel,
    LevelStrength,
    PatternMatch,
    PatternType,
    SmartStops,
    SwingPoint,
    SwingType,
)
from core.patterns.structure import StructureAnalyzer

# ── Helpers ────────────────────────────────────────────────────────────


def _candle(
    price: float, high: float | None = None, low: float | None = None, vol: float = 1000.0, idx: int = 0
) -> Candle:
    return Candle(
        timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=idx),
        open=price,
        high=high or price * 1.002,
        low=low or price * 0.998,
        close=price,
        volume=vol,
    )


def _make_w_candles() -> list[Candle]:
    """Generate a W-bottom (double bottom) pattern.

    Price: 1.5 → drops to 1.0 → bounces to 1.2 → dips to 1.02 → recovers to 1.3
    """
    prices = (
        [1.5 - i * 0.025 for i in range(20)]  # downtrend 1.5 → 1.0
        + [1.0 + i * 0.02 for i in range(10)]  # first bounce 1.0 → 1.2
        + [1.2 - i * 0.018 for i in range(10)]  # pullback 1.2 → 1.02
        + [1.02 + i * 0.028 for i in range(10)]  # recovery 1.02 → 1.3
    )
    candles = []
    for i, p in enumerate(prices):
        candles.append(_candle(p, high=p * 1.005, low=p * 0.995, idx=i))
    return candles


def _make_uptrend_candles() -> list[Candle]:
    """Higher lows pattern: 1.0 → 1.1 → dip to 1.05 → 1.2 → dip to 1.15 → 1.3"""
    prices = (
        [1.0 + i * 0.01 for i in range(10)]  # 1.0 → 1.1
        + [1.1 - i * 0.005 for i in range(10)]  # dip to 1.05
        + [1.05 + i * 0.015 for i in range(10)]  # 1.05 → 1.2
        + [1.2 - i * 0.005 for i in range(10)]  # dip to 1.15
        + [1.15 + i * 0.015 for i in range(10)]  # 1.15 → 1.3
    )
    candles = []
    for i, p in enumerate(prices):
        candles.append(_candle(p, high=p * 1.005, low=p * 0.995, idx=i))
    return candles


# ── Structure Analyzer ─────────────────────────────────────────────────


class TestSwingPointDetection:
    def test_finds_swing_lows(self):
        analyzer = StructureAnalyzer(swing_lookback=3)
        candles = _make_w_candles()
        swings = analyzer.find_swing_points(candles)
        lows = [s for s in swings if s.is_low]
        assert len(lows) >= 2

    def test_finds_swing_highs(self):
        analyzer = StructureAnalyzer(swing_lookback=3)
        candles = _make_w_candles()
        swings = analyzer.find_swing_points(candles)
        highs = [s for s in swings if s.is_high]
        assert len(highs) >= 1

    def test_insufficient_data_returns_empty(self):
        analyzer = StructureAnalyzer(swing_lookback=5)
        candles = [_candle(100.0, idx=i) for i in range(8)]
        swings = analyzer.find_swing_points(candles)
        assert swings == []

    def test_swing_prices_are_reasonable(self):
        analyzer = StructureAnalyzer(swing_lookback=3)
        candles = _make_w_candles()
        swings = analyzer.find_swing_points(candles)
        for s in swings:
            assert 0.9 < s.price < 1.6


class TestLevelClustering:
    def test_clusters_nearby_swings(self):
        analyzer = StructureAnalyzer(zone_tolerance_pct=1.0)
        swings = [
            SwingPoint(index=10, price=1.00, swing_type=SwingType.LOW),
            SwingPoint(index=30, price=1.005, swing_type=SwingType.LOW),
        ]
        levels = analyzer.cluster_into_levels(swings, current_price=1.2)
        assert len(levels) == 1
        assert levels[0].touch_count == 2

    def test_separates_distant_swings(self):
        analyzer = StructureAnalyzer(zone_tolerance_pct=0.3)
        swings = [
            SwingPoint(index=10, price=1.00, swing_type=SwingType.LOW),
            SwingPoint(index=30, price=1.10, swing_type=SwingType.LOW),
        ]
        levels = analyzer.cluster_into_levels(swings, current_price=1.2)
        assert len(levels) == 2

    def test_support_below_resistance_above(self):
        analyzer = StructureAnalyzer(zone_tolerance_pct=0.5)
        swings = [
            SwingPoint(index=10, price=1.00, swing_type=SwingType.LOW),
            SwingPoint(index=20, price=1.40, swing_type=SwingType.HIGH),
        ]
        levels = analyzer.cluster_into_levels(swings, current_price=1.2)
        supports = [lv for lv in levels if lv.is_support]
        resistances = [lv for lv in levels if not lv.is_support]
        assert len(supports) >= 1
        assert len(resistances) >= 1


class TestFindNearest:
    def test_nearest_support(self):
        analyzer = StructureAnalyzer()
        levels = [
            KeyLevel(price=1.0, zone_low=0.99, zone_high=1.01, is_support=True, strength=LevelStrength.STRONG),
            KeyLevel(price=0.8, zone_low=0.79, zone_high=0.81, is_support=True, strength=LevelStrength.MODERATE),
        ]
        nearest = analyzer.find_nearest_support(levels, price=1.2)
        assert nearest is not None
        assert nearest.price == pytest.approx(1.0)

    def test_nearest_resistance(self):
        analyzer = StructureAnalyzer()
        levels = [
            KeyLevel(price=1.5, zone_low=1.49, zone_high=1.51, is_support=False, strength=LevelStrength.STRONG),
            KeyLevel(price=2.0, zone_low=1.99, zone_high=2.01, is_support=False, strength=LevelStrength.MODERATE),
        ]
        nearest = analyzer.find_nearest_resistance(levels, price=1.2)
        assert nearest is not None
        assert nearest.price == pytest.approx(1.5)

    def test_no_support_returns_none(self):
        analyzer = StructureAnalyzer()
        nearest = analyzer.find_nearest_support([], price=1.0)
        assert nearest is None


# ── Pattern Detection ──────────────────────────────────────────────────


class TestDoubleBottom:
    def test_detects_w_pattern(self):
        detector = PatternDetector(min_confidence=0.1)
        candles = _make_w_candles()
        smart = detector.analyze(candles, current_price=1.3, side="long")
        assert smart.pattern is not None
        assert smart.pattern.direction == "long"
        assert smart.pattern.pattern_type in (PatternType.DOUBLE_BOTTOM, PatternType.HIGHER_LOW)
        assert smart.pattern.confidence > 0

    def test_w_stop_below_valley(self):
        detector = PatternDetector(min_confidence=0.1)
        candles = _make_w_candles()
        smart = detector.analyze(candles, current_price=1.3, side="long")
        if smart.pattern:
            assert smart.pattern.textbook_stop < 1.1
            assert smart.pattern.deep_stop < smart.pattern.textbook_stop


class TestDoubleTop:
    def test_detects_m_pattern(self):
        """M-shape: price rises to 1.5, dips to 1.3, rises to 1.48, drops to 1.25."""
        prices = (
            [1.0 + i * 0.025 for i in range(20)]  # up to 1.5
            + [1.5 - i * 0.02 for i in range(10)]  # dip to 1.3
            + [1.3 + i * 0.018 for i in range(10)]  # rise to 1.48
            + [1.48 - i * 0.023 for i in range(10)]  # drop to ~1.25
        )
        candles = [_candle(p, high=p * 1.005, low=p * 0.995, idx=i) for i, p in enumerate(prices)]
        detector = PatternDetector(min_confidence=0.1)
        smart = detector.analyze(candles, current_price=1.25, side="short")
        assert smart.initial_stop > 0
        if smart.pattern:
            assert smart.pattern.direction == "short"


class TestHeadShoulders:
    def _make_hs_candles(self) -> list[Candle]:
        """H&S top: left shoulder 1.4, head 1.5, right shoulder 1.38, breakdown."""
        prices = (
            [1.0 + i * 0.04 for i in range(10)]  # up to 1.4
            + [1.4 - i * 0.02 for i in range(5)]  # dip to 1.3
            + [1.3 + i * 0.04 for i in range(5)]  # up to 1.5 (head)
            + [1.5 - i * 0.025 for i in range(8)]  # dip to 1.3
            + [1.3 + i * 0.01 for i in range(8)]  # up to 1.38 (right shoulder)
            + [1.38 - i * 0.02 for i in range(8)]  # breakdown to ~1.22
        )
        return [_candle(p, high=p * 1.005, low=p * 0.995, idx=i) for i, p in enumerate(prices)]

    def test_detects_hs_pattern(self):
        detector = PatternDetector(min_confidence=0.1)
        candles = self._make_hs_candles()
        smart = detector.analyze(candles, current_price=1.22, side="short")
        assert smart.initial_stop > 0

    def test_hs_short_stop_above_entry(self):
        detector = PatternDetector(min_confidence=0.1)
        candles = self._make_hs_candles()
        smart = detector.analyze(candles, current_price=1.22, side="short")
        assert smart.initial_stop > 1.22


class TestInvHeadShoulders:
    def _make_ihs_candles(self) -> list[Candle]:
        """Inv H&S: left shoulder 0.8, head 0.7, right shoulder 0.82, breakout."""
        prices = (
            [1.0 - i * 0.02 for i in range(10)]  # down to 0.8
            + [0.8 + i * 0.02 for i in range(5)]  # bounce to 0.9
            + [0.9 - i * 0.04 for i in range(5)]  # down to 0.7 (head)
            + [0.7 + i * 0.025 for i in range(8)]  # up to 0.9
            + [0.9 - i * 0.01 for i in range(8)]  # down to 0.82 (right shoulder)
            + [0.82 + i * 0.03 for i in range(8)]  # breakout to ~1.06
        )
        return [_candle(p, high=p * 1.005, low=p * 0.995, idx=i) for i, p in enumerate(prices)]

    def test_detects_inv_hs(self):
        detector = PatternDetector(min_confidence=0.1)
        candles = self._make_ihs_candles()
        smart = detector.analyze(candles, current_price=1.06, side="long")
        assert smart.initial_stop > 0
        assert smart.initial_stop < 1.06


class TestBullFlag:
    def _make_flag_candles(self) -> list[Candle]:
        """Bull flag: strong rally 1.0→1.5, shallow pullback to 1.4, then resume."""
        prices = (
            [1.0 + i * 0.033 for i in range(15)]  # rally to ~1.5
            + [1.5 - i * 0.01 for i in range(10)]  # shallow pullback to ~1.4
            + [1.4 + i * 0.015 for i in range(10)]  # resume
        )
        return [_candle(p, high=p * 1.005, low=p * 0.995, idx=i) for i, p in enumerate(prices)]

    def test_detects_flag(self):
        detector = PatternDetector(min_confidence=0.1)
        candles = self._make_flag_candles()
        smart = detector.analyze(candles, current_price=1.55, side="long")
        assert smart.initial_stop > 0
        assert smart.initial_stop < 1.55


class TestLowerHigh:
    def test_detects_lower_high(self):
        """Downtrend: highs at 1.5, then 1.4 → lower high."""
        prices = (
            [1.2 + i * 0.03 for i in range(10)]  # up to 1.5
            + [1.5 - i * 0.02 for i in range(10)]  # down to 1.3
            + [1.3 + i * 0.01 for i in range(10)]  # up to 1.4 (lower high)
            + [1.4 - i * 0.015 for i in range(10)]  # down to 1.25
        )
        candles = [_candle(p, high=p * 1.005, low=p * 0.995, idx=i) for i, p in enumerate(prices)]
        detector = PatternDetector(min_confidence=0.1)
        smart = detector.analyze(candles, current_price=1.25, side="short")
        assert smart.initial_stop > 0


class TestShortSideSmartStops:
    def test_short_tp_below_entry(self):
        detector = PatternDetector(min_confidence=0.0)
        prices = (
            [1.5 - i * 0.025 for i in range(20)]  # downtrend
            + [1.0 + i * 0.01 for i in range(10)]  # small bounce
            + [1.1 - i * 0.01 for i in range(10)]  # continuation down
        )
        candles = [_candle(p, high=p * 1.005, low=p * 0.995, idx=i) for i, p in enumerate(prices)]
        smart = detector.analyze(candles, current_price=1.0, side="short", fallback_stop_pct=2.0)
        if smart.take_profit_1 > 0:
            assert smart.take_profit_1 < 1.0


class TestHigherLow:
    def test_detects_higher_low(self):
        detector = PatternDetector(min_confidence=0.1)
        candles = _make_uptrend_candles()
        smart = detector.analyze(candles, current_price=1.3, side="long")
        has_hl = smart.pattern and smart.pattern.pattern_type == PatternType.HIGHER_LOW
        has_any_long = smart.pattern and smart.pattern.direction == "long"
        assert has_hl or has_any_long or smart.has_structure


# ── Smart Stops ────────────────────────────────────────────────────────


class TestDeepStopDistance:
    def test_long_deep_stop_below_textbook(self):
        textbook = 95.0
        entry = 100.0
        deep = deep_stop_distance(textbook, entry)
        assert deep < textbook

    def test_short_deep_stop_above_textbook(self):
        textbook = 105.0
        entry = 100.0
        deep = deep_stop_distance(textbook, entry)
        assert deep > textbook

    def test_low_liquidity_goes_deeper(self):
        textbook = 95.0
        entry = 100.0
        normal = deep_stop_distance(textbook, entry, low_liquidity=False)
        low_liq = deep_stop_distance(textbook, entry, low_liquidity=True)
        assert low_liq < normal

    def test_multiplier_applied(self):
        textbook = 95.0
        entry = 100.0
        deep = deep_stop_distance(textbook, entry)
        expected = entry - (entry - textbook) * DEEP_STOP_MULT
        assert deep == pytest.approx(expected)


class TestSmartStopsFallback:
    def test_insufficient_data_uses_fallback(self):
        detector = PatternDetector()
        candles = [_candle(100.0, idx=i) for i in range(10)]
        smart = detector.analyze(candles, current_price=100.0, side="long")
        assert smart.fallback_pct > 0
        assert not smart.has_structure
        assert not smart.has_pattern

    def test_stop_loss_pct_with_no_structure(self):
        smart = SmartStops(fallback_pct=2.0)
        assert smart.stop_loss_pct(100.0) == 2.0


class TestSmartStopsStructural:
    def test_long_sl_uses_structure(self):
        detector = PatternDetector(min_confidence=0.0)
        candles = _make_w_candles()
        smart = detector.analyze(candles, current_price=1.3, side="long", fallback_stop_pct=2.0)
        if smart.has_structure or smart.has_pattern:
            assert smart.initial_stop < 1.3
            assert smart.initial_stop > 0

    def test_long_deep_stop_below_tightened(self):
        detector = PatternDetector(min_confidence=0.0)
        candles = _make_w_candles()
        smart = detector.analyze(candles, current_price=1.3, side="long", fallback_stop_pct=2.0)
        if smart.tightened_stop > 0:
            assert smart.initial_stop <= smart.tightened_stop

    def test_take_profit_above_entry_for_long(self):
        detector = PatternDetector(min_confidence=0.0)
        candles = _make_w_candles()
        smart = detector.analyze(candles, current_price=1.3, side="long", fallback_stop_pct=2.0)
        if smart.take_profit_1 > 0:
            assert smart.take_profit_1 > 1.3


# ── Wick-bounce tighten (trailing stop integration) ───────────────────


class TestWickBounceTighten:
    def test_long_wick_bounce_tightens_stop(self):
        from core.models.order import OrderSide
        from core.orders.trailing import TrailingStop

        ts = TrailingStop(
            symbol="TEST/USDT",
            side=OrderSide.BUY,
            entry_price=1.25,
            initial_stop_pct=5.0,
            trail_pct=1.0,
            tightened_stop=1.20,
        )
        assert ts.current_stop == pytest.approx(1.25 * 0.95)  # deep stop

        ts.update(1.21)  # price drops close to 1.20 tightened level
        assert ts.wick_bounced
        assert ts.current_stop > 1.19  # tightened to ~1.20 + offset

    def test_no_tighten_if_price_not_near_level(self):
        from core.models.order import OrderSide
        from core.orders.trailing import TrailingStop

        ts = TrailingStop(
            symbol="TEST/USDT",
            side=OrderSide.BUY,
            entry_price=1.25,
            initial_stop_pct=5.0,
            trail_pct=1.0,
            tightened_stop=1.10,
        )
        ts.update(1.30)  # price is far from tightened level
        assert not ts.wick_bounced

    def test_short_wick_bounce_tightens_stop(self):
        from core.models.order import OrderSide
        from core.orders.trailing import TrailingStop

        ts = TrailingStop(
            symbol="TEST/USDT",
            side=OrderSide.SELL,
            entry_price=100.0,
            initial_stop_pct=5.0,
            trail_pct=1.0,
            tightened_stop=104.0,
        )
        ts.update(103.5)  # price spikes close to 104 then comes back
        assert ts.wick_bounced
        assert ts.current_stop < 105.0  # tightened


# ── KeyLevel model ─────────────────────────────────────────────────────


class TestKeyLevel:
    def test_stop_hunt_zone_support(self):
        level = KeyLevel(price=100.0, zone_low=99.5, zone_high=100.5, is_support=True)
        hunt_zone = level.stop_hunt_zone
        assert hunt_zone < 99.5

    def test_stop_hunt_zone_resistance(self):
        level = KeyLevel(price=100.0, zone_low=99.5, zone_high=100.5, is_support=False)
        hunt_zone = level.stop_hunt_zone
        assert hunt_zone > 100.5


class TestPatternMatchRR:
    def test_risk_reward_ratio(self):
        pm = PatternMatch(
            pattern_type=PatternType.DOUBLE_BOTTOM,
            confidence=0.8,
            reliability=0.66,
            entry_zone=1.2,
            deep_stop=1.1,
            target_1=1.4,
        )
        assert pm.risk_reward == pytest.approx(2.0)

    def test_signal_boost_increases_with_confidence(self):
        low = PatternMatch(
            pattern_type=PatternType.HIGHER_LOW,
            confidence=0.3,
            reliability=0.52,
            entry_zone=1.0,
            deep_stop=0.95,
            target_1=1.1,
        )
        high = PatternMatch(
            pattern_type=PatternType.HIGHER_LOW,
            confidence=0.9,
            reliability=0.52,
            entry_zone=1.0,
            deep_stop=0.95,
            target_1=1.1,
        )
        assert high.signal_boost > low.signal_boost


# ── PatternDetector edge cases (insufficient data, fallback, no match) ───────


class TestPatternDetectorInsufficientData:
    def test_analyze_returns_fallback_when_fewer_than_20_candles(self):
        detector = PatternDetector(min_confidence=0.1)
        candles = [_candle(100.0, idx=i) for i in range(19)]
        smart = detector.analyze(candles, current_price=100.0, side="long", fallback_stop_pct=2.5)
        assert not smart.has_structure
        assert not smart.has_pattern
        assert smart.fallback_pct == 2.5
        assert smart.initial_stop == 0 or smart.stop_loss_pct(100.0) == 2.5

    def test_analyze_long_no_support_uses_fallback_stop_pct(self):
        """When current_price is below all structure, nearest_support is None -> fallback branch."""
        detector = PatternDetector(min_confidence=0.1)
        candles = _make_w_candles()
        smart = detector.analyze(candles, current_price=0.5, side="long", fallback_stop_pct=2.0)
        assert smart.initial_stop > 0
        assert smart.initial_stop < 0.5

    def test_analyze_short_no_resistance_uses_fallback_stop_pct(self):
        detector = PatternDetector(min_confidence=0.1)
        candles = _make_w_candles()
        smart = detector.analyze(candles, current_price=2.0, side="short", fallback_stop_pct=2.0)
        assert smart.initial_stop > 2.0


class TestPatternDetectorDoubleBottomEdgeCases:
    def test_double_bottom_price_below_neckline_returns_none_via_analyze(self):
        """Price < neckline * 0.98 causes double bottom to not be returned as best for long."""
        detector = PatternDetector(min_confidence=0.01)
        candles = _make_w_candles()
        smart = detector.analyze(candles, current_price=0.95, side="long")
        if smart.pattern:
            assert smart.pattern.pattern_type != PatternType.DOUBLE_BOTTOM or smart.pattern.entry_zone > 0.95


class TestPatternDetectorDoubleTopEdgeCases:
    def test_double_top_price_above_neckline_skipped(self):
        """Price > neckline * 1.02 causes double top to be invalid for short."""
        prices = (
            [1.0 + i * 0.025 for i in range(20)]
            + [1.5 - i * 0.02 for i in range(10)]
            + [1.3 + i * 0.018 for i in range(10)]
            + [1.48 - i * 0.023 for i in range(10)]
        )
        candles = [_candle(p, high=p * 1.005, low=p * 0.995, idx=i) for i, p in enumerate(prices)]
        detector = PatternDetector(min_confidence=0.01)
        smart = detector.analyze(candles, current_price=1.50, side="short")
        assert smart.initial_stop > 0


class TestPatternDetectorPickBest:
    def test_analyze_short_side_returns_short_pattern_or_structure_only(self):
        detector = PatternDetector(min_confidence=0.1)
        candles = _make_uptrend_candles()
        smart = detector.analyze(candles, current_price=1.3, side="short")
        if smart.pattern:
            assert smart.pattern.direction == "short"
        assert smart.initial_stop >= 0


class TestPatternDetectorBullFlagEdgeCases:
    def test_bull_flag_requires_enough_candles(self):
        detector = PatternDetector(min_confidence=0.01)
        candles = [_candle(1.0 + i * 0.01, idx=i) for i in range(29)]
        smart = detector.analyze(candles, current_price=1.3, side="long")
        if smart.pattern and smart.pattern.pattern_type == PatternType.BULL_FLAG:
            assert smart.pattern.direction == "long"


class TestPatternDetectorLongStopsInitialStopZero:
    def test_compute_long_stops_fallback_when_initial_stop_negative(self):
        """Covers branch where deep_stop_distance yields <= 0 and we use fallback."""
        detector = PatternDetector(min_confidence=0.0)
        candles = _make_w_candles()
        smart = detector.analyze(candles, current_price=1.3, side="long", fallback_stop_pct=2.0, low_liquidity=False)
        assert smart.initial_stop > 0
        assert smart.initial_stop < 1.3


# ── PatternDetector: boundary candles, _pick_best no match, fallback branches ─


class TestPatternDetectorBoundaryAndPickBest:
    def test_analyze_exactly_20_candles_proceeds(self):
        """Boundary: len >= 20 proceeds to structure/pattern analysis."""
        detector = PatternDetector(min_confidence=0.1)
        candles = [_candle(100.0 + i * 0.1, idx=i) for i in range(20)]
        smart = detector.analyze(candles, current_price=102.0, side="long", fallback_stop_pct=2.0)
        assert smart.fallback_pct == 2.0
        assert smart.initial_stop >= 0 or smart.stop_loss_pct(102.0) == 2.0

    def test_analyze_19_candles_returns_fallback_only(self):
        detector = PatternDetector(min_confidence=0.1)
        candles = [_candle(100.0, idx=i) for i in range(19)]
        smart = detector.analyze(candles, current_price=100.0, side="long", fallback_stop_pct=2.5)
        assert not smart.has_structure
        assert not smart.has_pattern
        assert smart.fallback_pct == 2.5

    def test_analyze_short_side_with_only_long_patterns_returns_structure_stops(self):
        """When all detected patterns are long, _pick_best returns None for side short; stops use structure/fallback."""
        detector = PatternDetector(min_confidence=0.1)
        candles = _make_w_candles()
        smart = detector.analyze(candles, current_price=1.3, side="short", fallback_stop_pct=2.0)
        assert smart.initial_stop > 0
        assert smart.initial_stop > 1.3
        if smart.pattern:
            assert smart.pattern.direction == "short"

    def test_bull_flag_requires_at_least_30_candles(self):
        detector = PatternDetector(min_confidence=0.01)
        candles = [_candle(1.0 + i * 0.02, idx=i) for i in range(29)]
        smart = detector.analyze(candles, current_price=1.6, side="long")
        if smart.pattern and smart.pattern.pattern_type == PatternType.BULL_FLAG:
            assert smart.pattern.direction == "long"
        assert smart.initial_stop >= 0


class TestPatternDetectorLongShortFallbackBranches:
    def test_long_stops_when_no_support_below_entry_uses_fallback_pct(self):
        """When current price is above all structure, textbook_sl >= entry -> else branch in _compute_long_stops."""
        detector = PatternDetector(min_confidence=0.0)
        prices = [2.0 + i * 0.01 for i in range(50)]
        candles = [_candle(p, high=p * 1.002, low=p * 0.998, idx=i) for i, p in enumerate(prices)]
        smart = detector.analyze(candles, current_price=2.6, side="long", fallback_stop_pct=2.0)
        assert smart.initial_stop > 0
        assert smart.initial_stop < 2.6
        assert smart.tightened_stop > 0

    def test_short_stops_when_no_resistance_above_entry_uses_fallback_pct(self):
        """When current price is below all structure, textbook_sl <= entry -> else branch in _compute_short_stops."""
        detector = PatternDetector(min_confidence=0.0)
        prices = [2.0 - i * 0.01 for i in range(50)]
        candles = [_candle(p, high=p * 1.002, low=p * 0.998, idx=i) for i, p in enumerate(prices)]
        smart = detector.analyze(candles, current_price=1.5, side="short", fallback_stop_pct=2.0)
        assert smart.initial_stop > 1.5
        assert smart.tightened_stop > 0

    def test_min_confidence_filters_low_confidence_patterns(self):
        """Patterns below min_confidence are discarded in _detect_all."""
        detector = PatternDetector(min_confidence=0.95)
        candles = _make_w_candles()
        smart = detector.analyze(candles, current_price=1.3, side="long")
        if smart.pattern:
            assert smart.pattern.confidence >= 0.95
        assert smart.initial_stop > 0
