from __future__ import annotations

import math
from typing import Any

import ta

from core.models import Candle, Signal, SignalAction, Ticker
from strategies.base import BaseStrategy


class BollingerStrategy(BaseStrategy):
    """Bollinger Bands mean reversion strategy.

    Buys when price touches/breaks below lower band,
    sells when price touches/breaks above upper band.
    """

    @property
    def name(self) -> str:
        return "bollinger"

    def __init__(self, symbol: str, market_type: str = "spot", leverage: int = 1, **params: Any):
        super().__init__(symbol, market_type, leverage, **params)
        self.period = int(params.get("period", 20))
        self.std_dev = float(params.get("std_dev", 2.0))

    def analyze(self, candles: list[Candle], ticker: Ticker | None = None) -> Signal | None:
        df = self.candles_to_df(candles)
        if len(df) < self.period:
            return None

        bb = ta.volatility.BollingerBands(df["close"], window=self.period, window_dev=self.std_dev)
        upper = bb.bollinger_hband().iloc[-1]
        lower = bb.bollinger_lband().iloc[-1]
        _middle = bb.bollinger_mavg().iloc[-1]
        price = df["close"].iloc[-1]

        if math.isnan(price) or math.isnan(upper) or math.isnan(lower):
            return None
        if price <= 0:
            return None
        band_width = upper - lower
        if band_width == 0:
            return None

        if price <= lower:
            distance = (lower - price) / band_width
            return Signal(
                symbol=self.symbol,
                action=SignalAction.BUY,
                strength=min(1.0, distance * 2),
                strategy=self.name,
                reason=f"Price at lower Bollinger band ({price:.2f} <= {lower:.2f})",
                suggested_price=price,
                market_type=self.market_type,
                leverage=self.leverage,
            )

        if price >= upper:
            distance = (price - upper) / band_width
            return Signal(
                symbol=self.symbol,
                action=SignalAction.SELL,
                strength=min(1.0, distance * 2),
                strategy=self.name,
                reason=f"Price at upper Bollinger band ({price:.2f} >= {upper:.2f})",
                suggested_price=price,
                market_type=self.market_type,
                leverage=self.leverage,
            )

        return None
