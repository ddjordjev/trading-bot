"""Example custom strategy. Copy this file, rename it, and implement your logic.

Your class must:
1. Extend BaseStrategy
2. Have a `name` property
3. Implement `analyze(candles, ticker)` returning Optional[Signal]

Place the file in this directory (custom_strategies/) and it will be
auto-discovered on bot startup.
"""

from __future__ import annotations

from core.models import Candle, Signal, Ticker
from strategies.base import BaseStrategy


class ExampleStrategy(BaseStrategy):
    @property
    def name(self) -> str:
        return "my_custom_strategy"

    def analyze(self, candles: list[Candle], ticker: Ticker | None = None) -> Signal | None:
        # Your logic here — return a Signal to trade, or None to skip
        return None
