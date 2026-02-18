from __future__ import annotations

from typing import Optional

from core.models import Candle, Ticker, Signal, SignalAction
from strategies.base import BaseStrategy


class GridStrategy(BaseStrategy):
    """Grid trading strategy.

    Places buy/sell signals at regular price intervals around a center price.
    Profits from sideways price action.
    """

    @property
    def name(self) -> str:
        return "grid"

    def __init__(self, symbol: str, market_type: str = "spot", leverage: int = 1, **params: object):
        super().__init__(symbol, market_type, leverage, **params)
        self.grid_size_pct = float(params.get("grid_size_pct", 1.0))
        self.num_grids = int(params.get("num_grids", 5))
        self._center_price: Optional[float] = None
        self._last_grid_level: int = 0

    def analyze(self, candles: list[Candle], ticker: Optional[Ticker] = None) -> Optional[Signal]:
        df = self.candles_to_df(candles)
        if len(df) < 2:
            return None

        price = df["close"].iloc[-1]

        if self._center_price is None:
            self._center_price = price
            return None

        grid_step = self._center_price * (self.grid_size_pct / 100)
        if grid_step == 0:
            return None

        current_level = int((price - self._center_price) / grid_step)

        if current_level == self._last_grid_level:
            return None

        moved_down = current_level < self._last_grid_level
        self._last_grid_level = current_level

        if moved_down:
            return Signal(
                symbol=self.symbol, action=SignalAction.BUY,
                strength=min(1.0, abs(current_level) / self.num_grids),
                strategy=self.name,
                reason=f"Grid buy at level {current_level} (price={price:.2f})",
                suggested_price=price, market_type=self.market_type, leverage=self.leverage,
            )

        return Signal(
            symbol=self.symbol, action=SignalAction.SELL,
            strength=min(1.0, abs(current_level) / self.num_grids),
            strategy=self.name,
            reason=f"Grid sell at level {current_level} (price={price:.2f})",
            suggested_price=price, market_type=self.market_type, leverage=self.leverage,
        )
