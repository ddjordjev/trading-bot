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
        self.period = int(params.get("period", 7))
        self.oversold = float(params.get("oversold", 25))
        self.overbought = float(params.get("overbought", 75))
        self.trend_ma_period = int(params.get("trend_ma_period", 50))
        self.trend_tolerance_pct = float(params.get("trend_tolerance_pct", 1.0))
        self.require_trend_alignment = bool(params.get("require_trend_alignment", True))
        self.min_quote_volume_usd = float(params.get("min_quote_volume_usd", 100_000.0))
        self.min_atr_pct = float(params.get("min_atr_pct", 0.12))
        self.max_atr_pct = float(params.get("max_atr_pct", 22.0))

    def analyze(self, candles: list[Candle], ticker: Ticker | None = None) -> Signal | None:
        df = self.candles_to_df(candles)
        if len(df) < max(self.period + 1, self.trend_ma_period):
            return None
        if not self._passes_crypto_market_filters(
            df,
            min_quote_volume_usd=self.min_quote_volume_usd,
            min_atr_pct=self.min_atr_pct,
            max_atr_pct=self.max_atr_pct,
        ):
            return None

        rsi = ta.momentum.RSIIndicator(df["close"], window=self.period).rsi()
        current_rsi = rsi.iloc[-1]
        price = df["close"].iloc[-1]
        if math.isnan(current_rsi) or not math.isfinite(price) or price <= 0:
            return None

        trend_ma = float(df["close"].rolling(self.trend_ma_period).mean().iloc[-1])
        trend_tolerance = self.trend_tolerance_pct / 100.0

        if current_rsi <= self.oversold:
            if self.require_trend_alignment and price < trend_ma * (1 - trend_tolerance):
                return None
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
            if self.require_trend_alignment and price > trend_ma * (1 + trend_tolerance):
                return None
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
