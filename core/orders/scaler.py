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
    WINNERS = "winners"     # add to winning positions only (rare, ultra-short scalps)
    PYRAMID = "pyramid"     # DCA down: buy dips, average down, lever up on recovery (DEFAULT)


class ScaledPosition(BaseModel):
    """Tracks a position being built up in stages.

    Philosophy: risk $50 to start. Nobody can predict exact bottoms.
    Market makers wick through expected support to grab liquidity.
    Instead of getting stopped out by the wick, we DCA into it.
    Stop adding once the leveraged (notional) position hits $100K.

    PYRAMID mode (DEFAULT for all strategies):
        Start with $50 at low leverage -> let it go red -> DCA into wicks ->
        avg entry improves -> price recovers -> raise leverage + lock break-even ->
        take partial profit to pull capital out -> ride the rest -> cap at $100K

    WINNERS mode (rare, for ultra-short scalps only):
        Start with $50 -> add when in profit -> ride with trail -> cap at $100K

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

    # Position sizing: fixed dollar amounts, not percentages
    initial_risk_amount: float = 50.0     # $50 first entry
    max_notional: float = 100_000.0       # stop adding at $100K leveraged value
    current_size: float = 0.0             # quantity of asset held
    avg_entry_price: float = 0.0
    phase: ScalePhase = ScalePhase.INITIAL
    adds: int = 0
    low_liquidity: bool = False

    # WINNERS mode conditions
    min_profit_to_add_pct: float = 1.0
    pullback_tolerance_pct: float = 0.5

    # PYRAMID mode conditions
    dca_interval_pct: float = 2.0   # add every 2% the price drops
    dca_multiplier: float = 1.5     # each DCA add is 1.5x the previous
    profit_to_lever_up_pct: float = 1.0
    partial_take_pct: float = 30.0
    breakeven_after_lever: bool = True

    last_add_price: float = 0.0
    peak_since_entry: float = 0.0
    trough_since_entry: float = 0.0
    partial_taken: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def notional_value(self) -> float:
        return self.current_size * self.avg_entry_price * self.current_leverage

    @property
    def notional_at_price(self) -> float:
        """Notional using last_add_price (more current than avg_entry)."""
        price = self.last_add_price or self.avg_entry_price
        return self.current_size * price * self.current_leverage

    @property
    def has_room_to_add(self) -> bool:
        return self.notional_value < self.max_notional

    @property
    def fill_pct(self) -> float:
        if self.max_notional == 0:
            return 0
        return min(100, self.notional_value / self.max_notional * 100)

    def get_initial_amount(self, price: float) -> float:
        """Fixed dollar initial entry -> convert to asset quantity."""
        if price <= 0:
            return 0.0
        dollars = self.initial_risk_amount
        if self.low_liquidity:
            dollars *= 0.3  # even smaller for gambling
        notional = dollars * self.current_leverage
        return notional / price

    def get_add_amount(self, price: float) -> float:
        """Each add = same as initial, scaled up slightly with each successful add."""
        if price <= 0:
            return 0.0

        room_notional = self.max_notional - self.notional_value
        if room_notional <= 0:
            return 0.0

        if self.mode == ScaleMode.PYRAMID:
            base_dollars = self.initial_risk_amount
            add_dollars = base_dollars * (self.dca_multiplier ** self.adds)
        else:
            add_dollars = self.initial_risk_amount * (1 + self.adds * 0.5)

        add_notional = add_dollars * self.current_leverage
        add_notional = min(add_notional, room_notional)
        return add_notional / price

    def should_add(self, current_price: float) -> bool:
        if self.phase == ScalePhase.GAMBLING:
            logger.debug("Scale {}: skip add — gambling phase (no adds)", self.symbol)
            return False
        if not self.has_room_to_add:
            if self.phase != ScalePhase.FULL:
                self.phase = ScalePhase.FULL
                logger.info("{} hit notional cap ${:.0f} -- no more adds",
                            self.symbol, self.max_notional)
            return False

        if self.mode == ScaleMode.PYRAMID:
            return self._should_add_pyramid(current_price)
        return self._should_add_winners(current_price)

    def _should_add_winners(self, current_price: float) -> bool:
        profit_pct = self._current_profit_pct(current_price)
        if profit_pct < self.min_profit_to_add_pct:
            logger.debug("Scale {}: WINNERS no add — profit {:.2f}% < min {:.1f}%",
                         self.symbol, profit_pct, self.min_profit_to_add_pct)
            return False

        if self.peak_since_entry > 0:
            peak_profit = self._profit_at_price(self.peak_since_entry)
            if peak_profit - profit_pct > self.pullback_tolerance_pct:
                logger.debug("Scale {}: WINNERS no add — pullback {:.2f}% from peak",
                             self.symbol, peak_profit - profit_pct)
                return False

        if self.last_add_price > 0:
            dist = abs(current_price - self.last_add_price) / self.last_add_price * 100
            if dist < self.min_profit_to_add_pct * 0.5:
                logger.debug("Scale {}: WINNERS no add — too close to last add ({:.2f}%)",
                             self.symbol, dist)
                return False

        logger.debug("Scale {}: WINNERS add OK — profit {:.2f}%, adds={}",
                     self.symbol, profit_pct, self.adds)
        return True

    def _should_add_pyramid(self, current_price: float) -> bool:
        """Add when price has dropped another dca_interval_pct from last add.

        Market makers wick through expected support to grab stop-loss liquidity,
        then reverse. We WANT to buy the wick, not get stopped by it.
        If price has recovered from a deeper trough (wick-through), that's
        actually the best time to add — the liquidity grab is done.
        """
        if self.last_add_price == 0:
            return False

        if self.side == "long":
            drop_pct = (self.last_add_price - current_price) / self.last_add_price * 100
            trough_drop = (self.last_add_price - self.trough_since_entry) / self.last_add_price * 100 if self.trough_since_entry > 0 else 0
            bounced_from_wick = trough_drop >= self.dca_interval_pct and drop_pct < trough_drop * 0.7
        else:
            drop_pct = (current_price - self.last_add_price) / self.last_add_price * 100
            trough_drop = (self.trough_since_entry - self.last_add_price) / self.last_add_price * 100 if self.trough_since_entry > 0 else 0
            bounced_from_wick = trough_drop >= self.dca_interval_pct and drop_pct < trough_drop * 0.7

        if bounced_from_wick:
            logger.info("Wick-through detected on {} | wicked {:.1f}% but now only {:.1f}% down -- "
                        "liquidity grab done, good DCA point", self.symbol, trough_drop, drop_pct)
            return True

        should = drop_pct >= self.dca_interval_pct
        logger.debug(
            "Scale {}: PYRAMID check — drop={:.2f}% interval={:.1f}% trough={:.2f}% "
            "price={:.6f} last_add={:.6f} => {}",
            self.symbol, drop_pct, self.dca_interval_pct, trough_drop,
            current_price, self.last_add_price, "ADD" if should else "wait",
        )
        return should

    def should_lever_up(self, current_price: float) -> bool:
        if self.mode != ScaleMode.PYRAMID:
            return False
        if self.leverage_raised:
            return False
        if self.adds < 1:
            return False

        profit_pct = self._current_profit_pct(current_price)
        should = profit_pct >= self.profit_to_lever_up_pct
        if should:
            logger.debug("Scale {}: lever-up ready — profit {:.2f}% >= {:.1f}%, "
                         "lev {}x -> {}x",
                         self.symbol, profit_pct, self.profit_to_lever_up_pct,
                         self.current_leverage, self.target_leverage)
        return should

    def should_take_partial(self, current_price: float) -> bool:
        if self.mode != ScaleMode.PYRAMID:
            return False
        if not self.leverage_raised:
            return False
        if self.partial_taken:
            return False

        profit_pct = self._current_profit_pct(current_price)
        threshold = self.profit_to_lever_up_pct * 2
        should = profit_pct >= threshold
        if should:
            logger.debug("Scale {}: partial take ready — profit {:.2f}% >= {:.1f}%, "
                         "taking {:.0f}% off",
                         self.symbol, profit_pct, threshold, self.partial_take_pct)
        return should

    def get_partial_take_amount(self) -> float:
        return self.current_size * (self.partial_take_pct / 100)

    def record_add(self, amount: float, price: float) -> None:
        total_cost = self.avg_entry_price * self.current_size + price * amount
        self.current_size += amount
        self.avg_entry_price = total_cost / self.current_size if self.current_size > 0 else price
        self.last_add_price = price
        self.adds += 1

        if self.notional_value >= self.max_notional * 0.95:
            self.phase = ScalePhase.FULL
        else:
            self.phase = ScalePhase.ADDING

        mode_tag = f" [{self.mode.value}]"
        logger.info("Scaled into {} - add #{} | size: {:.4f} | notional: ${:.0f}/${:.0f}K "
                     "({:.0f}%) | avg: {:.6f}{}",
                     self.symbol, self.adds, self.current_size,
                     self.notional_value, self.max_notional / 1000,
                     self.fill_pct, self.avg_entry_price, mode_tag)

    def record_partial_close(self, amount: float) -> None:
        self.current_size = max(0, self.current_size - amount)
        self.partial_taken = True
        logger.info("Partial close on {} | removed {:.4f} | remaining: {:.4f} | notional: ${:.0f}",
                     self.symbol, amount, self.current_size, self.notional_value)

    def record_lever_up(self, new_leverage: int) -> None:
        self.current_leverage = new_leverage
        self.leverage_raised = True
        logger.info("LEVERAGE RAISED on {} | {} -> {}x | notional now: ${:.0f}",
                     self.symbol, self.initial_leverage, new_leverage, self.notional_value)

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

    _profit_at_price = _current_profit_pct

    def status_line(self) -> str:
        return (
            f"{self.symbol} [{self.mode.value}] phase={self.phase.value} "
            f"adds={self.adds} notional=${self.notional_value:.0f}/${self.max_notional/1000:.0f}K "
            f"({self.fill_pct:.0f}%) avg={self.avg_entry_price:.6f} lev={self.current_leverage}x "
            f"lev_raised={self.leverage_raised} partial={self.partial_taken} "
            f"low_liq={self.low_liquidity}"
        )


class PositionScaler:
    """Manages scaled entries across all positions (both WINNERS and PYRAMID modes).

    Core idea: start with a fixed $ amount (default $50), keep adding
    as the position grows, stop when leveraged notional hits the cap ($100K).
    """

    def __init__(self, initial_risk_amount: float = 50.0,
                 max_notional: float = 100_000.0,
                 gambling_budget_pct: float = 2.0):
        self.initial_risk_amount = initial_risk_amount
        self.max_notional = max_notional
        self.gambling_budget_pct = gambling_budget_pct
        self._positions: dict[str, ScaledPosition] = {}

    def create(self, symbol: str, side: str, strategy: str,
               market_type: str = "futures", leverage: int = 10,
               low_liquidity: bool = False,
               mode: ScaleMode = ScaleMode.WINNERS,
               dca_interval_pct: float = 2.0,
               dca_multiplier: float = 1.5) -> ScaledPosition:

        if mode == ScaleMode.PYRAMID:
            init_lev = max(1, leverage // 5)
            sp = ScaledPosition(
                symbol=symbol, side=side, strategy=strategy,
                market_type=market_type,
                mode=ScaleMode.PYRAMID,
                initial_leverage=init_lev,
                target_leverage=leverage,
                current_leverage=init_lev,
                initial_risk_amount=self.initial_risk_amount,
                max_notional=self.max_notional,
                low_liquidity=low_liquidity,
                dca_interval_pct=dca_interval_pct,
                dca_multiplier=dca_multiplier,
            )
        else:
            sp = ScaledPosition(
                symbol=symbol, side=side, strategy=strategy,
                market_type=market_type, mode=ScaleMode.WINNERS,
                initial_leverage=leverage, target_leverage=leverage,
                current_leverage=leverage,
                initial_risk_amount=self.initial_risk_amount,
                max_notional=self.max_notional,
                low_liquidity=low_liquidity,
                phase=ScalePhase.GAMBLING if low_liquidity else ScalePhase.INITIAL,
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
                amount = sp.get_add_amount(price)
                if amount > 0:
                    to_add.append((sym, amount))
        return to_add

    def get_symbols_to_lever_up(self, prices: dict[str, float]) -> list[str]:
        result: list[str] = []
        for sym, sp in self._positions.items():
            price = prices.get(sym, 0)
            if price > 0 and sp.should_lever_up(price):
                result.append(sym)
        return result

    def get_symbols_for_partial_take(self, prices: dict[str, float]) -> list[tuple[str, float]]:
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
