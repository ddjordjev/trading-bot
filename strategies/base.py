from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import pandas as pd

from core.models import Candle, Signal, Ticker


class BaseStrategy(ABC):
    """Base class for all trading strategies.

    Subclass this and implement `analyze` to create your own strategy.
    The bot calls `analyze` on every new candle/tick and acts on any
    returned Signal.
    """

    def __init__(self, symbol: str, market_type: str = "spot", leverage: int = 1, **params: Any):
        self.symbol = symbol
        self.market_type = market_type
        self.leverage = leverage
        self.params = params
        self._candle_history: list[Candle] = []
        self._max_history = int(params.get("max_history", 500))

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def analyze(self, candles: list[Candle], ticker: Ticker | None = None) -> Signal | None:
        """Analyze market data and optionally return a trading signal."""
        ...

    def feed_candle(self, candle: Candle) -> None:
        self._candle_history.append(candle)
        if len(self._candle_history) > self._max_history:
            self._candle_history = self._candle_history[-self._max_history :]

    def candles_to_df(self, candles: list[Candle] | None = None) -> pd.DataFrame:
        src = candles or self._candle_history
        if not src:
            return pd.DataFrame()
        data = [
            {
                "timestamp": c.timestamp,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
            }
            for c in src
        ]
        df = pd.DataFrame(data)
        df.set_index("timestamp", inplace=True)
        return df

    def set_position_state(self, has_position: bool, side: str | None = None) -> None:  # noqa: B027
        """Sync strategy's internal position state from exchange data.

        Called by the bot each tick so strategies stay in sync after restarts.
        Override in subclasses that track internal position state.
        """

    def reset(self) -> None:
        self._candle_history.clear()

    def _latest_atr_pct(self, df: pd.DataFrame, atr_period: int = 14) -> float:
        """Approximate ATR/price in percent for crypto volatility gating."""
        if len(df) < max(atr_period + 1, 5):
            return 0.0
        high = df["high"]
        low = df["low"]
        close = df["close"]
        tr = pd.concat(
            [
                (high - low).abs(),
                (high - close.shift(1)).abs(),
                (low - close.shift(1)).abs(),
            ],
            axis=1,
        ).max(axis=1)
        atr = float(tr.rolling(atr_period).mean().iloc[-1])
        price = float(close.iloc[-1])
        if price <= 0:
            return 0.0
        return (atr / price) * 100.0

    def _passes_crypto_market_filters(
        self,
        df: pd.DataFrame,
        *,
        min_quote_volume_usd: float = 100_000.0,
        min_atr_pct: float = 0.10,
        max_atr_pct: float = 25.0,
        volume_window: int = 20,
        atr_period: int = 14,
    ) -> bool:
        """Require enough liquidity and sane volatility for crypto entries."""
        if df.empty or len(df) < max(volume_window, atr_period + 2):
            return False
        last_price = float(df["close"].iloc[-1])
        if last_price <= 0:
            return False
        recent_quote_volume = float((df["close"] * df["volume"]).iloc[-volume_window:].mean())
        atr_pct = self._latest_atr_pct(df, atr_period=atr_period)
        if recent_quote_volume < min_quote_volume_usd:
            return False
        if atr_pct < min_atr_pct:
            return False
        return not (max_atr_pct > 0 and atr_pct > max_atr_pct)
