from __future__ import annotations

import ta

from core.models import Candle, Signal, SignalAction, Ticker
from strategies.base import BaseStrategy


class MACDStrategy(BaseStrategy):
    """MACD crossover strategy.

    Buys on bullish crossover (MACD crosses above signal line),
    sells on bearish crossover.
    """

    @property
    def name(self) -> str:
        return "macd"

    def __init__(self, symbol: str, market_type: str = "spot", leverage: int = 1, **params: object):
        super().__init__(symbol, market_type, leverage, **params)
        self.fast = int(params.get("fast", 12))
        self.slow = int(params.get("slow", 26))
        self.signal_period = int(params.get("signal_period", 9))

    def analyze(self, candles: list[Candle], ticker: Ticker | None = None) -> Signal | None:
        df = self.candles_to_df(candles)
        if len(df) < self.slow + self.signal_period:
            return None

        macd_ind = ta.trend.MACD(
            df["close"], window_slow=self.slow, window_fast=self.fast, window_sign=self.signal_period
        )
        _macd_line = macd_ind.macd()
        _signal_line = macd_ind.macd_signal()
        histogram = macd_ind.macd_diff()

        curr_hist = histogram.iloc[-1]
        prev_hist = histogram.iloc[-2]
        price = df["close"].iloc[-1]

        if prev_hist <= 0 < curr_hist:
            return Signal(
                symbol=self.symbol,
                action=SignalAction.BUY,
                strength=min(1.0, abs(curr_hist) / price * 1000),
                strategy=self.name,
                reason="MACD bullish crossover",
                suggested_price=price,
                market_type=self.market_type,
                leverage=self.leverage,
            )

        if prev_hist >= 0 > curr_hist:
            return Signal(
                symbol=self.symbol,
                action=SignalAction.SELL,
                strength=min(1.0, abs(curr_hist) / price * 1000),
                strategy=self.name,
                reason="MACD bearish crossover",
                suggested_price=price,
                market_type=self.market_type,
                leverage=self.leverage,
            )

        return None
