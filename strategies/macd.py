from __future__ import annotations

import math
from typing import Any

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

    def __init__(self, symbol: str, market_type: str = "spot", leverage: int = 1, **params: Any):
        super().__init__(symbol, market_type, leverage, **params)
        self.fast = int(params.get("fast", 12))
        self.slow = int(params.get("slow", 26))
        self.signal_period = int(params.get("signal_period", 9))
        self.trend_ma_period = int(params.get("trend_ma_period", 200))
        self.histogram_min_atr_mult = float(params.get("histogram_min_atr_mult", 0.15))
        self.require_trend_alignment = bool(params.get("require_trend_alignment", True))
        self.min_quote_volume_usd = float(params.get("min_quote_volume_usd", 150_000.0))
        self.min_atr_pct = float(params.get("min_atr_pct", 0.12))
        self.max_atr_pct = float(params.get("max_atr_pct", 22.0))

    def analyze(self, candles: list[Candle], ticker: Ticker | None = None) -> Signal | None:
        df = self.candles_to_df(candles)
        if len(df) < max(self.slow + self.signal_period, self.trend_ma_period):
            return None
        if not self._passes_crypto_market_filters(
            df,
            min_quote_volume_usd=self.min_quote_volume_usd,
            min_atr_pct=self.min_atr_pct,
            max_atr_pct=self.max_atr_pct,
        ):
            return None

        macd_ind = ta.trend.MACD(
            df["close"], window_slow=self.slow, window_fast=self.fast, window_sign=self.signal_period
        )
        histogram = macd_ind.macd_diff()

        curr_hist = histogram.iloc[-1]
        prev_hist = histogram.iloc[-2]
        price = df["close"].iloc[-1]
        if math.isnan(curr_hist) or math.isnan(prev_hist) or math.isnan(price):
            return None
        if price <= 0:
            return None
        atr = (self._latest_atr_pct(df) / 100.0) * price
        if abs(curr_hist) < atr * self.histogram_min_atr_mult:
            return None

        trend_ma = float(df["close"].rolling(self.trend_ma_period).mean().iloc[-1])

        if prev_hist <= 0 < curr_hist:
            if self.require_trend_alignment and price < trend_ma:
                return None
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
            if self.require_trend_alignment and price > trend_ma:
                return None
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
