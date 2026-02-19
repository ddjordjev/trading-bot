from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from core.models import Candle, Ticker, OrderBook, Order, OrderSide, OrderType, OrderStatus, Position, MarketType


def parse_order_status(status: str) -> OrderStatus:
    """Translate ccxt order status strings to our enum."""
    mapping = {
        "open": OrderStatus.OPEN,
        "closed": OrderStatus.FILLED,
        "canceled": OrderStatus.CANCELLED,
        "cancelled": OrderStatus.CANCELLED,
        "expired": OrderStatus.CANCELLED,
        "rejected": OrderStatus.FAILED,
    }
    return mapping.get(status, OrderStatus.PENDING)


def ts_to_dt(ts: Any) -> datetime:
    """Convert a millisecond timestamp (from ccxt) to a timezone-aware datetime."""
    if ts is None:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc)


class BaseExchange(ABC):
    """Abstract exchange interface. Implement this for each exchange."""

    SUPPORTED_MARKET_TYPES: tuple[str, ...] = ("spot",)
    HAS_TESTNET: bool = False

    def __init__(self, api_key: str = "", api_secret: str = "", sandbox: bool = True):
        self.api_key = api_key
        self.api_secret = api_secret
        self.sandbox = sandbox

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    def supports(self, market_type: str) -> bool:
        return market_type.lower() in self.SUPPORTED_MARKET_TYPES

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
    async def watch_ticker(self, symbol: str, callback: Callable) -> None:
        """Subscribe to real-time ticker updates."""
        ...

    @abstractmethod
    async def watch_candles(self, symbol: str, timeframe: str, callback: Callable) -> None:
        """Subscribe to real-time candle updates."""
        ...
