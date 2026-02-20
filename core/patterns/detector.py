"""Chart pattern detector and smart SL/TP engine.

Scans candle data for recognizable chart patterns, then combines
structural analysis with pattern rules to produce SL/TP targets.

Stop-loss philosophy (from the user):
  "Most traders and bots use the same rules. Market makers know this
   and wick through the expected SL.  Go deeper, and only if a wick
   happened, raise to textbook + offset."

Two-tier stops:
  initial_stop  → deep, past the stop-hunt zone (survive the wick)
  tightened_stop → textbook level + offset (activate after wick bounce)
"""

from __future__ import annotations

from loguru import logger

from core.models import Candle
from core.patterns.library import deep_stop_distance, get_spec
from core.patterns.models import (
    KeyLevel,
    PatternMatch,
    PatternType,
    SmartStops,
    SwingPoint,
)
from core.patterns.structure import StructureAnalyzer


class PatternDetector:
    """Detects chart patterns and produces smart SL/TP recommendations.

    Parameters
    ----------
    structure : StructureAnalyzer
        Shared structure analyzer (swing points, S/R zones).
    min_confidence : float
        Patterns below this confidence are discarded.
    """

    def __init__(
        self,
        structure: StructureAnalyzer | None = None,
        min_confidence: float = 0.3,
    ):
        self.structure = structure or StructureAnalyzer()
        self.min_confidence = min_confidence

    def analyze(
        self,
        candles: list[Candle],
        current_price: float,
        side: str = "long",
        low_liquidity: bool = False,
        fallback_stop_pct: float = 2.0,
    ) -> SmartStops:
        """Full analysis: structure → patterns → smart stops.

        Returns SmartStops with initial (deep) and tightened (textbook)
        stop-loss levels, plus TP targets.
        """
        if len(candles) < 20:
            return SmartStops(fallback_pct=fallback_stop_pct)

        swings, levels = self.structure.analyze(candles, current_price)

        patterns = self._detect_all(swings, levels, candles, current_price)
        best = self._pick_best(patterns, side)

        support = self.structure.find_nearest_support(levels, current_price)
        resistance = self.structure.find_nearest_resistance(levels, current_price)

        return self._build_smart_stops(
            side=side,
            entry=current_price,
            support=support,
            resistance=resistance,
            pattern=best,
            low_liquidity=low_liquidity,
            fallback_pct=fallback_stop_pct,
        )

    def _detect_all(
        self,
        swings: list[SwingPoint],
        levels: list[KeyLevel],
        candles: list[Candle],
        current_price: float,
    ) -> list[PatternMatch]:
        """Run all pattern detectors and collect matches."""
        patterns: list[PatternMatch] = []

        lows = [s for s in swings if s.is_low]
        highs = [s for s in swings if s.is_high]

        if db := self._detect_double_bottom(lows, highs, current_price):
            patterns.append(db)
        if dt := self._detect_double_top(highs, lows, current_price):
            patterns.append(dt)
        if hl := self._detect_higher_low(lows, current_price):
            patterns.append(hl)
        if lh := self._detect_lower_high(highs, current_price):
            patterns.append(lh)
        if ihs := self._detect_inv_head_shoulders(lows, highs, current_price):
            patterns.append(ihs)
        if hs := self._detect_head_shoulders(highs, lows, current_price):
            patterns.append(hs)
        if bf := self._detect_bull_flag(candles, swings, current_price):
            patterns.append(bf)

        patterns = [p for p in patterns if p.confidence >= self.min_confidence]
        return patterns

    def _pick_best(
        self,
        patterns: list[PatternMatch],
        side: str,
    ) -> PatternMatch | None:
        """Pick the highest-quality pattern matching the trade direction."""
        matching = [p for p in patterns if p.direction == side]
        if not matching:
            return None
        matching.sort(key=lambda p: p.confidence * p.reliability, reverse=True)
        best = matching[0]
        logger.info(
            "Pattern detected: {} (conf={:.0%}, rel={:.0%}, R:R={:.1f})",
            best.pattern_type.value,
            best.confidence,
            best.reliability,
            best.risk_reward,
        )
        return best

    def _build_smart_stops(
        self,
        side: str,
        entry: float,
        support: KeyLevel | None,
        resistance: KeyLevel | None,
        pattern: PatternMatch | None,
        low_liquidity: bool,
        fallback_pct: float,
    ) -> SmartStops:
        """Combine structure + pattern into final SL/TP recommendation."""
        stops = SmartStops(
            nearest_support=support,
            nearest_resistance=resistance,
            pattern=pattern,
            fallback_pct=fallback_pct,
        )

        if side == "long":
            stops = self._compute_long_stops(stops, entry, low_liquidity)
        else:
            stops = self._compute_short_stops(stops, entry, low_liquidity)

        return stops

    def _compute_long_stops(
        self,
        stops: SmartStops,
        entry: float,
        low_liquidity: bool,
    ) -> SmartStops:
        textbook_sl = 0.0

        if stops.pattern and stops.pattern.textbook_stop > 0:
            textbook_sl = stops.pattern.textbook_stop
        elif stops.nearest_support:
            textbook_sl = stops.nearest_support.zone_low

        if textbook_sl > 0 and textbook_sl < entry:
            stops.tightened_stop = textbook_sl
            stops.initial_stop = deep_stop_distance(textbook_sl, entry, low_liquidity)
            if stops.initial_stop <= 0:
                stops.initial_stop = entry * (1 - stops.fallback_pct * 1.5 / 100)
        else:
            stops.initial_stop = entry * (1 - stops.fallback_pct / 100)
            stops.tightened_stop = entry * (1 - stops.fallback_pct * 0.6 / 100)

        if stops.pattern and stops.pattern.target_1 > 0:
            stops.take_profit_1 = stops.pattern.target_1
            stops.take_profit_2 = stops.pattern.target_2
        elif stops.nearest_resistance:
            stops.take_profit_1 = stops.nearest_resistance.zone_low
            stops.take_profit_2 = stops.nearest_resistance.zone_high * 1.02
        else:
            risk = abs(entry - stops.initial_stop)
            stops.take_profit_1 = entry + risk * 2.0
            stops.take_profit_2 = entry + risk * 3.0

        if stops.pattern:
            stops.invalidation = stops.pattern.invalidation
        elif stops.nearest_support:
            stops.invalidation = stops.nearest_support.stop_hunt_zone

        return stops

    def _compute_short_stops(
        self,
        stops: SmartStops,
        entry: float,
        low_liquidity: bool,
    ) -> SmartStops:
        textbook_sl = 0.0

        if stops.pattern and stops.pattern.textbook_stop > 0:
            textbook_sl = stops.pattern.textbook_stop
        elif stops.nearest_resistance:
            textbook_sl = stops.nearest_resistance.zone_high

        if textbook_sl > 0 and textbook_sl > entry:
            stops.tightened_stop = textbook_sl
            stops.initial_stop = deep_stop_distance(textbook_sl, entry, low_liquidity)
        else:
            stops.initial_stop = entry * (1 + stops.fallback_pct / 100)
            stops.tightened_stop = entry * (1 + stops.fallback_pct * 0.6 / 100)

        if stops.pattern and stops.pattern.target_1 > 0:
            stops.take_profit_1 = stops.pattern.target_1
            stops.take_profit_2 = stops.pattern.target_2
        elif stops.nearest_support:
            stops.take_profit_1 = stops.nearest_support.zone_high
            stops.take_profit_2 = stops.nearest_support.zone_low * 0.98
        else:
            risk = abs(stops.initial_stop - entry)
            stops.take_profit_1 = entry - risk * 2.0
            stops.take_profit_2 = entry - risk * 3.0

        if stops.pattern:
            stops.invalidation = stops.pattern.invalidation
        elif stops.nearest_resistance:
            stops.invalidation = stops.nearest_resistance.stop_hunt_zone

        return stops

    # ── Pattern detectors ──────────────────────────────────────────────

    def _detect_double_bottom(
        self,
        lows: list[SwingPoint],
        highs: list[SwingPoint],
        price: float,
    ) -> PatternMatch | None:
        """W-shape: two swing lows near the same price with a peak between."""
        if len(lows) < 2:
            return None

        l1, l2 = lows[-2], lows[-1]
        diff_pct = abs(l1.price - l2.price) / l1.price * 100

        if diff_pct > 2.0:
            return None
        if l2.index <= l1.index:
            return None

        mid_highs = [h for h in highs if l1.index < h.index < l2.index]
        if not mid_highs:
            return None
        neckline = max(h.price for h in mid_highs)

        if price < neckline * 0.98:
            return None

        valley = min(l1.price, l2.price)
        height = neckline - valley
        spec = get_spec(PatternType.DOUBLE_BOTTOM)

        conf = 1.0 - diff_pct / 2.0
        if l2.price > l1.price:
            conf += 0.1

        return PatternMatch(
            pattern_type=PatternType.DOUBLE_BOTTOM,
            confidence=min(max(conf, 0.0), 1.0),
            reliability=spec.reliability,
            direction="long",
            entry_zone=neckline,
            textbook_stop=valley,
            deep_stop=deep_stop_distance(valley, neckline),
            target_1=neckline + height * 0.8,
            target_2=neckline + height,
            invalidation=valley * 0.97,
            swing_points=[l1, l2],
            detected_at_index=l2.index,
        )

    def _detect_double_top(
        self,
        highs: list[SwingPoint],
        lows: list[SwingPoint],
        price: float,
    ) -> PatternMatch | None:
        """M-shape: two swing highs near the same price with a trough between."""
        if len(highs) < 2:
            return None

        h1, h2 = highs[-2], highs[-1]
        diff_pct = abs(h1.price - h2.price) / h1.price * 100

        if diff_pct > 2.0:
            return None
        if h2.index <= h1.index:
            return None

        mid_lows = [lo for lo in lows if h1.index < lo.index < h2.index]
        if not mid_lows:
            return None
        neckline = min(lo.price for lo in mid_lows)

        if price > neckline * 1.02:
            return None

        peak = max(h1.price, h2.price)
        height = peak - neckline
        spec = get_spec(PatternType.DOUBLE_TOP)

        conf = 1.0 - diff_pct / 2.0
        if h2.price < h1.price:
            conf += 0.1

        return PatternMatch(
            pattern_type=PatternType.DOUBLE_TOP,
            confidence=min(max(conf, 0.0), 1.0),
            reliability=spec.reliability,
            direction="short",
            entry_zone=neckline,
            textbook_stop=peak,
            deep_stop=deep_stop_distance(peak, neckline),
            target_1=neckline - height * 0.8,
            target_2=neckline - height,
            invalidation=peak * 1.03,
            swing_points=[h1, h2],
            detected_at_index=h2.index,
        )

    def _detect_higher_low(
        self,
        lows: list[SwingPoint],
        price: float,
    ) -> PatternMatch | None:
        """Latest swing low higher than the previous → uptrend structure."""
        if len(lows) < 2:
            return None

        prev, last = lows[-2], lows[-1]
        if last.price <= prev.price:
            return None
        if price < last.price:
            return None

        improvement_pct = (last.price - prev.price) / prev.price * 100
        conf = min(improvement_pct / 5.0, 1.0)
        spec = get_spec(PatternType.HIGHER_LOW)

        swing_range = price - last.price
        textbook_sl = last.price

        return PatternMatch(
            pattern_type=PatternType.HIGHER_LOW,
            confidence=max(conf, 0.3),
            reliability=spec.reliability,
            direction="long",
            entry_zone=price,
            textbook_stop=textbook_sl,
            deep_stop=deep_stop_distance(textbook_sl, price),
            target_1=price + swing_range * 1.5,
            target_2=price + swing_range * 2.5,
            invalidation=prev.price * 0.98,
            swing_points=[prev, last],
            detected_at_index=last.index,
        )

    def _detect_lower_high(
        self,
        highs: list[SwingPoint],
        price: float,
    ) -> PatternMatch | None:
        """Latest swing high lower than previous → downtrend structure."""
        if len(highs) < 2:
            return None

        prev, last = highs[-2], highs[-1]
        if last.price >= prev.price:
            return None
        if price > last.price:
            return None

        drop_pct = (prev.price - last.price) / prev.price * 100
        conf = min(drop_pct / 5.0, 1.0)
        spec = get_spec(PatternType.LOWER_HIGH)

        swing_range = last.price - price
        textbook_sl = last.price

        return PatternMatch(
            pattern_type=PatternType.LOWER_HIGH,
            confidence=max(conf, 0.3),
            reliability=spec.reliability,
            direction="short",
            entry_zone=price,
            textbook_stop=textbook_sl,
            deep_stop=deep_stop_distance(textbook_sl, price),
            target_1=price - swing_range * 1.5,
            target_2=price - swing_range * 2.5,
            invalidation=prev.price * 1.02,
            swing_points=[prev, last],
            detected_at_index=last.index,
        )

    def _detect_inv_head_shoulders(
        self,
        lows: list[SwingPoint],
        highs: list[SwingPoint],
        price: float,
    ) -> PatternMatch | None:
        """Inverse H&S: three lows where the middle (head) is deepest."""
        if len(lows) < 3:
            return None

        ls, head, rs = lows[-3], lows[-2], lows[-1]
        if head.price >= ls.price or head.price >= rs.price:
            return None
        if not (ls.index < head.index < rs.index):
            return None

        shoulder_diff = abs(ls.price - rs.price) / ls.price * 100
        if shoulder_diff > 5.0:
            return None

        mid_highs_l = [h for h in highs if ls.index < h.index < head.index]
        mid_highs_r = [h for h in highs if head.index < h.index < rs.index]
        if not mid_highs_l or not mid_highs_r:
            return None

        neckline = (max(h.price for h in mid_highs_l) + max(h.price for h in mid_highs_r)) / 2
        height = neckline - head.price
        spec = get_spec(PatternType.INV_HEAD_SHOULDERS)

        conf = 1.0 - shoulder_diff / 5.0
        if price >= neckline * 0.98:
            conf += 0.15

        return PatternMatch(
            pattern_type=PatternType.INV_HEAD_SHOULDERS,
            confidence=min(max(conf, 0.0), 1.0),
            reliability=spec.reliability,
            direction="long",
            entry_zone=neckline,
            textbook_stop=rs.price,
            deep_stop=deep_stop_distance(rs.price, neckline),
            target_1=neckline + height * 0.8,
            target_2=neckline + height,
            invalidation=head.price * 0.97,
            swing_points=[ls, head, rs],
            detected_at_index=rs.index,
        )

    def _detect_head_shoulders(
        self,
        highs: list[SwingPoint],
        lows: list[SwingPoint],
        price: float,
    ) -> PatternMatch | None:
        """H&S top: three highs where the middle (head) is highest."""
        if len(highs) < 3:
            return None

        ls, head, rs = highs[-3], highs[-2], highs[-1]
        if head.price <= ls.price or head.price <= rs.price:
            return None
        if not (ls.index < head.index < rs.index):
            return None

        shoulder_diff = abs(ls.price - rs.price) / ls.price * 100
        if shoulder_diff > 5.0:
            return None

        mid_lows_l = [lo for lo in lows if ls.index < lo.index < head.index]
        mid_lows_r = [lo for lo in lows if head.index < lo.index < rs.index]
        if not mid_lows_l or not mid_lows_r:
            return None

        neckline = (min(lo.price for lo in mid_lows_l) + min(lo.price for lo in mid_lows_r)) / 2
        height = head.price - neckline
        spec = get_spec(PatternType.HEAD_SHOULDERS)

        conf = 1.0 - shoulder_diff / 5.0
        if price <= neckline * 1.02:
            conf += 0.15

        return PatternMatch(
            pattern_type=PatternType.HEAD_SHOULDERS,
            confidence=min(max(conf, 0.0), 1.0),
            reliability=spec.reliability,
            direction="short",
            entry_zone=neckline,
            textbook_stop=rs.price,
            deep_stop=deep_stop_distance(rs.price, neckline),
            target_1=neckline - height * 0.8,
            target_2=neckline - height,
            invalidation=head.price * 1.03,
            swing_points=[ls, head, rs],
            detected_at_index=rs.index,
        )

    def _detect_bull_flag(
        self,
        candles: list[Candle],
        swings: list[SwingPoint],
        price: float,
    ) -> PatternMatch | None:
        """Bull flag: strong rally followed by a shallow pullback channel."""
        if len(candles) < 30:
            return None

        recent_highs = [s for s in swings if s.is_high]
        recent_lows = [s for s in swings if s.is_low]
        if len(recent_highs) < 2 or len(recent_lows) < 1:
            return None

        pole_high = recent_highs[-2]
        flag_low = recent_lows[-1]
        if flag_low.index <= pole_high.index:
            return None

        pole_start_idx = max(0, pole_high.index - 15)
        pole_start_price = candles[pole_start_idx].low
        pole_height = pole_high.price - pole_start_price
        pullback = pole_high.price - flag_low.price

        if pole_height <= 0:
            return None
        pullback_ratio = pullback / pole_height
        if pullback_ratio > 0.5 or pullback_ratio < 0.1:
            return None

        if price < flag_low.price:
            return None

        conf = 1.0 - pullback_ratio
        spec = get_spec(PatternType.BULL_FLAG)

        return PatternMatch(
            pattern_type=PatternType.BULL_FLAG,
            confidence=min(max(conf, 0.0), 1.0),
            reliability=spec.reliability,
            direction="long",
            entry_zone=price,
            textbook_stop=flag_low.price,
            deep_stop=deep_stop_distance(flag_low.price, price),
            target_1=price + pole_height * 0.7,
            target_2=price + pole_height,
            invalidation=pole_start_price,
            swing_points=[pole_high, flag_low],
            detected_at_index=flag_low.index,
        )
