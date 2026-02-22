"""Momentum validator — confirms breakout/spike is still in progress."""

from __future__ import annotations

import math

import pandas as pd
import ta

from core.models import Candle, Ticker
from validators.base import ValidationResult, Validator


class MomentumValidator(Validator):
    MIN_CANDLES = 20
    RSI_OVERSOLD = 35
    RSI_OVERBOUGHT = 65

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
        volumes = pd.Series([c.volume for c in candles])

        rsi = ta.momentum.RSIIndicator(closes, window=14).rsi().iloc[-1]
        if not isinstance(rsi, (int, float)) or not math.isfinite(rsi):
            return ValidationResult(valid=False, reason="RSI not available")

        if side == "long" and rsi > self.RSI_OVERBOUGHT:
            return ValidationResult(valid=False, reason=f"RSI already overbought ({rsi:.0f})")
        if side == "short" and rsi < self.RSI_OVERSOLD:
            return ValidationResult(valid=False, reason=f"RSI already oversold ({rsi:.0f})")

        recent_vol = volumes.iloc[-3:].mean()
        avg_vol = volumes.mean()
        vol_ratio = recent_vol / avg_vol if avg_vol > 0 else 1.0
        if vol_ratio < 0.8:
            return ValidationResult(
                valid=False,
                reason=f"volume declining ({vol_ratio:.1f}x avg)",
            )

        ema_fast = closes.ewm(span=8).mean().iloc[-1]
        ema_slow = closes.ewm(span=21).mean().iloc[-1]
        if side == "long" and ema_fast < ema_slow:
            return ValidationResult(valid=False, reason="fast EMA below slow — trend weakening")
        if side == "short" and ema_fast > ema_slow:
            return ValidationResult(valid=False, reason="fast EMA above slow — trend weakening")

        return ValidationResult(valid=True, reason="momentum confirmed", confidence=0.85)
