from __future__ import annotations

from datetime import UTC, datetime

from loguru import logger
from pydantic import BaseModel, Field

from core.models import OrderSide, Position


class TrailingStop(BaseModel):
    """Tracks a trailing stop for an open position.

    Key behaviors:
    - Initial stop: hard stop from entry (cut the loser fast)
    - Break-even lock: once profit hits breakeven_trigger_pct, stop moves to entry price
    - Trailing activation: once profit hits activation_pct, stop follows peak price
    - Stop never moves backward

    On low-liquidity coins, MEXC may skip stop-loss execution on fast wicks.
    For those, we use tighter initial stops and rely on our own polling to
    close via market order rather than trusting exchange stop orders.
    """

    symbol: str
    side: OrderSide
    entry_price: float
    initial_stop_pct: float
    trail_pct: float
    peak_price: float = 0.0
    current_stop: float = 0.0
    activated: bool = False
    breakeven_locked: bool = False
    activation_pct: float = 0.5
    breakeven_trigger_pct: float = 5.0  # move stop to entry once at +5%
    low_liquidity: bool = False  # if True, we manage stop ourselves (no exchange SL)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

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
        if current_price <= 0:
            return False
        if self.side == OrderSide.BUY:
            return self._update_long(current_price)
        return self._update_short(current_price)

    def _update_long(self, price: float) -> bool:
        if self.entry_price <= 0:
            return False
        if price <= self.current_stop:
            logger.info(
                "Stop HIT for {} (long) at {:.6f} (stop={:.6f}, locked_be={}, trail={})",
                self.symbol,
                price,
                self.current_stop,
                self.breakeven_locked,
                self.activated,
            )
            return True

        profit_pct = (price - self.entry_price) / self.entry_price * 100
        distance_to_stop = (price - self.current_stop) / price * 100

        logger.debug(
            "Trail {}: price={:.6f} pnl={:+.2f}% stop={:.6f} dist={:.2f}% be={} trail={} peak={:.6f}",
            self.symbol,
            price,
            profit_pct,
            self.current_stop,
            distance_to_stop,
            self.breakeven_locked,
            self.activated,
            self.peak_price,
        )

        if not self.breakeven_locked and profit_pct >= self.breakeven_trigger_pct:
            self.breakeven_locked = True
            if self.entry_price > self.current_stop:
                self.current_stop = self.entry_price
                logger.info(
                    "BREAK-EVEN locked for {} at {:.2f}% profit (stop -> {:.6f})",
                    self.symbol,
                    profit_pct,
                    self.entry_price,
                )

        # Trailing activation
        if not self.activated and profit_pct >= self.activation_pct:
            self.activated = True
            logger.info("Trailing ACTIVATED for {} at {:.2f}% profit", self.symbol, profit_pct)

        if price > self.peak_price:
            self.peak_price = price
            if self.activated:
                new_stop = price * (1 - self.trail_pct / 100)
                if new_stop > self.current_stop:
                    old = self.current_stop
                    self.current_stop = new_stop
                    logger.debug("Trail raised {}: {:.6f} -> {:.6f} (peak: {:.6f})", self.symbol, old, new_stop, price)

        return False

    def _update_short(self, price: float) -> bool:
        if self.entry_price <= 0:
            return False
        if price >= self.current_stop:
            logger.info(
                "Stop HIT for {} (short) at {:.6f} (stop={:.6f}, locked_be={}, trail={})",
                self.symbol,
                price,
                self.current_stop,
                self.breakeven_locked,
                self.activated,
            )
            return True

        profit_pct = (self.entry_price - price) / self.entry_price * 100
        distance_to_stop = (self.current_stop - price) / price * 100

        logger.debug(
            "Trail {}: price={:.6f} pnl={:+.2f}% stop={:.6f} dist={:.2f}% be={} trail={} peak={:.6f}",
            self.symbol,
            price,
            profit_pct,
            self.current_stop,
            distance_to_stop,
            self.breakeven_locked,
            self.activated,
            self.peak_price,
        )

        if not self.breakeven_locked and profit_pct >= self.breakeven_trigger_pct:
            self.breakeven_locked = True
            if self.entry_price < self.current_stop:
                self.current_stop = self.entry_price
                logger.info(
                    "BREAK-EVEN locked for {} at {:.2f}% profit (stop -> {:.6f})",
                    self.symbol,
                    profit_pct,
                    self.entry_price,
                )

        if not self.activated and profit_pct >= self.activation_pct:
            self.activated = True
            logger.info("Trailing ACTIVATED for {} at {:.2f}% profit", self.symbol, profit_pct)

        if price < self.peak_price:
            self.peak_price = price
            if self.activated:
                new_stop = price * (1 + self.trail_pct / 100)
                if new_stop < self.current_stop:
                    old = self.current_stop
                    self.current_stop = new_stop
                    logger.debug("Trail lowered {}: {:.6f} -> {:.6f} (peak: {:.6f})", self.symbol, old, new_stop, price)

        return False

    @property
    def pnl_from_stop(self) -> float:
        if self.entry_price <= 0:
            return 0.0
        if self.side == OrderSide.BUY:
            return (self.current_stop - self.entry_price) / self.entry_price * 100
        return (self.entry_price - self.current_stop) / self.entry_price * 100


class TrailingStopManager:
    """Manages trailing stops for all open positions."""

    def __init__(
        self,
        default_initial_pct: float = 1.5,
        default_trail_pct: float = 0.5,
        activation_pct: float = 0.5,
        breakeven_pct: float = 5.0,
    ):
        self.default_initial_pct = default_initial_pct
        self.default_trail_pct = default_trail_pct
        self.activation_pct = activation_pct
        self.breakeven_pct = breakeven_pct
        self._stops: dict[str, TrailingStop] = {}

    def register(
        self,
        position: Position,
        initial_stop_pct: float | None = None,
        trail_pct: float | None = None,
        low_liquidity: bool = False,
        key: str | None = None,
    ) -> TrailingStop:
        ts = TrailingStop(
            symbol=position.symbol,
            side=position.side,
            entry_price=position.entry_price,
            initial_stop_pct=initial_stop_pct or self.default_initial_pct,
            trail_pct=trail_pct or self.default_trail_pct,
            activation_pct=self.activation_pct,
            breakeven_trigger_pct=self.breakeven_pct,
            low_liquidity=low_liquidity,
        )
        stop_key = key or position.symbol
        self._stops[stop_key] = ts
        liq_tag = " [LOW-LIQ]" if low_liquidity else ""
        logger.info(
            "Stop registered for {}{} - initial: {:.6f}, BE at +{:.0f}%, trail: {:.1f}%",
            stop_key,
            liq_tag,
            ts.current_stop,
            self.breakeven_pct,
            ts.trail_pct,
        )
        return ts

    def update_all(self, positions: list[Position]) -> list[str]:
        """Update all stops with latest prices. Returns keys of stopped positions.

        Keys may be plain symbols ("BTC/USDT") or suffixed
        ("BTC/USDT:hedge", "BTC/USDT:wick") for sub-position stops.
        """
        stopped: list[str] = []
        price_map = {p.symbol: p.current_price for p in positions}
        for key, ts in list(self._stops.items()):
            price = price_map.get(ts.symbol)
            if price is None:
                continue
            if ts.update(price):
                stopped.append(key)
        return stopped

    def remove(self, symbol: str) -> None:
        self._stops.pop(symbol, None)

    def get(self, symbol: str) -> TrailingStop | None:
        return self._stops.get(symbol)

    @property
    def active_stops(self) -> dict[str, TrailingStop]:
        return dict(self._stops)
