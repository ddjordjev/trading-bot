"""Tests for the tiered daily target system.

These are critical: wrong tier logic means the bot either risks too much
on a winning day or overtrades on a losing day.
"""

import pytest

from core.risk.daily_target import DailyTargetTracker, DailyTier


@pytest.fixture
def tracker():
    t = DailyTargetTracker(daily_target_pct=10.0)
    t.reset_day(1000.0)
    return t


class TestTierClassification:
    def test_losing_tier(self, tracker: DailyTargetTracker):
        tracker.update_balance(950.0)
        assert tracker.tier == DailyTier.LOSING

    def test_building_tier(self, tracker: DailyTargetTracker):
        tracker.update_balance(1020.0)  # +2% — below secure_target (3%)
        assert tracker.tier == DailyTier.BUILDING

    def test_secured_tier(self, tracker: DailyTargetTracker):
        tracker.update_balance(1040.0)  # +4% — between secure (3%) and ride (5%)
        assert tracker.tier == DailyTier.SECURED

    def test_strong_tier(self, tracker: DailyTargetTracker):
        tracker.update_balance(1100.0)  # +10% — above ride_target (5%), below 20%
        assert tracker.tier == DailyTier.STRONG

    def test_excellent_tier(self, tracker: DailyTargetTracker):
        tracker.update_balance(1350.0)
        assert tracker.tier == DailyTier.EXCELLENT

    def test_monster_tier(self, tracker: DailyTargetTracker):
        tracker.update_balance(1700.0)
        assert tracker.tier == DailyTier.MONSTER

    def test_legendary_tier(self, tracker: DailyTargetTracker):
        tracker.update_balance(2100.0)
        assert tracker.tier == DailyTier.LEGENDARY


class TestAggression:
    def test_never_above_one(self, tracker: DailyTargetTracker):
        for bal in [900, 950, 1000, 1050, 1100, 1200, 1500, 2000]:
            tracker.update_balance(float(bal))
            assert tracker.aggression_multiplier() <= 1.0

    def test_legendary_is_zero(self, tracker: DailyTargetTracker):
        tracker.update_balance(2100.0)
        assert tracker.aggression_multiplier() == 0.0

    def test_losing_is_reduced(self, tracker: DailyTargetTracker):
        tracker.update_balance(930.0)
        assert tracker.aggression_multiplier() <= 0.7

    def test_building_full_aggression(self, tracker: DailyTargetTracker):
        tracker.update_balance(1020.0)  # +2% BUILDING
        assert tracker.aggression_multiplier() == 1.0


class TestShouldTrade:
    def test_legendary_blocks_new_entries(self, tracker: DailyTargetTracker):
        tracker.update_balance(2100.0)
        assert tracker.should_trade() is False

    def test_building_allows_trading(self, tracker: DailyTargetTracker):
        tracker.update_balance(1050.0)
        assert tracker.should_trade() is True

    def test_deep_loss_still_allows_trading(self, tracker: DailyTargetTracker):
        tracker.update_balance(960.0)  # -4% — no blanket sit-out, aggression is just reduced
        assert tracker.should_trade() is True

    def test_moderate_loss_allows_trading(self, tracker: DailyTargetTracker):
        tracker.update_balance(985.0)
        assert tracker.should_trade() is True

    def test_losing_tier_never_blocks(self, tracker: DailyTargetTracker):
        tracker.update_balance(900.0)  # -10%
        assert tracker.should_trade() is True


class TestCloseAllDecision:
    def test_close_all_on_legendary_with_reversal(self, tracker: DailyTargetTracker):
        tracker.update_balance(2100.0)
        should_close, reason = tracker.should_close_all(reversal_risk=True)
        assert should_close is True
        assert "reversal" in reason.lower()

    def test_ride_legendary_without_reversal(self, tracker: DailyTargetTracker):
        tracker.update_balance(2100.0)
        should_close, _ = tracker.should_close_all(reversal_risk=False)
        assert should_close is False

    def test_no_close_below_legendary(self, tracker: DailyTargetTracker):
        tracker.update_balance(1500.0)
        should_close, _ = tracker.should_close_all(reversal_risk=True)
        assert should_close is False


