from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from loguru import logger
from pydantic import BaseModel, Field


class ScalePhase(str, Enum):
    INITIAL = "initial"       # first entry (small)
    CONFIRMING = "confirming" # watching for pullback / continuation
    ADDING = "adding"         # scaling in more
    FULL = "full"             # max position reached
    GAMBLING = "gambling"     # yolo pocket money bet (low-liq)


class ScaledPosition(BaseModel):
    """Tracks a position being built up in stages.

    Rule: ALWAYS start small and add to winners. Never go all-in on entry.

    Flow:
    1. INITIAL: Enter with 30% of intended size
    2. CONFIRMING: Wait for price to hold / pull back
    3. ADDING: If it continues, add another 30-40%
    4. FULL: If still running, add remaining (up to max)

    Each add raises the average entry but also raises the stop to lock profit.
    """

    symbol: str
    side: str  # "long" or "short"
    strategy: str
    market_type: str = "futures"
    leverage: int = 10

    intended_size: float = 0.0  # full position size in base currency
    current_size: float = 0.0
    avg_entry_price: float = 0.0
    phase: ScalePhase = ScalePhase.INITIAL
    adds: int = 0
    max_adds: int = 3
    low_liquidity: bool = False

    initial_pct: float = 0.30   # start with 30% of intended
    add_pct: float = 0.35       # each add is 35%

    # Conditions to add
    min_profit_to_add_pct: float = 1.0   # must be at least +1% before adding
    pullback_tolerance_pct: float = 0.5  # allow 0.5% pullback from peak

    last_add_price: float = 0.0
    peak_since_entry: float = 0.0
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def fill_pct(self) -> float:
        if self.intended_size == 0:
            return 0
        return self.current_size / self.intended_size * 100

    @property
    def remaining_size(self) -> float:
        return max(0, self.intended_size - self.current_size)

    def get_initial_amount(self) -> float:
        """Size for the first entry."""
        if self.low_liquidity:
            return self.intended_size * 0.15  # tiny for shitcoins
        return self.intended_size * self.initial_pct

    def get_add_amount(self) -> float:
        """Size for each subsequent add."""
        remaining = self.remaining_size
        add = self.intended_size * self.add_pct
        return min(add, remaining)

    def should_add(self, current_price: float) -> bool:
        """Check if conditions are met to add to the position."""
        if self.phase in (ScalePhase.FULL, ScalePhase.GAMBLING):
            return False
        if self.adds >= self.max_adds:
            return False
        if self.remaining_size <= 0:
            return False

        # Must be in profit before adding
        profit_pct = self._current_profit_pct(current_price)
        if profit_pct < self.min_profit_to_add_pct:
            return False

        # Don't add during a pullback that's too deep
        if self.peak_since_entry > 0:
            peak_profit = self._profit_at_price(self.peak_since_entry)
            if peak_profit - profit_pct > self.pullback_tolerance_pct:
                return False

        # Don't add too close to last add price
        if self.last_add_price > 0:
            dist = abs(current_price - self.last_add_price) / self.last_add_price * 100
            if dist < self.min_profit_to_add_pct * 0.5:
                return False

        return True

    def record_add(self, amount: float, price: float) -> None:
        """Record a successful position add."""
        total_cost = self.avg_entry_price * self.current_size + price * amount
        self.current_size += amount
        self.avg_entry_price = total_cost / self.current_size if self.current_size > 0 else price
        self.last_add_price = price
        self.adds += 1

        if self.current_size >= self.intended_size * 0.95:
            self.phase = ScalePhase.FULL
        else:
            self.phase = ScalePhase.ADDING

        logger.info("Scaled into {} - add #{} | size: {:.4f}/{:.4f} ({:.0f}%) | avg: {:.6f}",
                     self.symbol, self.adds, self.current_size, self.intended_size,
                     self.fill_pct, self.avg_entry_price)

    def update_peak(self, current_price: float) -> None:
        if self.side == "long" and current_price > self.peak_since_entry:
            self.peak_since_entry = current_price
        elif self.side == "short" and (self.peak_since_entry == 0 or current_price < self.peak_since_entry):
            self.peak_since_entry = current_price

    def _current_profit_pct(self, price: float) -> float:
        if self.avg_entry_price == 0:
            return 0
        if self.side == "long":
            return (price - self.avg_entry_price) / self.avg_entry_price * 100
        return (self.avg_entry_price - price) / self.avg_entry_price * 100

    def _profit_at_price(self, price: float) -> float:
        if self.avg_entry_price == 0:
            return 0
        if self.side == "long":
            return (price - self.avg_entry_price) / self.avg_entry_price * 100
        return (self.avg_entry_price - price) / self.avg_entry_price * 100


class PositionScaler:
    """Manages scaled entries across all positions."""

    def __init__(self, gambling_budget_pct: float = 2.0):
        self.gambling_budget_pct = gambling_budget_pct
        self._positions: dict[str, ScaledPosition] = {}

    def create(self, symbol: str, side: str, intended_size: float, strategy: str,
               market_type: str = "futures", leverage: int = 10,
               low_liquidity: bool = False) -> ScaledPosition:
        sp = ScaledPosition(
            symbol=symbol,
            side=side,
            strategy=strategy,
            market_type=market_type,
            leverage=leverage,
            intended_size=intended_size,
            low_liquidity=low_liquidity,
            phase=ScalePhase.GAMBLING if low_liquidity else ScalePhase.INITIAL,
            max_adds=1 if low_liquidity else 3,
        )
        self._positions[symbol] = sp
        return sp

    def get(self, symbol: str) -> Optional[ScaledPosition]:
        return self._positions.get(symbol)

    def remove(self, symbol: str) -> None:
        self._positions.pop(symbol, None)

    def get_symbols_to_add(self, prices: dict[str, float]) -> list[tuple[str, float]]:
        """Check all positions and return (symbol, add_amount) for those ready to add."""
        to_add: list[tuple[str, float]] = []
        for sym, sp in self._positions.items():
            price = prices.get(sym, 0)
            if price <= 0:
                continue
            sp.update_peak(price)
            if sp.should_add(price):
                amount = sp.get_add_amount()
                if amount > 0:
                    to_add.append((sym, amount))
        return to_add

    @property
    def active_positions(self) -> dict[str, ScaledPosition]:
        return dict(self._positions)

    def gambling_size(self, balance: float, price: float, leverage: int = 10) -> float:
        """Tiny position size for low-liq yolo bets."""
        capital = balance * (self.gambling_budget_pct / 100)
        notional = capital * leverage
        if price == 0:
            return 0
        return notional / price
