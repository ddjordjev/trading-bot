from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from loguru import logger
from pydantic import BaseModel, Field

from core.models import OrderSide, Position


class TrailingStop(BaseModel):
    """Tracks a trailing stop for an open position.

    The stop ratchets in the profitable direction and never moves backward.
    Once price retraces past the stop, the position should be closed.
    """

    symbol: str
    side: OrderSide
    entry_price: float
    initial_stop_pct: float  # distance from entry as %
    trail_pct: float         # trailing distance as % from peak
    peak_price: float = 0.0  # best price seen so far
    current_stop: float = 0.0
    activated: bool = False  # trailing only activates once in profit
    activation_pct: float = 0.5  # min profit % to start trailing
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def model_post_init(self, __context: object) -> None:
        if self.peak_price == 0:
            self.peak_price = self.entry_price
        if self.current_stop == 0:
            if self.side == OrderSide.BUY:
                self.current_stop = self.entry_price * (1 - self.initial_stop_pct / 100)
            else:
                self.current_stop = self.entry_price * (1 + self.initial_stop_pct / 100)

    def update(self, current_price: float) -> bool:
        """Update with latest price. Returns True if stop was hit."""

        if self.side == OrderSide.BUY:
            return self._update_long(current_price)
        return self._update_short(current_price)

    def _update_long(self, price: float) -> bool:
        if price <= self.current_stop:
            logger.info("Trailing stop HIT for {} (long) at {:.6f} (stop was {:.6f})",
                        self.symbol, price, self.current_stop)
            return True

        profit_pct = (price - self.entry_price) / self.entry_price * 100
        if not self.activated and profit_pct >= self.activation_pct:
            self.activated = True
            logger.info("Trailing stop ACTIVATED for {} at {:.2f}% profit", self.symbol, profit_pct)

        if price > self.peak_price:
            self.peak_price = price
            if self.activated:
                new_stop = price * (1 - self.trail_pct / 100)
                if new_stop > self.current_stop:
                    old = self.current_stop
                    self.current_stop = new_stop
                    logger.debug("Trail raised for {}: {:.6f} -> {:.6f} (peak: {:.6f})",
                                 self.symbol, old, new_stop, price)

        return False

    def _update_short(self, price: float) -> bool:
        if price >= self.current_stop:
            logger.info("Trailing stop HIT for {} (short) at {:.6f} (stop was {:.6f})",
                        self.symbol, price, self.current_stop)
            return True

        profit_pct = (self.entry_price - price) / self.entry_price * 100
        if not self.activated and profit_pct >= self.activation_pct:
            self.activated = True
            logger.info("Trailing stop ACTIVATED for {} at {:.2f}% profit", self.symbol, profit_pct)

        if price < self.peak_price:
            self.peak_price = price
            if self.activated:
                new_stop = price * (1 + self.trail_pct / 100)
                if new_stop < self.current_stop:
                    old = self.current_stop
                    self.current_stop = new_stop
                    logger.debug("Trail lowered for {}: {:.6f} -> {:.6f} (peak: {:.6f})",
                                 self.symbol, old, new_stop, price)

        return False

    @property
    def pnl_from_stop(self) -> float:
        """If stopped out now, what % PnL would result."""
        if self.side == OrderSide.BUY:
            return (self.current_stop - self.entry_price) / self.entry_price * 100
        return (self.entry_price - self.current_stop) / self.entry_price * 100


class TrailingStopManager:
    """Manages trailing stops for all open positions."""

    def __init__(self, default_initial_pct: float = 2.0, default_trail_pct: float = 1.0,
                 activation_pct: float = 0.5):
        self.default_initial_pct = default_initial_pct
        self.default_trail_pct = default_trail_pct
        self.activation_pct = activation_pct
        self._stops: dict[str, TrailingStop] = {}

    def register(self, position: Position, initial_stop_pct: Optional[float] = None,
                 trail_pct: Optional[float] = None) -> TrailingStop:
        ts = TrailingStop(
            symbol=position.symbol,
            side=position.side,
            entry_price=position.entry_price,
            initial_stop_pct=initial_stop_pct or self.default_initial_pct,
            trail_pct=trail_pct or self.default_trail_pct,
            activation_pct=self.activation_pct,
        )
        self._stops[position.symbol] = ts
        logger.info("Trailing stop registered for {} - initial stop: {:.6f}, trail: {:.1f}%",
                     position.symbol, ts.current_stop, ts.trail_pct)
        return ts

    def update_all(self, positions: list[Position]) -> list[str]:
        """Update all trailing stops. Returns symbols that were stopped out."""
        stopped: list[str] = []
        for pos in positions:
            ts = self._stops.get(pos.symbol)
            if not ts:
                continue
            hit = ts.update(pos.current_price)
            if hit:
                stopped.append(pos.symbol)
        return stopped

    def remove(self, symbol: str) -> None:
        self._stops.pop(symbol, None)

    def get(self, symbol: str) -> Optional[TrailingStop]:
        return self._stops.get(symbol)

    @property
    def active_stops(self) -> dict[str, TrailingStop]:
        return dict(self._stops)
