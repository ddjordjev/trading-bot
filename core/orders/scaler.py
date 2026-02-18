from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from loguru import logger
from pydantic import BaseModel, Field


class ScalePhase(str, Enum):
    INITIAL = "initial"
    CONFIRMING = "confirming"
    ADDING = "adding"
    FULL = "full"
    GAMBLING = "gambling"


class ScaleMode(str, Enum):
    WINNERS = "winners"     # add to winning positions only (default scalp mode)
    PYRAMID = "pyramid"     # DCA down: buy dips, average down, lever up on recovery


class ScaledPosition(BaseModel):
    """Tracks a position being built up in stages.

    Two modes:

    WINNERS mode (default for scalps):
        Start small -> add when in profit -> ride with trail

    PYRAMID mode (DCA / conviction entries):
        Start tiny with low leverage -> let it go red ->
        add more at lower prices to improve avg entry ->
        when it recovers above avg entry, raise leverage + lock break-even ->
        take partial profit to pull capital out -> ride the rest

    """

    symbol: str
    side: str  # "long" or "short"
    strategy: str
    market_type: str = "futures"
    mode: ScaleMode = ScaleMode.WINNERS

    # Leverage management for PYRAMID mode
    initial_leverage: int = 1      # start low/no leverage
    target_leverage: int = 10      # ramp up to this when in profit
    current_leverage: int = 1
    leverage_raised: bool = False

    # Common fields
    intended_size: float = 0.0
    current_size: float = 0.0
    avg_entry_price: float = 0.0
    phase: ScalePhase = ScalePhase.INITIAL
    adds: int = 0
    max_adds: int = 3
    low_liquidity: bool = False

    initial_pct: float = 0.30
    add_pct: float = 0.35

    # WINNERS mode conditions
    min_profit_to_add_pct: float = 1.0
    pullback_tolerance_pct: float = 0.5

    # PYRAMID mode conditions
    dca_interval_pct: float = 2.0   # add every 2% the price drops
    dca_multiplier: float = 1.5     # each DCA add is 1.5x the previous
    profit_to_lever_up_pct: float = 1.0  # raise leverage once avg entry is +1% in profit
    partial_take_pct: float = 30.0  # take 30% off the table when levering up
    breakeven_after_lever: bool = True  # lock break-even after leverage raise

    last_add_price: float = 0.0
    peak_since_entry: float = 0.0
    trough_since_entry: float = 0.0  # worst price seen (for PYRAMID)
    partial_taken: bool = False
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
        if self.low_liquidity:
            return self.intended_size * 0.15
        if self.mode == ScaleMode.PYRAMID:
            return self.intended_size * 0.15  # even smaller for DCA
        return self.intended_size * self.initial_pct

    def get_add_amount(self) -> float:
        remaining = self.remaining_size
        if self.mode == ScaleMode.PYRAMID:
            base = self.intended_size * 0.15
            add = base * (self.dca_multiplier ** self.adds)
            return min(add, remaining)
        add = self.intended_size * self.add_pct
        return min(add, remaining)

    def should_add(self, current_price: float) -> bool:
        if self.phase in (ScalePhase.FULL, ScalePhase.GAMBLING):
            return False
        if self.adds >= self.max_adds:
            return False
        if self.remaining_size <= 0:
            return False

        if self.mode == ScaleMode.PYRAMID:
            return self._should_add_pyramid(current_price)
        return self._should_add_winners(current_price)

    def _should_add_winners(self, current_price: float) -> bool:
        profit_pct = self._current_profit_pct(current_price)
        if profit_pct < self.min_profit_to_add_pct:
            return False

        if self.peak_since_entry > 0:
            peak_profit = self._profit_at_price(self.peak_since_entry)
            if peak_profit - profit_pct > self.pullback_tolerance_pct:
                return False

        if self.last_add_price > 0:
            dist = abs(current_price - self.last_add_price) / self.last_add_price * 100
            if dist < self.min_profit_to_add_pct * 0.5:
                return False

        return True

    def _should_add_pyramid(self, current_price: float) -> bool:
        """Add when price has dropped another dca_interval_pct from last add."""
        if self.last_add_price == 0:
            return False

        if self.side == "long":
            drop_pct = (self.last_add_price - current_price) / self.last_add_price * 100
        else:
            drop_pct = (current_price - self.last_add_price) / self.last_add_price * 100

        return drop_pct >= self.dca_interval_pct

    def should_lever_up(self, current_price: float) -> bool:
        """PYRAMID mode: check if we should raise leverage now."""
        if self.mode != ScaleMode.PYRAMID:
            return False
        if self.leverage_raised:
            return False
        if self.adds < 1:
            return False

        profit_pct = self._current_profit_pct(current_price)
        return profit_pct >= self.profit_to_lever_up_pct

    def should_take_partial(self, current_price: float) -> bool:
        """PYRAMID mode: take profit on a portion once leveraged up."""
        if self.mode != ScaleMode.PYRAMID:
            return False
        if not self.leverage_raised:
            return False
        if self.partial_taken:
            return False

        profit_pct = self._current_profit_pct(current_price)
        return profit_pct >= self.profit_to_lever_up_pct * 2

    def get_partial_take_amount(self) -> float:
        return self.current_size * (self.partial_take_pct / 100)

    def record_add(self, amount: float, price: float) -> None:
        total_cost = self.avg_entry_price * self.current_size + price * amount
        self.current_size += amount
        self.avg_entry_price = total_cost / self.current_size if self.current_size > 0 else price
        self.last_add_price = price
        self.adds += 1

        if self.current_size >= self.intended_size * 0.95:
            self.phase = ScalePhase.FULL
        else:
            self.phase = ScalePhase.ADDING

        mode_tag = f" [{self.mode.value}]"
        logger.info("Scaled into {} - add #{} | size: {:.4f}/{:.4f} ({:.0f}%) | avg: {:.6f}{}",
                     self.symbol, self.adds, self.current_size, self.intended_size,
                     self.fill_pct, self.avg_entry_price, mode_tag)

    def record_partial_close(self, amount: float) -> None:
        self.current_size = max(0, self.current_size - amount)
        self.partial_taken = True
        logger.info("Partial close on {} | removed {:.4f} | remaining: {:.4f}",
                     self.symbol, amount, self.current_size)

    def record_lever_up(self, new_leverage: int) -> None:
        self.current_leverage = new_leverage
        self.leverage_raised = True
        logger.info("LEVERAGE RAISED on {} | {} -> {}x", self.symbol, self.initial_leverage, new_leverage)

    def update_peak(self, current_price: float) -> None:
        if self.side == "long":
            if current_price > self.peak_since_entry:
                self.peak_since_entry = current_price
            if self.trough_since_entry == 0 or current_price < self.trough_since_entry:
                self.trough_since_entry = current_price
        else:
            if self.peak_since_entry == 0 or current_price < self.peak_since_entry:
                self.peak_since_entry = current_price
            if current_price > self.trough_since_entry:
                self.trough_since_entry = current_price

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

    def status_line(self) -> str:
        return (
            f"{self.symbol} [{self.mode.value}] phase={self.phase.value} "
            f"fill={self.fill_pct:.0f}% adds={self.adds} "
            f"avg={self.avg_entry_price:.6f} lev={self.current_leverage}x "
            f"lev_raised={self.leverage_raised} partial={self.partial_taken} "
            f"low_liq={self.low_liquidity}"
        )


