"""Mean reversion validator — confirms price is still extended from mean."""

from __future__ import annotations

import math

import pandas as pd
import ta

from core.models import Candle, Ticker
from validators.base import ValidationResult, Validator


class MeanRevValidator(Validator):
    MIN_CANDLES = 20
    BB_PERIOD = 20
    BB_STD = 2.0

    def validate(
        self,
        candles: list[Candle],
        ticker: Ticker | None,
        side: str,
        strategy: str,
    ) -> ValidationResult:
        if len(candles) < self.MIN_CANDLES:
            return ValidationResult(valid=False, reason="insufficient candles")

        closes = pd.Series([c.close for c in candles])
        price = closes.iloc[-1]

        bb = ta.volatility.BollingerBands(closes, window=self.BB_PERIOD, window_dev=self.BB_STD)
        mid = bb.bollinger_mavg().iloc[-1]

        if not isinstance(price, (int, float)) or not math.isfinite(price):
            return ValidationResult(valid=False, reason="invalid or missing price")
        if not isinstance(mid, (int, float)) or not math.isfinite(mid):
            return ValidationResult(valid=False, reason="indicator not ready")

        if side == "long":
            if price > mid:
                return ValidationResult(
                    valid=False,
                    reason=f"price already reverted above mean ({price:.4f} > {mid:.4f})",
                )
            deviation = (mid - price) / mid * 100 if mid > 0 else 0
            if deviation < 0.5:
                return ValidationResult(
                    valid=False,
                    reason=f"price not extended enough from mean ({deviation:.1f}%)",
                )
        else:
            if price < mid:
                return ValidationResult(
                    valid=False,
                    reason=f"price already reverted below mean ({price:.4f} < {mid:.4f})",
                )
            deviation = (price - mid) / mid * 100 if mid > 0 else 0
            if deviation < 0.5:
                return ValidationResult(
                    valid=False,
                    reason=f"price not extended enough from mean ({deviation:.1f}%)",
                )

        return ValidationResult(valid=True, reason="price still extended from mean", confidence=0.8)
