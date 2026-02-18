from __future__ import annotations

from loguru import logger

from core.models import Candle, Ticker


class MarketQualityFilter:
    """Evaluates whether market conditions are worth trading.

    If conditions are choppy, low-volume, or directionless, the bot
    should sit on its hands. Missing a trade is always better than
    forcing one in bad conditions.
    """

    def __init__(
        self,
        min_volume_ratio: float = 0.7,
        max_spread_pct: float = 0.3,
        min_atr_ratio: float = 0.5,
        max_chop_score: float = 0.7,
    ):
        self.min_volume_ratio = min_volume_ratio
        self.max_spread_pct = max_spread_pct
        self.min_atr_ratio = min_atr_ratio
        self.max_chop_score = max_chop_score

    def is_tradeable(self, candles: list[Candle], ticker: Ticker) -> tuple[bool, str]:
        """Returns (tradeable, reason). If not tradeable, reason explains why."""

        if not candles or len(candles) < 30:
            return False, "insufficient data"

        # Spread check -- wide spreads mean low liquidity
        if ticker.spread_pct > self.max_spread_pct:
            return False, f"spread too wide ({ticker.spread_pct:.2f}%)"

        # Volume check -- is volume at least a fraction of the recent average?
        volumes = [c.volume for c in candles[-30:]]
        avg_vol = sum(volumes) / len(volumes) if volumes else 0
        recent_vol = sum(volumes[-5:]) / 5 if len(volumes) >= 5 else 0

        if avg_vol > 0 and recent_vol / avg_vol < self.min_volume_ratio:
            return False, f"volume dry ({recent_vol/avg_vol:.1f}x vs avg)"

        # Choppiness check -- are candles just wicking back and forth?
        chop_score = self._choppiness(candles[-20:])
        if chop_score > self.max_chop_score:
            return False, f"market too choppy (score={chop_score:.2f})"

        # ATR check -- is there enough movement to be worth trading?
        atr_ratio = self._atr_ratio(candles[-30:])
        if atr_ratio < self.min_atr_ratio:
            return False, f"not enough movement (ATR ratio={atr_ratio:.2f})"

        return True, "conditions OK"

    @staticmethod
    def _choppiness(candles: list[Candle]) -> float:
        """0 = trending, 1 = choppy. Based on wick-to-body ratio."""
        if not candles:
            return 1.0

        scores = []
        for c in candles:
            body = abs(c.close - c.open)
            full_range = c.high - c.low
            if full_range == 0:
                scores.append(1.0)
            else:
                scores.append(1.0 - body / full_range)

        return sum(scores) / len(scores)

    @staticmethod
    def _atr_ratio(candles: list[Candle]) -> float:
        """Recent ATR vs historical ATR. < 1 means less volatile than usual."""
        if len(candles) < 20:
            return 1.0

        ranges = [c.high - c.low for c in candles]
        full_atr = sum(ranges) / len(ranges)
        recent_atr = sum(ranges[-5:]) / 5

        if full_atr == 0:
            return 0.0
        return recent_atr / full_atr
