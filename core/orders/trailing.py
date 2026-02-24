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
    tightened_stop: float = 0.0  # textbook level to tighten to after wick bounce
    wick_bounced: bool = False  # True once price wicked near tightened_stop and recovered
    wick_tighten_enabled: bool = False  # fast wick tighten only for quick/extreme trades
    wick_touched: bool = False  # True once price actually tags the tighten zone
    structure_guard: float = 0.0  # long: max stop, short: min stop (market-structure guard)
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
                be_stop = self._be_with_fee_offset(self.entry_price, long=True)
                if self._move_long_stop(be_stop):
                    logger.info(
                        "BREAK-EVEN locked for {} at {:.2f}% profit (stop -> {:.6f}, entry was {:.6f})",
                        self.symbol,
                        profit_pct,
                        self.current_stop,
                        self.entry_price,
                    )

        # Wick-bounce tighten: requires explicit touch, then reclaim.
        if (
            self.wick_tighten_enabled
            and self.tightened_stop > 0
            and not self.wick_bounced
            and self.tightened_stop > self.current_stop
        ):
            touch_px = self.tightened_stop * (1 + 0.10 / 100)
            reclaim_px = self.tightened_stop * (1 + 0.05 / 100)
            if not self.wick_touched and price <= touch_px:
                self.wick_touched = True
            if self.wick_touched and price >= reclaim_px:
                self.wick_bounced = True
                old = self.current_stop
                tightened = self._be_with_fee_offset(self.tightened_stop, long=True)
                if self._move_long_stop(tightened):
                    logger.info(
                        "WICK BOUNCE tighten {}: {:.6f} -> {:.6f} (price touched {:.6f} then reclaimed)",
                        self.symbol,
                        old,
                        self.current_stop,
                        self.tightened_stop,
                    )

        # Trailing activation
        if not self.activated and profit_pct >= self.activation_pct:
            self.activated = True
            logger.info("Trailing ACTIVATED for {} at {:.2f}% profit", self.symbol, profit_pct)

        if price > self.peak_price:
            self.peak_price = price
            if self.activated:
                new_stop = price * (1 - self.trail_pct / 100)
                old = self.current_stop
                if self._move_long_stop(new_stop):
                    logger.debug(
                        "Trail raised {}: {:.6f} -> {:.6f} (peak: {:.6f})", self.symbol, old, self.current_stop, price
                    )

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
                be_stop = self._be_with_fee_offset(self.entry_price, long=False)
                if self._move_short_stop(be_stop):
                    logger.info(
                        "BREAK-EVEN locked for {} at {:.2f}% profit (stop -> {:.6f}, entry was {:.6f})",
                        self.symbol,
                        profit_pct,
                        self.current_stop,
                        self.entry_price,
                    )

        # Wick-bounce tighten (short): requires explicit touch, then reclaim lower.
        if (
            self.wick_tighten_enabled
            and self.tightened_stop > 0
            and not self.wick_bounced
            and self.tightened_stop < self.current_stop
        ):
            touch_px = self.tightened_stop * (1 - 0.10 / 100)
            reclaim_px = self.tightened_stop * (1 - 0.05 / 100)
            if not self.wick_touched and price >= touch_px:
                self.wick_touched = True
            if self.wick_touched and price <= reclaim_px:
                self.wick_bounced = True
                old = self.current_stop
                tightened = self._be_with_fee_offset(self.tightened_stop, long=False)
                if self._move_short_stop(tightened):
                    logger.info(
                        "WICK BOUNCE tighten {}: {:.6f} -> {:.6f} (price touched {:.6f} then reclaimed)",
                        self.symbol,
                        old,
                        self.current_stop,
                        self.tightened_stop,
                    )

        if not self.activated and profit_pct >= self.activation_pct:
            self.activated = True
            logger.info("Trailing ACTIVATED for {} at {:.2f}% profit", self.symbol, profit_pct)

        if price < self.peak_price:
            self.peak_price = price
            if self.activated:
                new_stop = price * (1 + self.trail_pct / 100)
                old = self.current_stop
                if self._move_short_stop(new_stop):
                    logger.debug(
                        "Trail lowered {}: {:.6f} -> {:.6f} (peak: {:.6f})", self.symbol, old, self.current_stop, price
                    )

        return False

    def _apply_structure_guard(self, candidate: float) -> float:
        if self.structure_guard <= 0:
            return candidate
        if self.side == OrderSide.BUY:
            return min(candidate, self.structure_guard)
        return max(candidate, self.structure_guard)

    def _move_long_stop(self, candidate: float) -> bool:
        guarded = self._apply_structure_guard(candidate)
        if guarded <= self.current_stop:
            return False
        self.current_stop = guarded
        return True

    def _move_short_stop(self, candidate: float) -> bool:
        guarded = self._apply_structure_guard(candidate)
        if guarded >= self.current_stop:
            return False
        self.current_stop = guarded
        return True

    @staticmethod
    def _be_with_fee_offset(entry: float, long: bool) -> float:
        """Nudge the BE stop slightly past entry to cover trading fees.

        Uses the smallest meaningful tick for the price magnitude so we
        don't set the stop at the exact entry price (which would guarantee
        a tiny loss after fees).  For a long: stop goes one tick above
        entry.  For a short: one tick below.
        """
        if entry <= 0:
            return entry
        tick: float
        if entry >= 10000:
            tick = 10.0
        elif entry >= 100:
            tick = 1.0
        elif entry >= 10:
            tick = 0.1
        elif entry >= 1:
            tick = 0.001
        else:
            # Small-cap symbols still need a minimal fee-covering nudge,
            # but never a large % jump that can force an immediate stop-out.
            tick = max(entry * 0.001, 1e-6)
        return entry + tick if long else entry - tick

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
        self._base_trail_pct = default_trail_pct
        self._base_breakeven_pct = breakeven_pct
        self.activation_pct = activation_pct
        self.breakeven_pct = breakeven_pct
        self._stops: dict[str, TrailingStop] = {}
        self._profit_taking_aggression: float = 1.0

    def set_profit_taking_mode(self, aggression: float) -> None:
        """Adjust trail tightness based on daily profit status.

        aggression > 1.0: not yet secured daily target — tighter trails,
                          lower BE trigger (take profits eagerly).
        aggression == 1.0: in the daily target zone — normal behavior.
        aggression < 1.0: daily target secured — wider trails, higher BE
                          trigger (let winners run).
        """
        if aggression == self._profit_taking_aggression:
            return
        old = self._profit_taking_aggression
        self._profit_taking_aggression = aggression

        if aggression > 1.0:
            self.default_trail_pct = max(0.3, self._base_trail_pct * 0.6)
            self.breakeven_pct = max(2.0, self._base_breakeven_pct * 0.6)
        elif aggression < 1.0:
            self.default_trail_pct = self._base_trail_pct * 1.5
            self.breakeven_pct = self._base_breakeven_pct * 1.5
        else:
            self.default_trail_pct = self._base_trail_pct
            self.breakeven_pct = self._base_breakeven_pct

        logger.info(
            "Profit-taking mode: {:.1f}x -> {:.1f}x | trail: {:.2f}% | BE trigger: {:.1f}%",
            old,
            aggression,
            self.default_trail_pct,
            self.breakeven_pct,
        )

    def register(
        self,
        position: Position,
        initial_stop_pct: float | None = None,
        trail_pct: float | None = None,
        low_liquidity: bool = False,
        key: str | None = None,
        tightened_stop: float = 0.0,
        wick_tighten_enabled: bool = False,
    ) -> TrailingStop:
        ts = TrailingStop(
            symbol=position.symbol,
            side=position.side,
            entry_price=position.entry_price,
            initial_stop_pct=initial_stop_pct or self.default_initial_pct,
            trail_pct=trail_pct or self.default_trail_pct,
            activation_pct=self.activation_pct,
            tightened_stop=tightened_stop,
            wick_tighten_enabled=wick_tighten_enabled,
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
        active_symbols = {p.symbol for p in positions if p.amount > 0}
        for key, ts in list(self._stops.items()):
            price = price_map.get(ts.symbol)
            if price is None:
                # If this is a sub-position stop and the main position is gone,
                # trigger the stop so the sub-position gets closed
                if ":" in key:
                    base_sym = key.rsplit(":", 1)[0]
                    if base_sym not in active_symbols:
                        logger.warning("Main position gone for {} — triggering sub-stop {}", base_sym, key)
                        stopped.append(key)
                continue
            if ts.update(price):
                stopped.append(key)
        return stopped

    def remove(self, symbol: str) -> None:
        self._stops.pop(symbol, None)

    def get(self, symbol: str) -> TrailingStop | None:
        return self._stops.get(symbol)

    def set_structure_guard(self, symbol: str, level: float) -> None:
        ts = self._stops.get(symbol)
        if ts:
            ts.structure_guard = level if level > 0 else 0.0

    @property
    def active_stops(self) -> dict[str, TrailingStop]:
        return dict(self._stops)
