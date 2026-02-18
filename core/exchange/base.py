from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from core.models import Candle, Ticker, OrderBook, Order, OrderSide, OrderType, Position, MarketType


class BaseExchange(ABC):
    """Abstract exchange interface. Implement this for each exchange."""

    def __init__(self, api_key: str = "", api_secret: str = "", sandbox: bool = True):
        self.api_key = api_key
        self.api_secret = api_secret
        self.sandbox = sandbox

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    async def connect(self) -> None:
        """Initialize connection and load markets."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Clean up connections."""
        ...

    # -- Market Data --

    @abstractmethod
    async def fetch_ticker(self, symbol: str) -> Ticker:
        ...

    @abstractmethod
    async def fetch_tickers(self, symbols: Optional[list[str]] = None) -> list[Ticker]:
        ...

    @abstractmethod
    async def fetch_candles(
        self, symbol: str, timeframe: str = "1m", limit: int = 100
    ) -> list[Candle]:
        ...

    @abstractmethod
    async def fetch_order_book(self, symbol: str, limit: int = 20) -> OrderBook:
        ...

    # -- Account --

    @abstractmethod
    async def fetch_balance(self) -> dict[str, float]:
        """Returns {asset: free_balance}."""
        ...

    @abstractmethod
    async def fetch_positions(self, symbol: Optional[str] = None) -> list[Position]:
        ...

    # -- Trading --

    @abstractmethod
    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        amount: float,
        price: Optional[float] = None,
        stop_price: Optional[float] = None,
        leverage: int = 1,
        market_type: MarketType = MarketType.SPOT,
    ) -> Order:
        ...

    @abstractmethod
    async def cancel_order(self, order_id: str, symbol: str) -> Order:
        ...

    @abstractmethod
    async def fetch_order(self, order_id: str, symbol: str) -> Order:
        ...

    @abstractmethod
    async def fetch_open_orders(self, symbol: Optional[str] = None) -> list[Order]:
        ...

    # -- Futures specific --

    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: int) -> None:
        ...

    # -- Symbols --

    @abstractmethod
    async def get_available_symbols(self, market_type: MarketType = MarketType.SPOT) -> list[str]:
        ...

    # -- Stream --

    @abstractmethod
    async def watch_ticker(self, symbol: str, callback: object) -> None:
        """Subscribe to real-time ticker updates."""
        ...

    @abstractmethod
    async def watch_candles(self, symbol: str, timeframe: str, callback: object) -> None:
        """Subscribe to real-time candle updates."""
        ...