class TestPnLCalculation:
    def test_pnl_pct_positive(self, tracker: DailyTargetTracker):
        tracker.update_balance(1100.0)
        assert tracker.todays_pnl_pct == pytest.approx(10.0)

    def test_pnl_pct_negative(self, tracker: DailyTargetTracker):
        tracker.update_balance(900.0)
        assert tracker.todays_pnl_pct == pytest.approx(-10.0)

    def test_progress_at_target(self, tracker: DailyTargetTracker):
        tracker.update_balance(1100.0)
        assert tracker.progress_pct == pytest.approx(100.0)


class TestPyramidExclusion:
    """Pyramid unrealized PnL should not affect tier or aggression."""

    def test_adjusted_pnl_excludes_pyramid(self, tracker: DailyTargetTracker):
        tracker.update_balance(950.0)  # raw PnL = -50 (-5%)
        tracker.update_pyramid_unrealized(-50.0)  # all of the loss is pyramid
        assert tracker.adjusted_todays_pnl_pct == pytest.approx(0.0)

    def test_tier_uses_adjusted_pnl(self, tracker: DailyTargetTracker):
        tracker.update_balance(950.0)  # raw = -5%, would be LOSING
        tracker.update_pyramid_unrealized(-50.0)
        assert tracker.tier == DailyTier.BUILDING  # adjusted is 0%

    def test_aggression_uses_adjusted_pnl(self, tracker: DailyTargetTracker):
        tracker.update_balance(930.0)  # raw = -7%
        tracker.update_pyramid_unrealized(-70.0)
        assert tracker.aggression_multiplier() >= 0.8  # adjusted is 0% → BUILDING

    def test_raw_pnl_still_correct(self, tracker: DailyTargetTracker):
        tracker.update_balance(950.0)
        tracker.update_pyramid_unrealized(-50.0)
        assert tracker.todays_pnl_pct == pytest.approx(-5.0)

    def test_pyramid_unrealized_resets_on_new_day(self, tracker: DailyTargetTracker):
        tracker.update_pyramid_unrealized(-100.0)
        tracker.reset_day(1000.0)
        assert tracker._pyramid_unrealized_pnl == 0.0

    def test_partial_pyramid_exclusion(self, tracker: DailyTargetTracker):
        tracker.update_balance(940.0)  # raw = -60 (-6%)
        tracker.update_pyramid_unrealized(-30.0)  # half is pyramid
        assert tracker.adjusted_todays_pnl_pct == pytest.approx(-3.0)
        assert tracker.tier == DailyTier.LOSING


class TestProfitBuffer:
    """Excess profits carry forward as a risk buffer."""

    def test_buffer_from_big_day(self):
        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(1000.0)
        t.update_balance(1500.0)  # +50%
        t.reset_day(1500.0)
        assert t.profit_buffer_pct == pytest.approx(20.0)  # (50-10)*0.5

    def test_buffer_zero_when_below_target(self):
        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(1000.0)
        t.update_balance(1050.0)  # +5%, below 10% target
        t.reset_day(1050.0)
        assert t.profit_buffer_pct == pytest.approx(0.0)

    def test_buffer_resets_after_losing_day(self):
        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(1000.0)
        t.update_balance(1500.0)  # +50% → buffer = 20
        t.reset_day(1500.0)
        assert t.profit_buffer_pct == pytest.approx(20.0)
        t.update_balance(1400.0)  # -6.67% losing day
        t.reset_day(1400.0)
        assert t.profit_buffer_pct == pytest.approx(0.0)

    def test_buffer_zero_on_first_day(self):
        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(1000.0)
        assert t.profit_buffer_pct == pytest.approx(0.0)

    def test_buffer_at_exactly_target(self):
        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(1000.0)
        t.update_balance(1100.0)  # exactly 10%
        t.reset_day(1100.0)
        assert t.profit_buffer_pct == pytest.approx(0.0)  # no excess


