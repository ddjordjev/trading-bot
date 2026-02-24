from __future__ import annotations

import math
from typing import Any

from core.models import Candle, Signal, SignalAction, Ticker
from strategies.base import BaseStrategy


class MeanReversionStrategy(BaseStrategy):
    """Mean reversion strategy.

    Assumes prices revert to the moving average. Buys when price is
    significantly below the MA, sells when significantly above.
    """

    @property
    def name(self) -> str:
        return "mean_reversion"

    def __init__(self, symbol: str, market_type: str = "spot", leverage: int = 1, **params: Any):
        super().__init__(symbol, market_type, leverage, **params)
        self.ma_period = int(params.get("ma_period", 50))
        self.deviation_pct = max(float(params.get("deviation_pct", 2.0)), 0.0001)

    def analyze(self, candles: list[Candle], ticker: Ticker | None = None) -> Signal | None:
        df = self.candles_to_df(candles)
        if len(df) < self.ma_period:
            return None

        ma = df["close"].rolling(self.ma_period).mean().iloc[-1]
        price = df["close"].iloc[-1]

        if math.isnan(ma) or math.isnan(price):
            return None
        if price <= 0 or ma == 0:
            return None

        deviation = (price - ma) / ma * 100

        if deviation <= -self.deviation_pct:
            return Signal(
                symbol=self.symbol,
                action=SignalAction.BUY,
                strength=min(1.0, abs(deviation) / (self.deviation_pct * 2)),
                strategy=self.name,
                reason=f"Price {deviation:.1f}% below {self.ma_period}-period MA",
                suggested_price=price,
                market_type=self.market_type,
                leverage=self.leverage,
            )

        if deviation >= self.deviation_pct:
            return Signal(
                symbol=self.symbol,
                action=SignalAction.SELL,
                strength=min(1.0, abs(deviation) / (self.deviation_pct * 2)),
                strategy=self.name,
                reason=f"Price {deviation:.1f}% above {self.ma_period}-period MA",
                suggested_price=price,
                market_type=self.market_type,
                leverage=self.leverage,
            )

        return None
