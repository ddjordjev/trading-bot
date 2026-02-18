from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class MarketType(str, Enum):
    SPOT = "spot"
    FUTURES = "futures"


class Candle(BaseModel):
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def body_pct(self) -> float:
        if self.open == 0:
            return 0.0
        return abs(self.close - self.open) / self.open * 100

    @property
    def range_pct(self) -> float:
        if self.low == 0:
            return 0.0
        return (self.high - self.low) / self.low * 100


class Ticker(BaseModel):
    symbol: str
    bid: float
    ask: float
    last: float
    volume_24h: float
    change_pct_24h: float
    timestamp: datetime

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_pct(self) -> float:
        if self.bid == 0:
            return 0.0
        return (self.ask - self.bid) / self.bid * 100


class OrderBook(BaseModel):
    symbol: str
    bids: list[tuple[float, float]]  # (price, amount)
    asks: list[tuple[float, float]]
    timestamp: datetime