class TestDepositDetection:
    """External deposits/withdrawals should adjust day-start balance,
    not inflate daily PnL."""

    def test_deposit_detected_when_balance_jumps(self):
        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(1000.0)
        # Balance jumped to 1500 with 0 unrealized PnL → $500 is a deposit
        t.update_balance(1500.0, unrealized_pnl=0.0)
        assert t._total_deposits == pytest.approx(500.0)
        assert t._day_start_balance == pytest.approx(1500.0)
        # Daily PnL should be ~0% since profit came from deposit
        assert abs(t.todays_pnl_pct) < 1.0

    def test_no_false_positive_from_trading_profit(self):
        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(1000.0)
        # Balance is 1050, unrealized PnL explains the gain
        t.update_balance(1050.0, unrealized_pnl=50.0)
        assert t._total_deposits == pytest.approx(0.0)
        assert t.todays_pnl_pct == pytest.approx(5.0)

    def test_deposit_plus_trading_profit(self):
        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(1000.0)
        # Balance is 1550: $50 unrealized + $500 deposit
        t.update_balance(1550.0, unrealized_pnl=50.0)
        assert t._total_deposits == pytest.approx(500.0)
        # Day-start adjusted to 1500, current 1550 → +3.33% from trading
        assert t.todays_pnl_pct == pytest.approx(50.0 / 1500.0 * 100, abs=0.5)

    def test_withdrawal_detected(self):
        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(1000.0)
        # Balance dropped to 700 with 0 unrealized → $300 withdrawal
        t.update_balance(700.0, unrealized_pnl=0.0)
        assert t._total_deposits == pytest.approx(-300.0)
        assert t._day_start_balance == pytest.approx(700.0)

    def test_small_fluctuation_ignored(self):
        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(1000.0)
        # $3 difference is below the noise threshold ($5)
        t.update_balance(1003.0, unrealized_pnl=0.0)
        assert t._total_deposits == pytest.approx(0.0)

    def test_deposit_skipped_when_no_unrealized_provided(self):
        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(1000.0)
        # No unrealized_pnl passed → deposit detection skipped
        t.update_balance(1500.0)
        assert t._total_deposits == pytest.approx(0.0)
        assert t.todays_pnl_pct == pytest.approx(50.0)

    def test_total_growth_excludes_deposits(self):
        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(1000.0)
        # Deposit $500, then make $50 trading profit
        t.update_balance(1550.0, unrealized_pnl=50.0)
        # Total growth = (1550 - 1000 - 500) / 1000 = 5%
        assert t.total_growth_pct == pytest.approx(5.0)

    def test_deposit_resets_on_new_day(self):
        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(1000.0)
        t.update_balance(1500.0, unrealized_pnl=0.0)
        assert t._total_deposits == pytest.approx(500.0)
        # New day — realized_pnl_today resets, but total_deposits persists
        t.reset_day(1500.0)
        assert t._total_deposits == pytest.approx(500.0)
        assert t._realized_pnl_today == pytest.approx(0.0)

    def test_realized_pnl_prevents_false_deposit(self):
        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(1000.0)
        # Closed a trade for $100 profit (realized)
        t.record_trade(realized_pnl=100.0)
        # Balance now 1100 with 0 unrealized — the $100 is explained by realized PnL
        t.update_balance(1100.0, unrealized_pnl=0.0)
        assert t._total_deposits == pytest.approx(0.0)

    def test_multiple_deposits_accumulate(self):
        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(1000.0)
        t.update_balance(1200.0, unrealized_pnl=0.0)  # +200 deposit
        assert t._total_deposits == pytest.approx(200.0)
        t.update_balance(1500.0, unrealized_pnl=0.0)  # +300 more
        assert t._total_deposits == pytest.approx(500.0)

    def test_status_report_shows_deposits(self):
        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(1000.0)
        t.update_balance(1500.0, unrealized_pnl=0.0)
        report = t.status_report()
        assert "Deposits" in report
        assert "+500.00" in report

    def test_status_report_hides_zero_deposits(self):
        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(1000.0)
        t.update_balance(1050.0)
        report = t.status_report()
        assert "Deposits" not in report

    def test_compound_report_shows_deposits(self):
        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(1000.0)
        t.update_balance(1500.0, unrealized_pnl=0.0)
        t.reset_day(1500.0)
        report = t.compound_report()
        assert "deposits" in report.lower()

    def test_total_deposits_property(self):
        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(1000.0)
        assert t.total_deposits == 0.0
        t.update_balance(1200.0, unrealized_pnl=0.0)
        assert t.total_deposits == pytest.approx(200.0)
