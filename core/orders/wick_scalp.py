from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from loguru import logger
from pydantic import BaseModel, Field


class WickScalp(BaseModel):
    """A quick counter-trade that exploits a wick while the main PYRAMID position DCA-s.

    Example: main position is LONG BTC, DCA-ing down. A wick happens (fast drop).
    -> Open a SHORT scalp to ride the wick down
    -> Wick reverses back up (as MM wicks do)
    -> Short scalp gets stopped for profit
    -> Main PYRAMID long got a better avg entry from the DCA
    -> Net: profited from the wick AND improved the main position.
    """

    symbol: str
    main_side: str                # the PYRAMID direction ("long" or "short")
    scalp_side: str               # opposite of main_side
    entry_price: float = 0.0
    amount: float = 0.0
    leverage: int = 10
    order_id: str = ""
    active: bool = False
    closed: bool = False
    pnl: float = 0.0
    max_hold_minutes: int = 5     # very short -- just ride the wick
    trail_pct: float = 0.3        # tight trail to lock wick profit fast
    stop_pct: float = 1.0         # hard stop if wick doesn't materialize
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def age_minutes(self) -> float:
        return (datetime.now(timezone.utc) - self.created_at).total_seconds() / 60

    @property
    def expired(self) -> bool:
        return self.age_minutes > self.max_hold_minutes


class WickScalpDetector:
    """Detects wick opportunities on PYRAMID positions and manages counter-scalps.

    A wick is detected when:
    1. A PYRAMID position exists and is currently in drawdown
    2. Price is moving fast against the position (velocity check)
    3. The move exceeds a threshold (not just noise)

    When a wick is detected, we signal the OrderManager to open a quick
    counter-scalp. The scalp has a very tight trail and short max hold.
    """

    def __init__(self, wick_threshold_pct: float = 1.5,
                 velocity_candles: int = 3,
                 min_wick_velocity: float = 0.5,
                 scalp_budget_pct: float = 100.0,
                 max_concurrent_scalps: int = 2):
        self.wick_threshold_pct = wick_threshold_pct
        self.velocity_candles = velocity_candles
        self.min_wick_velocity = min_wick_velocity
        self.scalp_budget_pct = scalp_budget_pct
        self.max_concurrent_scalps = max_concurrent_scalps

        self._active_scalps: dict[str, WickScalp] = {}
        self._recent_prices: dict[str, list[float]] = {}

    def feed_price(self, symbol: str, price: float) -> None:
        """Feed latest price for velocity tracking."""
        if symbol not in self._recent_prices:
            self._recent_prices[symbol] = []
        self._recent_prices[symbol].append(price)
        if len(self._recent_prices[symbol]) > 20:
            self._recent_prices[symbol] = self._recent_prices[symbol][-20:]

    def check_for_wick(self, symbol: str, main_side: str,
                       current_price: float, entry_price: float) -> Optional[WickScalp]:
        """Check if a wick is happening that we can scalp.

        Returns a WickScalp if we should open one, None otherwise.
        """
        if symbol in self._active_scalps and not self._active_scalps[symbol].closed:
            return None

        active_count = sum(1 for s in self._active_scalps.values() if s.active and not s.closed)
        if active_count >= self.max_concurrent_scalps:
            return None

        prices = self._recent_prices.get(symbol, [])
        if len(prices) < self.velocity_candles + 1:
            return None

        recent = prices[-(self.velocity_candles + 1):]
        velocity = self._calculate_velocity(recent, main_side)

        if velocity < self.min_wick_velocity:
            return None

        if main_side == "long":
            drop_from_entry = (entry_price - current_price) / entry_price * 100
            if drop_from_entry < self.wick_threshold_pct:
                return None
            scalp_side = "short"
        else:
            rise_from_entry = (current_price - entry_price) / entry_price * 100
            if rise_from_entry < self.wick_threshold_pct:
                return None
            scalp_side = "long"

        scalp = WickScalp(
            symbol=symbol,
            main_side=main_side,
            scalp_side=scalp_side,
        )

        logger.info("WICK DETECTED on {} | main={} | velocity={:.2f}%/candle | "
                     "opening {} scalp to ride the wick",
                     symbol, main_side, velocity, scalp_side)

        return scalp

    def activate(self, symbol: str, scalp: WickScalp, entry_price: float,
                 amount: float, order_id: str) -> None:
        scalp.entry_price = entry_price
        scalp.amount = amount
        scalp.order_id = order_id
        scalp.active = True
        self._active_scalps[symbol] = scalp
        logger.info("Wick scalp ACTIVE on {} | {} @ {:.6f} | trail={:.1f}% | max_hold={}m",
                     symbol, scalp.scalp_side, entry_price, scalp.trail_pct, scalp.max_hold_minutes)

    def close(self, symbol: str, pnl: float = 0.0) -> None:
        scalp = self._active_scalps.get(symbol)
        if scalp:
            scalp.closed = True
            scalp.active = False
            scalp.pnl = pnl
            logger.info("Wick scalp CLOSED on {} | pnl={:+.2f}", symbol, pnl)

    def get_expired(self) -> list[str]:
        """Return symbols with expired wick scalps that should be closed."""
        expired = []
        for sym, scalp in self._active_scalps.items():
            if scalp.active and not scalp.closed and scalp.expired:
                expired.append(sym)
        return expired

    def get(self, symbol: str) -> Optional[WickScalp]:
        return self._active_scalps.get(symbol)

    def has_active(self, symbol: str) -> bool:
        s = self._active_scalps.get(symbol)
        return s is not None and s.active and not s.closed

    def cleanup(self) -> None:
        """Remove closed scalps older than 10 minutes."""
        to_remove = [
            sym for sym, s in self._active_scalps.items()
            if s.closed and s.age_minutes > 10
        ]
        for sym in to_remove:
            del self._active_scalps[sym]

    @property
    def active_scalps(self) -> dict[str, WickScalp]:
        return {s: w for s, w in self._active_scalps.items() if w.active and not w.closed}

    def _calculate_velocity(self, prices: list[float], main_side: str) -> float:
        """Average % move per candle against the main position direction."""
        if len(prices) < 2:
            return 0.0

        moves = []
        for i in range(1, len(prices)):
            pct = (prices[i] - prices[i - 1]) / prices[i - 1] * 100
            if main_side == "long":
                moves.append(-pct)  # negative price move = against long
            else:
                moves.append(pct)   # positive price move = against short

        against_moves = [m for m in moves if m > 0]
        if not against_moves:
            return 0.0

        return sum(against_moves) / len(against_moves)
