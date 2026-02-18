from __future__ import annotations

from typing import Optional

import ta

from core.models import Candle, Ticker, Signal, SignalAction
from strategies.base import BaseStrategy


class RSIStrategy(BaseStrategy):
    """Relative Strength Index strategy.

    Buys when RSI dips below oversold threshold, sells when above overbought.
    """

    @property
    def name(self) -> str:
        return "rsi"

    def __init__(self, symbol: str, market_type: str = "spot", leverage: int = 1, **params: object):
        super().__init__(symbol, market_type, leverage, **params)
        self.period = int(params.get("period", 14))
        self.oversold = float(params.get("oversold", 30))
        self.overbought = float(params.get("overbought", 70))

    def analyze(self, candles: list[Candle], ticker: Optional[Ticker] = None) -> Optional[Signal]:
        df = self.candles_to_df(candles)
        if len(df) < self.period + 1:
            return None

        rsi = ta.momentum.RSIIndicator(df["close"], window=self.period).rsi()
        current_rsi = rsi.iloc[-1]
        price = df["close"].iloc[-1]

        if current_rsi <= self.oversold:
            return Signal(
                symbol=self.symbol, action=SignalAction.BUY,
                strength=min(1.0, (self.oversold - current_rsi) / self.oversold),
                strategy=self.name, reason=f"RSI oversold at {current_rsi:.1f}",
                suggested_price=price, market_type=self.market_type, leverage=self.leverage,
            )

        if current_rsi >= self.overbought:
            return Signal(
                symbol=self.symbol, action=SignalAction.SELL,
                strength=min(1.0, (current_rsi - self.overbought) / (100 - self.overbought)),
                strategy=self.name, reason=f"RSI overbought at {current_rsi:.1f}",
                suggested_price=price, market_type=self.market_type, leverage=self.leverage,
            )

        return None