class PositionScaler:
    """Manages scaled entries across all positions (both WINNERS and PYRAMID modes)."""

    def __init__(self, gambling_budget_pct: float = 2.0):
        self.gambling_budget_pct = gambling_budget_pct
        self._positions: dict[str, ScaledPosition] = {}

    def create(self, symbol: str, side: str, intended_size: float, strategy: str,
               market_type: str = "futures", leverage: int = 10,
               low_liquidity: bool = False,
               mode: ScaleMode = ScaleMode.WINNERS,
               dca_interval_pct: float = 2.0,
               dca_multiplier: float = 1.5) -> ScaledPosition:

        if mode == ScaleMode.PYRAMID:
            init_lev = max(1, leverage // 5)  # start at 1/5th of target leverage
            sp = ScaledPosition(
                symbol=symbol, side=side, strategy=strategy,
                market_type=market_type,
                mode=ScaleMode.PYRAMID,
                initial_leverage=init_lev,
                target_leverage=leverage,
                current_leverage=init_lev,
                intended_size=intended_size,
                low_liquidity=low_liquidity,
                max_adds=5,  # more adds allowed for DCA
                dca_interval_pct=dca_interval_pct,
                dca_multiplier=dca_multiplier,
            )
        else:
            sp = ScaledPosition(
                symbol=symbol, side=side, strategy=strategy,
                market_type=market_type, mode=ScaleMode.WINNERS,
                initial_leverage=leverage, target_leverage=leverage,
                current_leverage=leverage,
                intended_size=intended_size, low_liquidity=low_liquidity,
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

    def get_symbols_to_lever_up(self, prices: dict[str, float]) -> list[str]:
        """Return symbols where PYRAMID positions are ready for leverage raise."""
        result: list[str] = []
        for sym, sp in self._positions.items():
            price = prices.get(sym, 0)
            if price > 0 and sp.should_lever_up(price):
                result.append(sym)
        return result

    def get_symbols_for_partial_take(self, prices: dict[str, float]) -> list[tuple[str, float]]:
        """Return (symbol, amount_to_close) for PYRAMID positions ready for partial profit."""
        result: list[tuple[str, float]] = []
        for sym, sp in self._positions.items():
            price = prices.get(sym, 0)
            if price > 0 and sp.should_take_partial(price):
                amount = sp.get_partial_take_amount()
                if amount > 0:
                    result.append((sym, amount))
        return result

    @property
    def active_positions(self) -> dict[str, ScaledPosition]:
        return dict(self._positions)

    def gambling_size(self, balance: float, price: float, leverage: int = 10) -> float:
        capital = balance * (self.gambling_budget_pct / 100)
        notional = capital * leverage
        if price == 0:
            return 0
        return notional / price
