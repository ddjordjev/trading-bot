from __future__ import annotations

import math
from typing import Any

import ta

from core.models import Candle, Signal, SignalAction, Ticker
from strategies.base import BaseStrategy


class RSIStrategy(BaseStrategy):
    """Relative Strength Index strategy.

    Buys when RSI dips below oversold threshold, sells when above overbought.
    """

    @property
    def name(self) -> str:
        return "rsi"

    def __init__(self, symbol: str, market_type: str = "spot", leverage: int = 1, **params: Any):
        super().__init__(symbol, market_type, leverage, **params)
        self.period = int(params.get("period", 14))
        self.oversold = float(params.get("oversold", 30))
        self.overbought = float(params.get("overbought", 70))

    def analyze(self, candles: list[Candle], ticker: Ticker | None = None) -> Signal | None:
        df = self.candles_to_df(candles)
        if len(df) < self.period + 1:
            return None

        rsi = ta.momentum.RSIIndicator(df["close"], window=self.period).rsi()
        current_rsi = rsi.iloc[-1]
        price = df["close"].iloc[-1]
        if math.isnan(current_rsi) or not math.isfinite(price) or price <= 0:
            return None

        if current_rsi <= self.oversold:
            raw = (self.oversold - current_rsi) / self.oversold if self.oversold else 0
            return Signal(
                symbol=self.symbol,
                action=SignalAction.BUY,
                strength=max(0.4, min(1.0, raw + 0.4)),
                strategy=self.name,
                reason=f"RSI oversold at {current_rsi:.1f}",
                suggested_price=price,
                market_type=self.market_type,
                leverage=self.leverage,
            )

        if current_rsi >= self.overbought:
            denom = 100 - self.overbought
            raw = (current_rsi - self.overbought) / denom if denom else 0
            return Signal(
                symbol=self.symbol,
                action=SignalAction.SELL,
                strength=max(0.4, min(1.0, raw + 0.4)),
                strategy=self.name,
                reason=f"RSI overbought at {current_rsi:.1f}",
                suggested_price=price,
                market_type=self.market_type,
                leverage=self.leverage,
            )

        return None
