from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class SignalAction(str, Enum):
    BUY = "buy"
    SELL = "sell"
    CLOSE = "close"
    HOLD = "hold"


class Signal(BaseModel):
    symbol: str
    action: SignalAction
    strength: float = 0.0  # 0.0 to 1.0
    strategy: str = ""
    reason: str = ""
    suggested_price: Optional[float] = None
    suggested_stop_loss: Optional[float] = None
    suggested_take_profit: Optional[float] = None
    market_type: str = "spot"
    leverage: int = 1
    quick_trade: bool = False  # for spike/volatility in-and-out trades
    max_hold_minutes: Optional[int] = None  # auto-close after N minutes
    timestamp: datetime = Field(default_factory=datetime.utcnow)
