from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from loguru import logger
from pydantic import BaseModel


class DailyRecord(BaseModel):
    day: int
    date: str
    start_balance: float
    end_balance: float
    pnl: float
    pnl_pct: float
    target_hit: bool
    trades: int = 0


class DailyTier(str, Enum):
    """Behavior tiers based on daily PnL percentage.

    Core goal: secure 3-5% daily profit aggressively, then ride with
    looser stops once secured.
    """

    LOSING = "losing"  # in the red — capital preservation mode
    BUILDING = "building"  # 0% to secure_target — aggressive profit-taking
    SECURED = "secured"  # secure_target to ride_target — daily goal zone
    STRONG = "strong"  # ride_target to 20% — let it ride, looser stops
    EXCELLENT = "excellent"  # 20-50% — exceptional day, start tightening
    MONSTER = "monster"  # 50-100% — protect hard, only ride existing
    LEGENDARY = "legendary"  # 100%+ — close all if reversal risk, or email + ride


class DailyTargetTracker:
    """Tracks progress toward daily return targets with tiered behavior.

    Core goal: secure 3-5% daily profit aggressively, then let it ride.

    Phase 1 — BUILDING (0% to secure_target):
        Be aggressive with profit-taking. Close winners early to bank the
        daily 3-5%. Every dollar of realized profit counts toward the target.

    Phase 2 — SECURED / STRONG (above secure_target):
        Daily profit is banked. Now let remaining and new positions ride
        with looser trailing stops. Chase profits, don't cut them short.
        Next day: if positions carry over in profit, secure the new day's
        3-5% first, then ride again.

    Priority hierarchy:
    1. CAPITAL IS SAFE (never risk what you have)
    2. Secure 3-5% daily (base goal — aggressive profit-taking)
    3. Once secured, ride with loose stops (let winners run)
    4. At 100%: close all if reversal risk detected, OR email owner

    Manual override via files in the working directory:
    - Create a file named STOP  → halt all new trades (existing positions ride)
    - Create a file named CLOSE_ALL → close all positions immediately
    - Delete the file to resume normal operation
    """

    STOP_FILE = Path("data/STOP")
    CLOSE_ALL_FILE = Path("data/CLOSE_ALL")

    def __init__(
        self,
        daily_target_pct: float = 5.0,
        compound: bool = True,
        aggressive_mode: bool = False,
        bot_data_dir: Path | None = None,
        secure_target_pct: float = 3.0,
        ride_target_pct: float = 5.0,
    ):
        self.daily_target_pct = daily_target_pct
        self.secure_target_pct = secure_target_pct
        self.ride_target_pct = ride_target_pct
        self.compound = compound
        self.aggressive_mode = aggressive_mode
        self._bot_data_dir = bot_data_dir

        self._day_start_balance: float = 0.0
        self._current_balance: float = 0.0
        self._day_number: int = 0
        self._initial_capital: float = 0.0
        self._last_reset: datetime | None = None
        self._history: list[DailyRecord] = []
        self._todays_trades: int = 0
        self._legendary_email_sent: bool = False
        self._pyramid_unrealized_pnl: float = 0.0
        self._profit_buffer_pct: float = 0.0
        self._realized_pnl_today: float = 0.0
        self._total_deposits: float = 0.0

    def record_trade(self, realized_pnl: float = 0.0) -> None:
        self._todays_trades += 1
        self._realized_pnl_today += realized_pnl

    def reset_day(self, balance: float) -> None:
        if self._day_number > 0:
            day_pnl_pct = (
                (balance - self._day_start_balance) / self._day_start_balance * 100 if self._day_start_balance else 0.0
            )
            self._history.append(
                DailyRecord(
                    day=self._day_number,
                    date=(self._last_reset or datetime.now(UTC)).strftime("%Y-%m-%d"),
                    start_balance=self._day_start_balance,
                    end_balance=balance,
                    pnl=balance - self._day_start_balance,
                    pnl_pct=day_pnl_pct,
                    target_hit=day_pnl_pct >= self.daily_target_pct,
                    trades=self._todays_trades,
                )
            )

            self._compute_profit_buffer(day_pnl_pct)
        else:
            self._profit_buffer_pct = 0.0

        if self._initial_capital == 0:
            self._initial_capital = balance

        self._day_start_balance = balance
        self._current_balance = balance
        self._pyramid_unrealized_pnl = 0.0
        self._realized_pnl_today = 0.0
        self._day_number += 1
        self._todays_trades = 0
        self._last_reset = datetime.now(UTC)

        logger.info(
            "Day {} started | Balance: {:.2f} | Target: {:.2f} (+{:.1f}%) | Total growth: {:.1f}%",
            self._day_number,
            balance,
            self.todays_target_balance,
            self.daily_target_pct,
            self.total_growth_pct,
        )

    def update_balance(
        self,
        balance: float,
        unrealized_pnl: float | None = None,
    ) -> float | None:
        """Update current balance, detecting deposits/withdrawals.

        When *unrealized_pnl* is provided (from ``fetch_positions``), the
        method compares the actual balance to what's expected from trading
        activity.  Any large unexplained jump is treated as an external
        deposit (or withdrawal) and the day-start balance is adjusted so
        daily PnL percentages remain accurate.

        If *unrealized_pnl* is ``None`` (the default), deposit detection
        is skipped — only the balance value is stored.

        Returns the deposit amount if one was detected, else ``None``.
        """
        if self._current_balance <= 0 or self._day_start_balance <= 0:
            self._current_balance = balance
            return None

        self._current_balance = balance

        if unrealized_pnl is None:
            return None

        expected_balance = self._day_start_balance + unrealized_pnl + self._realized_pnl_today
        unexplained = balance - expected_balance

        noise_threshold = max(5.0, self._day_start_balance * 0.005)

        if abs(unexplained) > noise_threshold:
            deposit = unexplained
            self._day_start_balance += deposit
            self._total_deposits += deposit
            logger.info(
                "DEPOSIT DETECTED: {:+.2f} USDT | Day-start adjusted: {:.2f} | Total deposits: {:+.2f}",
                deposit,
                self._day_start_balance,
                self._total_deposits,
            )
            return deposit
        return None

    def update_pyramid_unrealized(self, pnl: float) -> None:
        """Store the combined unrealized PnL of all PYRAMID-mode positions.

        This amount is subtracted from the raw daily PnL so that pyramid
        drawdowns (which are expected by design) don't push the bot into
        LOSING tier or reduce aggression.
        """
        self._pyramid_unrealized_pnl = pnl

    def _compute_profit_buffer(self, day_pnl_pct: float) -> None:
        """Carry forward excess profits as a risk buffer for the next day.

        Only excess above daily_target_pct counts.  50% of the excess
        carries forward, capped at 2x the base daily-loss limit (from
        settings).  Resets to 0 after a losing day.
        """
        if day_pnl_pct <= 0:
            self._profit_buffer_pct = 0.0
            return
        excess = max(0.0, day_pnl_pct - self.daily_target_pct)
        self._profit_buffer_pct = excess * 0.5
        logger.info(
            "Profit buffer: yesterday {:+.1f}%, excess {:.1f}%, buffer carried forward {:.1f}%",
            day_pnl_pct,
            excess,
            self._profit_buffer_pct,
        )

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
    def adjusted_todays_pnl(self) -> float:
        """Daily PnL excluding unrealized losses from PYRAMID positions.

        Only subtract pyramid *losses* (negative unrealized PnL). Positive
        pyramid PnL must not reduce reported daily PnL or the bot would
        incorrectly drop into LOSING tier and shrink position sizing.
        """
        return self.todays_pnl - min(self._pyramid_unrealized_pnl, 0)

    @property
    def adjusted_todays_pnl_pct(self) -> float:
        if self._day_start_balance == 0:
            return 0.0
        return self.adjusted_todays_pnl / self._day_start_balance * 100

    @property
    def profit_buffer_pct(self) -> float:
        return self._profit_buffer_pct

    @property
    def progress_pct(self) -> float:
        """0-100+ how far we are toward the daily target."""
        if self.daily_target_pct == 0:
            return 100.0
        return (self.todays_pnl_pct / self.daily_target_pct) * 100

    @property
    def target_reached(self) -> bool:
        """True when the secure_target_pct is reached (3% default)."""
        return self.todays_pnl_pct >= self.secure_target_pct

    @property
    def total_growth_pct(self) -> float:
        if self._initial_capital == 0:
            return 0.0
        trading_profit = self._current_balance - self._initial_capital - self._total_deposits
        return trading_profit / self._initial_capital * 100

    @property
    def projected_balance(self) -> dict[str, float]:
        """Project balance at various time horizons assuming target is hit daily."""
        b = self._current_balance or self._initial_capital
        mult = 1 + self.daily_target_pct / 100
        return {
            "1_week": b * (mult**7),
            "1_month": b * (mult**30),
            "3_months": b * (mult**90),
        }

    @property
    def daily_profit_secured(self) -> bool:
        """True once realized daily PnL reaches secure_target_pct (default 3%)."""
        return self.adjusted_todays_pnl_pct >= self.secure_target_pct

    @property
    def in_ride_mode(self) -> bool:
        """True once daily PnL passes ride_target_pct (default 5%).

        In this mode the bot uses loose stops and lets positions run freely.
        """
        return self.adjusted_todays_pnl_pct >= self.ride_target_pct

    @property
    def profit_taking_aggression(self) -> float:
        """Multiplier for profit-taking behavior.

        Before daily target is secured: 1.5x (take profits eagerly).
        In the secure zone (3-5%): 1.0x (normal).
        After ride target: 0.5x (loose — let winners run).
        """
        if not self.daily_profit_secured:
            return 1.5
        if not self.in_ride_mode:
            return 1.0
        return 0.5

    @property
    def total_deposits(self) -> float:
        """Total external deposits/withdrawals detected since inception."""
        return self._total_deposits

    @property
    def tier(self) -> DailyTier:
        pnl = self.adjusted_todays_pnl_pct
        if pnl < 0:
            return DailyTier.LOSING
        if pnl < self.secure_target_pct:
            return DailyTier.BUILDING
        if pnl < self.ride_target_pct:
            return DailyTier.SECURED
        if pnl < 20:
            return DailyTier.STRONG
        if pnl < 50:
            return DailyTier.EXCELLENT
        if pnl < 100:
            return DailyTier.MONSTER
        return DailyTier.LEGENDARY

    @property
    def manual_stop(self) -> bool:
        """Check if the user dropped a STOP file to halt trading.

        Checks both global data/STOP and per-bot data/{bot_id}/STOP.
        """
        return self.STOP_FILE.exists() or bool(self._bot_data_dir and (self._bot_data_dir / "STOP").exists())

    @property
    def manual_close_all(self) -> bool:
        """Check if the user dropped a CLOSE_ALL file to close everything.

        Checks both global data/CLOSE_ALL and per-bot data/{bot_id}/CLOSE_ALL.
        """
        return self.CLOSE_ALL_FILE.exists() or bool(self._bot_data_dir and (self._bot_data_dir / "CLOSE_ALL").exists())

    def clear_close_all(self) -> None:
        """Remove the CLOSE_ALL file after positions are closed."""
        with contextlib.suppress(OSError):
            self.CLOSE_ALL_FILE.unlink(missing_ok=True)

    def aggression_multiplier(self) -> float:
        """Position sizing multiplier based on tier. NEVER above 1.0.

        LOSING:     0.5-0.7x — capital preservation, shrink everything
        BUILDING:   1.0x     — full speed, secure 3-5%
        SECURED:    0.8x     — daily goal zone, slightly reduce
        STRONG:     0.6x     — 5%+ banked, riding with loose stops
        EXCELLENT:  0.3x     — 20-50%, only very high conviction
        MONSTER:    0.15x    — 50-100%, almost nothing new
        LEGENDARY:  0.0x     — 100%+, no new trades at all
        """
        if self.aggressive_mode:
            return 1.0

        t = self.tier

        if t == DailyTier.LOSING:
            return 0.5 if self.adjusted_todays_pnl_pct < -3 else 0.7
        if t == DailyTier.BUILDING:
            return 1.0
        if t == DailyTier.SECURED:
            return 0.8
        if t == DailyTier.STRONG:
            return 0.6
        if t == DailyTier.EXCELLENT:
            return 0.3
        if t == DailyTier.MONSTER:
            return 0.15
        return 0.0  # LEGENDARY: no new trades

    def should_trade(self) -> bool:
        """Whether we should open new positions.

        Manual override: STOP file kills all new entries.
        Tier-based: at MONSTER (50-100%) only high-conviction.
        At LEGENDARY (100%+): no new trades, ride existing only.
        Aggressive mode: always trade (only manual overrides respected).
        """
        if self.manual_stop:
            logger.warning("MANUAL STOP active (STOP file detected) -- no new trades")
            return False

        if self.manual_close_all:
            logger.warning("MANUAL CLOSE_ALL active -- closing all positions")
            return False

        if self.aggressive_mode:
            return True

        t = self.tier

        if t == DailyTier.LEGENDARY:
            logger.info("LEGENDARY day ({:+.1f}%) -- no new trades, riding existing", self.todays_pnl_pct)
            return False

        if t == DailyTier.MONSTER:
            logger.info("MONSTER day ({:+.1f}%) -- only ultra-high conviction", self.todays_pnl_pct)
            return True

        if t == DailyTier.EXCELLENT:
            logger.info("EXCELLENT day ({:+.1f}%) -- reducing entries, protecting gains", self.todays_pnl_pct)
            return True

        if t == DailyTier.SECURED:
            logger.info(
                "SECURED ({:+.1f}%) -- daily target zone, slightly reduced entries",
                self.todays_pnl_pct,
            )
            return True

        return True

    def should_close_all(self, reversal_risk: bool = False) -> tuple[bool, str]:
        """At 100%+ daily: should we close everything?

        Returns (should_close, reason).
        If reversal_risk is True and we're at LEGENDARY, close all.
        If no reversal risk, let it ride but flag for email notification.
        """
        if self.manual_close_all:
            return True, "Manual CLOSE_ALL file detected"

        if self.tier != DailyTier.LEGENDARY:
            return False, ""

        if reversal_risk:
            return True, (
                f"LEGENDARY day ({self.todays_pnl_pct:+.1f}%) with reversal risk -- closing all to lock in gains"
            )

        return False, ""

    def legendary_ride_reason(self, intel_summary: str = "") -> str:
        """Generate email content explaining why we're letting a 100%+ day ride."""
        self._legendary_email_sent = True
        return (
            f"LEGENDARY DAY ALERT\n"
            f"{'=' * 50}\n\n"
            f"Daily PnL: {self.todays_pnl_pct:+.1f}% (${self.todays_pnl:+,.2f})\n"
            f"Balance: ${self._current_balance:,.2f}\n"
            f"Trades today: {self._todays_trades}\n\n"
            f"DECISION: Letting positions ride.\n\n"
            f"Reasons to continue:\n"
            f"- No strong reversal signals detected\n"
            f"- Trailing stops are protecting all positions\n"
            f"- Break-even locks are active on profitable positions\n\n"
            f"Market conditions:\n{intel_summary}\n\n"
            f"To close everything immediately:\n"
            f"  Create a file named data/CLOSE_ALL in the bot directory\n"
            f"  Or: touch data/CLOSE_ALL\n\n"
            f"To stop new trades but keep existing:\n"
            f"  Create a file named data/STOP in the bot directory\n"
            f"  Or: touch data/STOP\n"
        )

    @property
    def legendary_email_sent(self) -> bool:
        return self._legendary_email_sent

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
    def best_day(self) -> DailyRecord | None:
        return max(self._history, key=lambda d: d.pnl_pct) if self._history else None

    @property
    def worst_day(self) -> DailyRecord | None:
        return min(self._history, key=lambda d: d.pnl_pct) if self._history else None

    def status_report(self) -> str:
        manual = ""
        if self.manual_stop:
            manual = " ** MANUAL STOP **"
        elif self.manual_close_all:
            manual = " ** CLOSE ALL **"

        mode_tag = ""
        if self.in_ride_mode:
            mode_tag = " [RIDING]"
        elif self.daily_profit_secured:
            mode_tag = " [SECURED]"
        else:
            mode_tag = " [HUNTING]"

        deposit_tag = f" | Deposits: {self._total_deposits:+.2f}" if self._total_deposits != 0 else ""
        return (
            f"Day {self._day_number} [{self.tier.value.upper()}]{mode_tag} | "
            f"PnL: {self.todays_pnl:+.2f} ({self.todays_pnl_pct:+.1f}%) | "
            f"Target: {self.secure_target_pct:.0f}-{self.ride_target_pct:.0f}% | "
            f"Progress: {self.progress_pct:.0f}% | "
            f"Balance: {self._current_balance:.2f}{deposit_tag}{manual}"
        )

    def compound_report(self) -> str:
        """Full compound growth report for email."""
        lines: list[str] = []
        lines.append("=" * 55)
        lines.append("         COMPOUND GROWTH REPORT")
        lines.append("=" * 55)
        lines.append("")
        lines.append(f"  Initial capital:     {self._initial_capital:>12.2f} USDT")
        if self._total_deposits != 0:
            lines.append(f"  Total deposits:      {self._total_deposits:>+12.2f} USDT")
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
