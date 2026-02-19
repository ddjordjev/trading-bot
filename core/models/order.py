from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    STOP_LIMIT = "stop_limit"


class OrderStatus(str, Enum):
    PENDING = "pending"
    OPEN = "open"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    FAILED = "failed"


class Order(BaseModel):
    id: str = ""
    symbol: str
    side: OrderSide
    order_type: OrderType
    amount: float
    price: Optional[float] = None
    stop_price: Optional[float] = None
    status: OrderStatus = OrderStatus.PENDING
    filled: float = 0.0
    average_price: float = 0.0
    leverage: int = 1
    market_type: str = "spot"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    strategy: str = ""

    @property
    def remaining(self) -> float:
        return self.amount - self.filled

    @property
    def is_complete(self) -> bool:
        return self.status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.FAILED)


class Position(BaseModel):
    symbol: str
    side: OrderSide
    amount: float
    entry_price: float
    current_price: float = 0.0
    leverage: int = 1
    market_type: str = "spot"
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    opened_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    strategy: str = ""

    @property
    def notional_value(self) -> float:
        return self.amount * self.current_price

    @property
    def pnl_pct(self) -> float:
        if self.entry_price == 0:
            return 0.0
        if self.side == OrderSide.BUY:
            return (self.current_price - self.entry_price) / self.entry_price * 100 * self.leverage
        return (self.entry_price - self.current_price) / self.entry_price * 100 * self.leverage
