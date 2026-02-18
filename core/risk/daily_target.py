from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from loguru import logger
from pydantic import BaseModel, Field


class DailyRecord(BaseModel):
    day: int
    date: str
    start_balance: float
    end_balance: float
    pnl: float
    pnl_pct: float
    target_hit: bool
    trades: int = 0


class DailyTargetTracker:
    """Tracks progress toward a daily return target with compounding.

    Adjusts position sizing aggression based on how close we are to the
    daily goal. If we're behind, allows slightly larger positions. If we've
    hit the target, reduces aggression to protect gains.
    """

    def __init__(self, daily_target_pct: float = 10.0, compound: bool = True):
        self.daily_target_pct = daily_target_pct
        self.compound = compound

        self._day_start_balance: float = 0.0
        self._current_balance: float = 0.0
        self._day_number: int = 0
        self._initial_capital: float = 0.0
        self._last_reset: Optional[datetime] = None
        self._history: list[DailyRecord] = []
        self._todays_trades: int = 0

    def record_trade(self) -> None:
        self._todays_trades += 1

    def reset_day(self, balance: float) -> None:
        if self._day_number > 0:
            self._history.append(DailyRecord(
                day=self._day_number,
                date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                start_balance=self._day_start_balance,
                end_balance=balance,
                pnl=balance - self._day_start_balance,
                pnl_pct=(balance - self._day_start_balance) / self._day_start_balance * 100
                if self._day_start_balance else 0,
                target_hit=self.target_reached,
                trades=self._todays_trades,
            ))

        if self._initial_capital == 0:
            self._initial_capital = balance

        self._day_start_balance = balance
        self._current_balance = balance
        self._day_number += 1
        self._todays_trades = 0
        self._last_reset = datetime.now(timezone.utc)

        logger.info("Day {} started | Balance: {:.2f} | Target: {:.2f} (+{:.1f}%) | "
                     "Total growth: {:.1f}%",
                     self._day_number, balance, self.todays_target_balance,
                     self.daily_target_pct, self.total_growth_pct)

    def update_balance(self, balance: float) -> None:
        self._current_balance = balance

    @property
    def todays_target_balance(self) -> float:
        return self._day_start_balance * (1 + self.daily_target_pct / 100)

    @property
    def todays_pnl(self) -> float:
        return self._current_balance - self._day_start_balance

    @property
    def todays_pnl_pct(self) -> float:
        if self._day_start_balance == 0:
            return 0.0
        return self.todays_pnl / self._day_start_balance * 100

    @property
    def progress_pct(self) -> float:
        """0-100+ how far we are toward the daily target."""
        if self.daily_target_pct == 0:
            return 100.0
        return (self.todays_pnl_pct / self.daily_target_pct) * 100

    @property
    def target_reached(self) -> bool:
        return self.todays_pnl_pct >= self.daily_target_pct

    @property
    def total_growth_pct(self) -> float:
        if self._initial_capital == 0:
            return 0.0
        return (self._current_balance - self._initial_capital) / self._initial_capital * 100

    @property
    def projected_balance(self) -> dict[str, float]:
        """Project balance at various time horizons assuming target is hit daily."""
        b = self._current_balance or self._initial_capital
        mult = 1 + self.daily_target_pct / 100
        return {
            "1_week": b * (mult ** 7),
            "1_month": b * (mult ** 30),
            "3_months": b * (mult ** 90),
        }

    def aggression_multiplier(self) -> float:
        """Position sizing multiplier. NEVER above 1.0 -- we don't chase losses.

        - Losing money:        0.5x (cut size, preserve capital)
        - Flat / behind:       0.8x (cautious)
        - On track (50-90%):   1.0x (normal)
        - Near target (90%+):  0.7x (protect gains)
        - Target exceeded:     0.4x (ride existing winners only)
        """
        p = self.progress_pct

        if p < -20:
            return 0.5
        if p < 0:
            return 0.6
        if p < 50:
            return 0.8
        if p < 90:
            return 1.0
        if p < 100:
            return 0.7
        return 0.4

    def should_trade(self) -> bool:
        """Whether we should open new positions. Returns False to sit out entirely."""
        if self.target_reached:
            logger.info("Daily target reached ({:.1f}%) -- only riding existing winners",
                        self.todays_pnl_pct)
            return False

        if self.todays_pnl_pct < -self.daily_target_pct * 0.3:
            logger.warning("Down {:.1f}% today -- sitting out to preserve capital",
                           self.todays_pnl_pct)
            return False

        return True

    @property
    def history(self) -> list[DailyRecord]:
        return list(self._history)

    @property
    def winning_days(self) -> int:
        return sum(1 for d in self._history if d.pnl > 0)

    @property
    def losing_days(self) -> int:
        return sum(1 for d in self._history if d.pnl < 0)

    @property
    def target_hit_days(self) -> int:
        return sum(1 for d in self._history if d.target_hit)

    @property
    def avg_daily_pnl_pct(self) -> float:
        if not self._history:
            return 0.0
        return sum(d.pnl_pct for d in self._history) / len(self._history)

    @property
    def best_day(self) -> Optional[DailyRecord]:
        return max(self._history, key=lambda d: d.pnl_pct) if self._history else None

    @property
    def worst_day(self) -> Optional[DailyRecord]:
        return min(self._history, key=lambda d: d.pnl_pct) if self._history else None

    def status_report(self) -> str:
        return (
            f"Day {self._day_number} | "
            f"PnL: {self.todays_pnl:+.2f} ({self.todays_pnl_pct:+.1f}%) | "
            f"Target: {self.daily_target_pct:.1f}% | "
            f"Progress: {self.progress_pct:.0f}% | "
            f"Balance: {self._current_balance:.2f}"
        )

    def compound_report(self) -> str:
        """Full compound growth report for email."""
        lines: list[str] = []
        lines.append("=" * 55)
        lines.append("         COMPOUND GROWTH REPORT")
        lines.append("=" * 55)
        lines.append("")
        lines.append(f"  Initial capital:     {self._initial_capital:>12.2f} USDT")
        lines.append(f"  Current balance:     {self._current_balance:>12.2f} USDT")
        lines.append(f"  Total growth:        {self.total_growth_pct:>+11.1f}%")
        lines.append(f"  Days running:        {self._day_number:>12d}")
        lines.append(f"  Daily target:        {self.daily_target_pct:>11.1f}%")
        lines.append("")

        if self._history:
            lines.append(f"  Winning days:        {self.winning_days:>12d}")
            lines.append(f"  Losing days:         {self.losing_days:>12d}")
            lines.append(f"  Target hit days:     {self.target_hit_days:>12d}")
            lines.append(f"  Avg daily PnL:       {self.avg_daily_pnl_pct:>+11.1f}%")

            best = self.best_day
            worst = self.worst_day
            if best:
                lines.append(f"  Best day:            {best.pnl_pct:>+11.1f}%  (Day {best.day})")
            if worst:
                lines.append(f"  Worst day:           {worst.pnl_pct:>+11.1f}%  (Day {worst.day})")

            lines.append("")
            lines.append("-" * 55)
            lines.append(f"  {'Day':<5} {'Date':<12} {'Start':>10} {'End':>10} {'PnL%':>8} {'Hit':>4}")
            lines.append("-" * 55)

            for rec in self._history:
                hit = "Yes" if rec.target_hit else " - "
                lines.append(
                    f"  {rec.day:<5d} {rec.date:<12} "
                    f"{rec.start_balance:>10.2f} {rec.end_balance:>10.2f} "
                    f"{rec.pnl_pct:>+7.1f}% {hit:>4}"
                )

        lines.append("")
        projected = self.projected_balance
        lines.append("  PROJECTIONS (if target hit daily):")
        lines.append(f"    1 week:            {projected['1_week']:>12.2f} USDT")
        lines.append(f"    1 month:           {projected['1_month']:>12.2f} USDT")
        lines.append(f"    3 months:          {projected['3_months']:>12.2f} USDT")
        lines.append("=" * 55)

        return "\n".join(lines)
