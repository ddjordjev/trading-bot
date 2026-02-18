from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import ta

from config.settings import get_settings
from core.models import Candle, Ticker, Signal, SignalAction
from strategies.base import BaseStrategy


class MarketOpenVolatilityStrategy(BaseStrategy):
    """Exploits volatility during US and Asia market open windows.

    Only active during configurable market open hours. Looks for directional
    momentum using ATR spikes and volume surge. Generates quick in-and-out signals.
    """

    @property
    def name(self) -> str:
        return "market_open_volatility"

    def __init__(self, symbol: str, market_type: str = "spot", leverage: int = 1, **params: object):
        super().__init__(symbol, market_type, leverage, **params)
        settings = get_settings()
        self.us_open = int(params.get("us_open", settings.us_market_open_utc))
        self.us_close = int(params.get("us_close", settings.us_market_close_utc))
        self.asia_open = int(params.get("asia_open", settings.asia_market_open_utc))
        self.asia_close = int(params.get("asia_close", settings.asia_market_close_utc))
        self.atr_period = int(params.get("atr_period", 14))
        self.atr_multiplier = float(params.get("atr_multiplier", 1.5))
        self.volume_surge_multiplier = float(params.get("volume_surge_multiplier", 2.0))
        self.max_hold_minutes = int(params.get("max_hold_minutes", 30))

    def _is_market_open_window(self) -> str | None:
        hour = datetime.now(timezone.utc).hour
        if self.us_open <= hour < min(self.us_open + 2, self.us_close):
            return "US"
        if self.asia_open <= hour < min(self.asia_open + 2, self.asia_close):
            return "ASIA"
        return None

    def analyze(self, candles: list[Candle], ticker: Optional[Ticker] = None) -> Optional[Signal]:
        market = self._is_market_open_window()
        if not market:
            return None

        df = self.candles_to_df(candles)
        if len(df) < self.atr_period + 5:
            return None

        atr = ta.volatility.AverageTrueRange(df["high"], df["low"], df["close"],
                                               window=self.atr_period).average_true_range()
        current_atr = atr.iloc[-1]
        avg_atr = atr.iloc[-self.atr_period:].mean()

        avg_volume = df["volume"].iloc[-20:].mean()
        current_volume = df["volume"].iloc[-1]

        atr_spike = current_atr > avg_atr * self.atr_multiplier
        volume_surge = current_volume > avg_volume * self.volume_surge_multiplier

        if not (atr_spike or volume_surge):
            return None

        price = df["close"].iloc[-1]
        prev_price = df["close"].iloc[-2]
        direction_up = price > prev_price

        strength = 0.0
        if atr_spike:
            strength += 0.5
        if volume_surge:
            strength += 0.5

        action = SignalAction.BUY if direction_up else SignalAction.SELL

        return Signal(
            symbol=self.symbol,
            action=action,
            strength=min(1.0, strength),
            strategy=self.name,
            reason=f"{market} market open volatility - ATR spike: {atr_spike}, Volume surge: {volume_surge}",
            suggested_price=price,
            market_type=self.market_type,
            leverage=self.leverage,
            quick_trade=True,
            max_hold_minutes=self.max_hold_minutes,
        )
