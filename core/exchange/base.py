from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from core.models import Candle, MarketType, Order, OrderBook, OrderSide, OrderStatus, OrderType, Position, Ticker


def parse_order_status(status: str) -> OrderStatus:
    """Translate ccxt order status strings to our enum."""
    mapping = {
        "open": OrderStatus.OPEN,
        "closed": OrderStatus.FILLED,
        "filled": OrderStatus.FILLED,
        "canceled": OrderStatus.CANCELLED,
        "cancelled": OrderStatus.CANCELLED,
        "expired": OrderStatus.CANCELLED,
        "rejected": OrderStatus.FAILED,
        "partially_filled": OrderStatus.PARTIALLY_FILLED,
    }
    return mapping.get(status, OrderStatus.PENDING)


def parse_order_type(order_type: str) -> OrderType:
    """Translate ccxt order type strings to our enum."""
    raw = str(order_type or "").strip().lower()
    if raw == "market":
        return OrderType.MARKET
    if raw == "limit":
        return OrderType.LIMIT
    if raw in {"stop", "stop_market", "stop-loss", "stop_loss", "stoploss"}:
        return OrderType.STOP_LOSS
    if raw in {"take_profit", "take-profit", "take_profit_market", "tp"}:
        return OrderType.TAKE_PROFIT
    if "stop" in raw and "limit" in raw:
        return OrderType.STOP_LIMIT
    if "stop" in raw:
        return OrderType.STOP_LOSS
    if "take" in raw and "profit" in raw:
        return OrderType.TAKE_PROFIT
    return OrderType.LIMIT


def parse_stop_price(data: dict[str, Any]) -> float | None:
    """Extract stop/trigger price from CCXT order payload."""
    candidates = (
        data.get("stopPrice"),
        data.get("triggerPrice"),
        (data.get("info", {}) or {}).get("stopPrice"),
        (data.get("info", {}) or {}).get("triggerPrice"),
    )
    for value in candidates:
        try:
            if value is None:
                continue
            parsed = float(value)
            if parsed > 0:
                return parsed
        except (TypeError, ValueError):
            continue
    return None


def infer_position_leverage(raw_position: dict[str, Any]) -> int:
    """Infer leverage when exchange payload omits explicit value."""
    raw_leverage = raw_position.get("leverage")
    try:
        if raw_leverage is not None:
            parsed = round(float(raw_leverage))
            if parsed > 0:
                return parsed
    except (TypeError, ValueError):
        pass

    try:
        margin_pct = float(raw_position.get("initialMarginPercentage", 0) or 0)
        if margin_pct > 0:
            inferred = round(1.0 / margin_pct)
            if inferred > 0:
                return inferred
    except (TypeError, ValueError, ZeroDivisionError):
        pass

    info = raw_position.get("info") or {}
    try:
        initial_margin = float(raw_position.get("initialMargin") or info.get("positionInitialMargin") or 0)
        notional = abs(float(raw_position.get("notional") or info.get("notional") or 0))
        if initial_margin > 0 and notional > 0:
            inferred = round(notional / initial_margin)
            if inferred > 0:
                return inferred
    except (TypeError, ValueError):
        pass

    return 1


def extract_position_level(raw_position: dict[str, Any], keys: tuple[str, ...]) -> float:
    """Extract first positive level value from top-level/info payload keys."""
    info = raw_position.get("info") or {}
    for key in keys:
        val = raw_position.get(key)
        if val is None:
            val = info.get(key)
        try:
            f = float(val or 0)
            if f > 0:
                return f
        except (TypeError, ValueError):
            continue
    return 0.0


def ts_to_dt(ts: Any) -> datetime:
    """Convert a millisecond timestamp (from ccxt) to a timezone-aware datetime."""
    if ts is None:
        return datetime.now(UTC)
    try:
        return datetime.fromtimestamp(float(ts) / 1000, tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return datetime.now(UTC)


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
    def name(self) -> str: ...

    def supports(self, market_type: str) -> bool:
        if not market_type:
            return False
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
    async def fetch_ticker(self, symbol: str, market_type: MarketType = MarketType.SPOT) -> Ticker: ...

    @abstractmethod
    async def fetch_tickers(
        self, symbols: list[str] | None = None, market_type: MarketType = MarketType.SPOT
    ) -> list[Ticker]: ...

    @abstractmethod
    async def fetch_candles(
        self,
        symbol: str,
        timeframe: str = "1m",
        limit: int = 100,
        market_type: MarketType = MarketType.SPOT,
    ) -> list[Candle]: ...

    @abstractmethod
    async def fetch_order_book(
        self, symbol: str, limit: int = 20, market_type: MarketType = MarketType.SPOT
    ) -> OrderBook: ...

    # -- Account --

    @abstractmethod
    async def fetch_balance(self) -> dict[str, float]:
        """Returns {asset: free_balance}."""
        ...

    @abstractmethod
    async def fetch_positions(self, symbol: str | None = None) -> list[Position]: ...

    # -- Trading --

    @abstractmethod
    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        amount: float,
        price: float | None = None,
        stop_price: float | None = None,
        leverage: int = 1,
        market_type: MarketType = MarketType.SPOT,
    ) -> Order: ...

    @abstractmethod
    async def cancel_order(self, order_id: str, symbol: str, market_type: MarketType = MarketType.SPOT) -> Order: ...

    @abstractmethod
    async def fetch_order(self, order_id: str, symbol: str, market_type: MarketType = MarketType.SPOT) -> Order: ...

    @abstractmethod
    async def fetch_open_orders(
        self, symbol: str | None = None, market_type: MarketType = MarketType.SPOT
    ) -> list[Order]: ...

    # -- Futures specific --

    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: int) -> bool: ...

    @abstractmethod
    async def set_margin_mode(self, symbol: str, margin_mode: str) -> bool: ...

    # -- Symbols --

    @abstractmethod
    async def get_available_symbols(self, market_type: MarketType = MarketType.SPOT) -> list[str]: ...

    # -- Stream --

    @abstractmethod
    async def watch_ticker(self, symbol: str, callback: Callable[..., Any]) -> None:
        """Subscribe to real-time ticker updates."""
        ...

    @abstractmethod
    async def watch_candles(self, symbol: str, timeframe: str, callback: Callable[..., Any]) -> None:
        """Subscribe to real-time candle updates."""
        ...
