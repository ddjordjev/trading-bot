"""Swing validator — checks support/resistance levels still hold."""

from __future__ import annotations

import math

import pandas as pd

from core.models import Candle, Ticker
from validators.base import ValidationResult, Validator


class SwingValidator(Validator):
    MIN_CANDLES = 30
    LOOKBACK = 20

    def validate(
        self,
        candles: list[Candle],
        ticker: Ticker | None,
        side: str,
        strategy: str,
    ) -> ValidationResult:
        if len(candles) < self.MIN_CANDLES:
            return ValidationResult(valid=False, reason="insufficient candles")

        highs = pd.Series([c.high for c in candles])
        lows = pd.Series([c.low for c in candles])
        closes = pd.Series([c.close for c in candles])
        price = closes.iloc[-1]

        recent_high = highs.iloc[-self.LOOKBACK :].max()
        recent_low = lows.iloc[-self.LOOKBACK :].min()
        price_range = recent_high - recent_low

        if not isinstance(price, (int, float)) or not math.isfinite(price):
            return ValidationResult(valid=False, reason="invalid or missing price")
        if not math.isfinite(recent_high) or not math.isfinite(recent_low):
            return ValidationResult(valid=False, reason="invalid high/low")
        if not math.isfinite(price_range) or price_range <= 0:
            return ValidationResult(valid=False, reason="no price range detected")

        position_in_range = (price - recent_low) / price_range

        if side == "long" and position_in_range > 0.85:
            return ValidationResult(
                valid=False,
                reason=f"price near resistance ({position_in_range:.0%} of range)",
            )
        if side == "short" and position_in_range < 0.15:
            return ValidationResult(
                valid=False,
                reason=f"price near support ({position_in_range:.0%} of range)",
            )

        ma_50 = closes.rolling(min(50, len(closes))).mean().iloc[-1]
        if side == "long" and price < ma_50 * 0.95:
            return ValidationResult(valid=False, reason="price well below MA — downtrend")

        if side == "short" and price > ma_50 * 1.05:
            return ValidationResult(valid=False, reason="price well above MA — uptrend")

        return ValidationResult(valid=True, reason="support/resistance structure intact", confidence=0.75)
