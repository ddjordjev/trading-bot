from __future__ import annotations

import math
from typing import Any

import ta

from core.market_schedule import get_market_schedule
from core.models import Candle, Signal, SignalAction, Ticker
from core.models.signal import TickUrgency
from strategies.base import BaseStrategy


class MarketOpenVolatilityStrategy(BaseStrategy):
    """Exploits volatility during US and Asia market open windows.

    Uses the global MarketSchedule for DST-aware, holiday-aware market hours.
    Looks for directional momentum using ATR spikes and volume surge during
    the first N minutes after market open. Generates quick in-and-out signals.
    """

    @property
    def name(self) -> str:
        return "market_open_volatility"

    def __init__(self, symbol: str, market_type: str = "spot", leverage: int = 1, **params: Any):
        super().__init__(symbol, market_type, leverage, **params)
        self.open_window_minutes = int(params.get("open_window_minutes", 120))
        self.atr_period = int(params.get("atr_period", 14))
        self.atr_multiplier = float(params.get("atr_multiplier", 1.5))
        self.volume_surge_multiplier = float(params.get("volume_surge_multiplier", 2.0))
        self.max_hold_minutes = int(params.get("max_hold_minutes", 30))
        self._schedule = get_market_schedule()

    def _is_market_open_window(self) -> str | None:
        """Check if any tracked market is in its open window (DST + holiday aware)."""
        for market in ("US", "ASIA", "EUROPE"):
            if self._schedule.is_in_open_window(market, self.open_window_minutes):
                return market
        return None

    def analyze(self, candles: list[Candle], ticker: Ticker | None = None) -> Signal | None:
        market = self._is_market_open_window()
        if not market:
            return None

        df = self.candles_to_df(candles)
        if len(df) < self.atr_period + 5:
            return None

        atr = ta.volatility.AverageTrueRange(
            df["high"], df["low"], df["close"], window=self.atr_period
        ).average_true_range()
        current_atr = atr.iloc[-1]
        if math.isnan(current_atr):
            return None
        avg_atr = atr.iloc[-self.atr_period :].mean()

        avg_volume = df["volume"].iloc[-20:].mean()
        current_volume = df["volume"].iloc[-1]

        atr_spike = current_atr > avg_atr * self.atr_multiplier
        volume_surge = current_volume > avg_volume * self.volume_surge_multiplier

        if not (atr_spike or volume_surge):
            return None

        price = df["close"].iloc[-1]
        if not math.isfinite(price) or price <= 0:
            return None
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
            tick_urgency=TickUrgency.SCALP,
        )
