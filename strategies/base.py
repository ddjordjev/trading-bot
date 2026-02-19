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
