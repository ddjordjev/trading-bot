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
        self.min_band_width_pct = float(params.get("min_band_width_pct", 0.8))
        self.volume_confirm_mult = float(params.get("volume_confirm_mult", 1.1))
        self.trend_ma_period = int(params.get("trend_ma_period", 50))
        self.require_reversal_candle = bool(params.get("require_reversal_candle", True))
        self.min_quote_volume_usd = float(params.get("min_quote_volume_usd", 100_000.0))
        self.min_atr_pct = float(params.get("min_atr_pct", 0.12))
        self.max_atr_pct = float(params.get("max_atr_pct", 24.0))

    def analyze(self, candles: list[Candle], ticker: Ticker | None = None) -> Signal | None:
        df = self.candles_to_df(candles)
        if len(df) < max(self.period, self.trend_ma_period):
            return None
        if not self._passes_crypto_market_filters(
            df,
            min_quote_volume_usd=self.min_quote_volume_usd,
            min_atr_pct=self.min_atr_pct,
            max_atr_pct=self.max_atr_pct,
        ):
            return None

        bb = ta.volatility.BollingerBands(df["close"], window=self.period, window_dev=self.std_dev)
        upper = bb.bollinger_hband().iloc[-1]
        lower = bb.bollinger_lband().iloc[-1]
        price = df["close"].iloc[-1]

        if math.isnan(price) or math.isnan(upper) or math.isnan(lower):
            return None
        if price <= 0:
            return None
        band_width = upper - lower
        if band_width == 0:
            return None
        band_width_pct = (band_width / price) * 100
        if band_width_pct < self.min_band_width_pct:
            return None

        avg_vol = float(df["volume"].iloc[-20:].mean())
        current_vol = float(df["volume"].iloc[-1])
        if avg_vol <= 0 or current_vol < avg_vol * self.volume_confirm_mult:
            return None
        trend_ma = float(df["close"].rolling(self.trend_ma_period).mean().iloc[-1])
        bullish_reversal = float(df["close"].iloc[-1]) >= float(df["open"].iloc[-1])
        bearish_reversal = float(df["close"].iloc[-1]) <= float(df["open"].iloc[-1])

        if price <= lower:
            if price < trend_ma:
                return None
            if self.require_reversal_candle and not bullish_reversal:
                return None
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
            if price > trend_ma:
                return None
            if self.require_reversal_candle and not bearish_reversal:
                return None
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
