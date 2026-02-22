"""Indicators validator — verifies RSI/MACD is still in signal zone."""

from __future__ import annotations

import pandas as pd
import ta

from core.models import Candle, Ticker
from validators.base import ValidationResult, Validator


class IndicatorsValidator(Validator):
    MIN_CANDLES = 30

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

        rsi = ta.momentum.RSIIndicator(closes, window=14).rsi().iloc[-1]
        macd_ind = ta.trend.MACD(closes, window_slow=26, window_fast=12, window_sign=9)
        macd_line = macd_ind.macd().iloc[-1]
        signal_line = macd_ind.macd_signal().iloc[-1]
        macd_hist = macd_line - signal_line

        if side == "long":
            rsi_ok = rsi < 70
            macd_ok = macd_hist > 0 or macd_line > signal_line
            if not rsi_ok:
                return ValidationResult(valid=False, reason=f"RSI too high for long ({rsi:.0f})")
            if not macd_ok:
                return ValidationResult(valid=False, reason="MACD bearish for long entry")
        else:
            rsi_ok = rsi > 30
            macd_ok = macd_hist < 0 or macd_line < signal_line
            if not rsi_ok:
                return ValidationResult(valid=False, reason=f"RSI too low for short ({rsi:.0f})")
            if not macd_ok:
                return ValidationResult(valid=False, reason="MACD bullish for short entry")

        return ValidationResult(valid=True, reason="indicators confirm signal zone", confidence=0.8)
