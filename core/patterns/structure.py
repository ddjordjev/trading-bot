"""Market structure analysis: swing points, support/resistance zones.

Finds the "bones" of the chart — the swing highs and lows that define
market structure.  These become the anchor points for pattern detection
and smart stop placement.
"""

from __future__ import annotations

from loguru import logger

from core.models import Candle
from core.patterns.models import KeyLevel, LevelStrength, SwingPoint, SwingType


class StructureAnalyzer:
    """Detects swing points and clusters them into S/R zones.

    Parameters
    ----------
    swing_lookback : int
        A point is a swing high/low if it's the highest/lowest
        within this many bars on each side.
    zone_tolerance_pct : float
        Two swing points within this % of each other merge into
        one zone.  Wider tolerance = fewer, broader zones.
    min_touches : int
        Minimum touches for a zone to be considered "real".
    """

    def __init__(
        self,
        swing_lookback: int = 5,
        zone_tolerance_pct: float = 0.3,
        min_touches: int = 1,
    ):
        self.swing_lookback = swing_lookback
        self.zone_tolerance_pct = zone_tolerance_pct
        self.min_touches = min_touches

    def find_swing_points(self, candles: list[Candle]) -> list[SwingPoint]:
        """Find local highs and lows in the candle series."""
        if len(candles) < self.swing_lookback * 2 + 1:
            return []

        swings: list[SwingPoint] = []
        n = self.swing_lookback

        for i in range(n, len(candles) - n):
            high_i = candles[i].high
            low_i = candles[i].low
            vol_i = candles[i].volume

            is_swing_high = all(high_i >= candles[j].high for j in range(i - n, i + n + 1) if j != i)
            is_swing_low = all(low_i <= candles[j].low for j in range(i - n, i + n + 1) if j != i)

            if is_swing_high:
                swings.append(
                    SwingPoint(
                        index=i,
                        price=high_i,
                        swing_type=SwingType.HIGH,
                        volume=vol_i,
                        timestamp=candles[i].timestamp,
                    )
                )
            if is_swing_low:
                swings.append(
                    SwingPoint(
                        index=i,
                        price=low_i,
                        swing_type=SwingType.LOW,
                        volume=vol_i,
                        timestamp=candles[i].timestamp,
                    )
                )

        return swings

    def cluster_into_levels(
        self,
        swings: list[SwingPoint],
        current_price: float,
    ) -> list[KeyLevel]:
        """Group nearby swing points into S/R zones.

        Points within `zone_tolerance_pct` of each other form a single
        zone.  More touches = stronger level.
        """
        if not swings:
            return []

        sorted_swings = sorted(swings, key=lambda s: s.price)
        clusters: list[list[SwingPoint]] = []
        current_cluster: list[SwingPoint] = [sorted_swings[0]]

        for sw in sorted_swings[1:]:
            cluster_avg = sum(s.price for s in current_cluster) / len(current_cluster)
            if cluster_avg <= 0 or abs(sw.price - cluster_avg) / cluster_avg * 100 <= self.zone_tolerance_pct:
                current_cluster.append(sw)
            else:
                clusters.append(current_cluster)
                current_cluster = [sw]
        clusters.append(current_cluster)

        levels: list[KeyLevel] = []
        for cluster in clusters:
            if len(cluster) < self.min_touches:
                continue
            prices = [s.price for s in cluster]
            avg_price = sum(prices) / len(prices)
            total_vol = sum(s.volume for s in cluster)
            last_idx = max(s.index for s in cluster)

            strength = self._classify_strength(len(cluster), total_vol, cluster)

            levels.append(
                KeyLevel(
                    price=avg_price,
                    zone_low=min(prices),
                    zone_high=max(prices),
                    strength=strength,
                    touch_count=len(cluster),
                    total_volume=total_vol,
                    is_support=avg_price < current_price,
                    last_touch_index=last_idx,
                )
            )

        return levels

    def find_nearest_support(
        self,
        levels: list[KeyLevel],
        price: float,
    ) -> KeyLevel | None:
        """Find the strongest support level below the given price."""
        supports = [lv for lv in levels if lv.is_support and lv.zone_high < price]
        if not supports:
            return None
        supports.sort(key=lambda lv: (-self._strength_rank(lv.strength), -lv.price))
        return supports[0]

    def find_nearest_resistance(
        self,
        levels: list[KeyLevel],
        price: float,
    ) -> KeyLevel | None:
        """Find the strongest resistance level above the given price."""
        resistances = [lv for lv in levels if not lv.is_support and lv.zone_low > price]
        if not resistances:
            return None
        resistances.sort(key=lambda lv: (-self._strength_rank(lv.strength), lv.price))
        return resistances[0]

    def analyze(
        self,
        candles: list[Candle],
        current_price: float,
    ) -> tuple[list[SwingPoint], list[KeyLevel]]:
        """Full analysis: find swings, build levels."""
        swings = self.find_swing_points(candles)
        levels = self.cluster_into_levels(swings, current_price)

        support_count = sum(1 for lv in levels if lv.is_support)
        resist_count = len(levels) - support_count
        logger.debug(
            "Structure: {} swings → {} levels ({} support, {} resistance)",
            len(swings),
            len(levels),
            support_count,
            resist_count,
        )
        return swings, levels

    @staticmethod
    def _classify_strength(
        touch_count: int,
        total_volume: float,
        cluster: list[SwingPoint],
    ) -> LevelStrength:
        has_both = any(s.is_high for s in cluster) and any(s.is_low for s in cluster)
        if touch_count >= 4 or (touch_count >= 3 and has_both):
            return LevelStrength.FORTRESS
        if touch_count >= 3 or (touch_count >= 2 and has_both):
            return LevelStrength.STRONG
        if touch_count >= 2:
            return LevelStrength.MODERATE
        return LevelStrength.WEAK

    @staticmethod
    def _strength_rank(strength: LevelStrength) -> int:
        return {
            LevelStrength.FORTRESS: 4,
            LevelStrength.STRONG: 3,
            LevelStrength.MODERATE: 2,
            LevelStrength.WEAK: 1,
        }.get(strength, 0)
