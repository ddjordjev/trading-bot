from __future__ import annotations

from enum import Enum

from core.models import Candle, Ticker


class LiquidityTier(str, Enum):
    HIGH = "high"  # major coins, deep books, safe for full positions
    MEDIUM = "medium"  # decent liquidity, ok for normal trades
    LOW = "low"  # shitcoin territory -- only gambling bets allowed
    DEAD = "dead"  # untradeable


class LiquidityProfile:
    """Assessment of a symbol's liquidity."""

    def __init__(
        self, tier: LiquidityTier, volume_24h: float, spread_pct: float, avg_candle_volume: float, reason: str = ""
    ):
        self.tier = tier
        self.volume_24h = volume_24h
        self.spread_pct = spread_pct
        self.avg_candle_volume = avg_candle_volume
        self.reason = reason

    @property
    def is_safe_for_stops(self) -> bool:
        """Whether exchange stop-losses can be trusted on this pair."""
        return self.tier in (LiquidityTier.HIGH, LiquidityTier.MEDIUM)

    @property
    def max_position_multiplier(self) -> float:
        """Scale position size based on liquidity. 1.0 = full, 0.0 = don't trade."""
        return {
            LiquidityTier.HIGH: 1.0,
            LiquidityTier.MEDIUM: 0.7,
            LiquidityTier.LOW: 0.15,
            LiquidityTier.DEAD: 0.0,
        }[self.tier]


class MarketQualityFilter:
    """Evaluates whether market conditions are worth trading.

    Now includes explicit liquidity assessment. On smaller-cap symbols, low-liquidity coins
    have unreliable stop-loss execution: the price can wick through your SL
    and liquidate you before the exchange reacts. We classify each symbol
    and adjust behavior accordingly.
    """

    def __init__(
        self,
        min_volume_ratio: float = 0.7,
        max_spread_pct: float = 0.3,
        min_atr_ratio: float = 0.5,
        max_chop_score: float = 0.7,
        min_liquidity_volume: float = 1_000_000,
    ):
        self.min_volume_ratio = min_volume_ratio
        self.max_spread_pct = max_spread_pct
        self.min_atr_ratio = min_atr_ratio
        self.max_chop_score = max_chop_score
        self.min_liquidity_volume = min_liquidity_volume

    def assess_liquidity(self, candles: list[Candle], ticker: Ticker) -> LiquidityProfile:
        """Classify how liquid a symbol is."""
        if not candles:
            return LiquidityProfile(LiquidityTier.DEAD, 0, 0, 0, "no data")

        volumes = [c.volume for c in candles[-30:]]
        avg_vol = sum(volumes) / len(volumes) if volumes else 0
        vol_24h = sum(c.volume * c.close for c in candles[-1440:]) if len(candles) > 100 else avg_vol * 1440

        spread = ticker.spread_pct

        if spread > 1.0 or avg_vol == 0:
            return LiquidityProfile(
                LiquidityTier.DEAD, vol_24h, spread, avg_vol, f"spread={spread:.2f}%, avg_vol={avg_vol:.0f}"
            )

        if vol_24h < self.min_liquidity_volume or spread > 0.5:
            return LiquidityProfile(
                LiquidityTier.LOW, vol_24h, spread, avg_vol, f"vol_24h={vol_24h:.0f}, spread={spread:.2f}%"
            )

        if vol_24h < self.min_liquidity_volume * 10 or spread > 0.2:
            return LiquidityProfile(
                LiquidityTier.MEDIUM, vol_24h, spread, avg_vol, f"vol_24h={vol_24h:.0f}, spread={spread:.2f}%"
            )

        return LiquidityProfile(LiquidityTier.HIGH, vol_24h, spread, avg_vol, "deep liquidity")

    def is_tradeable(self, candles: list[Candle], ticker: Ticker) -> tuple[bool, str]:
        """Returns (tradeable, reason). If not tradeable, reason explains why."""

        if not candles or len(candles) < 30:
            return False, "insufficient data"

        liq = self.assess_liquidity(candles, ticker)
        if liq.tier == LiquidityTier.DEAD:
            return False, f"dead liquidity ({liq.reason})"

        if ticker.spread_pct > self.max_spread_pct and liq.tier != LiquidityTier.LOW:
            return False, f"spread too wide ({ticker.spread_pct:.2f}%)"

        volumes = [c.volume for c in candles[-30:]]
        avg_vol = sum(volumes) / len(volumes) if volumes else 0
        recent_vol = sum(volumes[-5:]) / 5 if len(volumes) >= 5 else 0

        if avg_vol > 0 and recent_vol / avg_vol < self.min_volume_ratio:
            return False, f"volume dry ({recent_vol / avg_vol:.1f}x vs avg)"

        chop_score = self._choppiness(candles[-20:])
        if chop_score > self.max_chop_score:
            return False, f"market too choppy (score={chop_score:.2f})"

        atr_ratio = self._atr_ratio(candles[-30:])
        if atr_ratio < self.min_atr_ratio:
            return False, f"not enough movement (ATR ratio={atr_ratio:.2f})"

        return True, f"OK (liquidity={liq.tier.value})"

    def is_low_liquidity(self, candles: list[Candle], ticker: Ticker) -> bool:
        liq = self.assess_liquidity(candles, ticker)
        return liq.tier in (LiquidityTier.LOW, LiquidityTier.DEAD)

    @staticmethod
    def _choppiness(candles: list[Candle]) -> float:
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
        if len(candles) < 20:
            return 1.0
        ranges = [c.high - c.low for c in candles]
        full_atr = sum(ranges) / len(ranges)
        recent_atr = sum(ranges[-5:]) / 5
        if full_atr == 0:
            return 0.0
        return recent_atr / full_atr
