from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from config.settings import Settings
from core.models import Signal, SignalAction, Order, Position


class RiskManager:
    """Enforces risk rules before any order is placed.

    Philosophy: capital preservation first. Missing a good trade is fine.
    Taking a bad trade is not. The bot should happily sit idle all day
    if there's nothing worth deploying capital on.
    """

    def __init__(self, settings: Settings):
        self.max_position_pct = settings.max_position_size_pct
        self.max_daily_loss_pct = settings.max_daily_loss_pct
        self.default_stop_loss_pct = settings.stop_loss_pct
        self.default_take_profit_pct = settings.take_profit_pct
        self.max_concurrent = settings.max_concurrent_positions
        self.min_strength = settings.min_signal_strength
        self.loss_cooldown_threshold = settings.consecutive_loss_cooldown

        self._daily_pnl: float = 0.0
        self._day_start_balance: float = 0.0
        self._last_reset: Optional[datetime] = None
        self._consecutive_losses: int = 0
        self._in_cooldown = False
        self._total_trades_today: int = 0
        self._winning_trades_today: int = 0
        self._losing_trades_today: int = 0

    def reset_daily(self, balance: float) -> None:
        self._daily_pnl = 0.0
        self._day_start_balance = balance
        self._last_reset = datetime.now(timezone.utc)
        self._consecutive_losses = 0
        self._in_cooldown = False
        self._total_trades_today = 0
        self._winning_trades_today = 0
        self._losing_trades_today = 0
        logger.info("Daily risk reset. Starting balance: {:.2f}", balance)

    def record_pnl(self, pnl: float) -> None:
        self._daily_pnl += pnl
        self._total_trades_today += 1

        if pnl >= 0:
            self._consecutive_losses = 0
            self._winning_trades_today += 1
            if self._in_cooldown:
                self._in_cooldown = False
                logger.info("Cooldown lifted after winning trade")
        else:
            self._consecutive_losses += 1
            self._losing_trades_today += 1
            if self._consecutive_losses >= self.loss_cooldown_threshold:
                self._in_cooldown = True
                logger.warning("COOLDOWN ACTIVATED: {} consecutive losses - pausing new entries",
                               self._consecutive_losses)

    @property
    def daily_loss_pct(self) -> float:
        if self._day_start_balance == 0:
            return 0.0
        return abs(min(0, self._daily_pnl)) / self._day_start_balance * 100

    @property
    def daily_pnl_pct(self) -> float:
        if self._day_start_balance == 0:
            return 0.0
        return self._daily_pnl / self._day_start_balance * 100

    @property
    def win_rate_today(self) -> float:
        if self._total_trades_today == 0:
            return 0.0
        return self._winning_trades_today / self._total_trades_today * 100

    def is_daily_loss_exceeded(self) -> bool:
        exceeded = self.daily_loss_pct >= self.max_daily_loss_pct
        if exceeded:
            logger.warning("DAILY LOSS LIMIT HIT: {:.2f}% >= {:.2f}% -- NO MORE TRADES TODAY",
                           self.daily_loss_pct, self.max_daily_loss_pct)
        return exceeded

    def check_signal(self, signal: Signal, balance: float, positions: list[Position]) -> bool:
        """Returns True if the signal passes all risk checks. Conservative by default."""

        # Always allow closing positions
        if signal.action in (SignalAction.HOLD, SignalAction.CLOSE):
            return True

        # Hard stop: daily loss limit
        if self.is_daily_loss_exceeded():
            logger.warning("Rejecting signal: daily loss limit exceeded - protecting capital")
            return False

        # Cooldown after consecutive losses
        if self._in_cooldown:
            logger.info("Rejecting signal: in cooldown after {} consecutive losses",
                        self._consecutive_losses)
            return False

        # Signal strength gate -- don't trade weak signals
        if signal.strength < self.min_strength:
            logger.debug("Rejecting signal: strength {:.2f} below minimum {:.2f}",
                         signal.strength, self.min_strength)
            return False

        # Max concurrent positions
        active_positions = [p for p in positions if p.amount > 0]
        if len(active_positions) >= self.max_concurrent:
            logger.info("Rejecting signal: already at max {} concurrent positions",
                        self.max_concurrent)
            return False

        # Position size check
        if signal.suggested_price and signal.suggested_price > 0:
            position_value = signal.suggested_price * self._estimate_amount(signal, balance)
            max_allowed = balance * (self.max_position_pct / 100)
            if position_value > max_allowed:
                logger.warning("Rejecting signal: position size {:.2f} exceeds max {:.2f}",
                               position_value, max_allowed)
                return False

        # Total exposure cap
        total_exposure = sum(p.notional_value for p in active_positions)
        if total_exposure > balance * 1.5:
            logger.warning("Rejecting signal: total exposure {:.2f} too high vs balance {:.2f}",
                           total_exposure, balance)
            return False

        # Progressive drawdown protection: reduce allowed position size as losses accumulate
        if self.daily_loss_pct > self.max_daily_loss_pct * 0.5:
            logger.info("Drawdown at {:.1f}% -- only high-conviction trades allowed",
                        self.daily_loss_pct)
            if signal.strength < 0.7:
                logger.info("Rejecting signal: not high-conviction enough during drawdown")
                return False

        return True

    def apply_stops(self, signal: Signal) -> Signal:
        """Add default stop-loss and take-profit if not already set."""
        price = signal.suggested_price
        if not price or price == 0:
            return signal

        updated = signal.model_copy()

        if not updated.suggested_stop_loss:
            if signal.action == SignalAction.BUY:
                updated.suggested_stop_loss = price * (1 - self.default_stop_loss_pct / 100)
            else:
                updated.suggested_stop_loss = price * (1 + self.default_stop_loss_pct / 100)

        if not updated.suggested_take_profit:
            if signal.action == SignalAction.BUY:
                updated.suggested_take_profit = price * (1 + self.default_take_profit_pct / 100)
            else:
                updated.suggested_take_profit = price * (1 - self.default_take_profit_pct / 100)

        return updated

    def _estimate_amount(self, signal: Signal, balance: float) -> float:
        max_value = balance * (self.max_position_pct / 100)
        if signal.suggested_price and signal.suggested_price > 0:
            return max_value / signal.suggested_price
        return 0.0

    def calculate_position_size(
        self, balance: float, price: float, leverage: int = 1, risk_pct: Optional[float] = None
    ) -> float:
        """Calculate position size. Scales down as daily losses accumulate."""
        pct = risk_pct or self.max_position_pct

        # Scale down position size based on how deep we are into daily loss limit
        if self.daily_loss_pct > 0:
            loss_ratio = self.daily_loss_pct / self.max_daily_loss_pct
            scale = max(0.3, 1.0 - loss_ratio * 0.7)
            pct *= scale

        capital_at_risk = balance * (pct / 100)
        notional = capital_at_risk * leverage
        if price == 0:
            return 0.0
        return notional / price

    def check_liquidation(self, position: Position, balance: float) -> bool:
        if position.leverage <= 1:
            return False
        liq_threshold = -100 / position.leverage * 0.9
        return position.pnl_pct <= liq_threshold

    def risk_summary(self) -> str:
        return (
            f"Risk | Loss: {self.daily_loss_pct:.1f}%/{self.max_daily_loss_pct:.1f}% | "
            f"Trades: {self._total_trades_today} (W:{self._winning_trades_today} L:{self._losing_trades_today}) | "
            f"WR: {self.win_rate_today:.0f}% | "
            f"Streak: {'COOLDOWN' if self._in_cooldown else f'{self._consecutive_losses} losses'}"
        )
