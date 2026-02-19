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
        tracker.update_balance(1050.0)
        assert tracker.tier == DailyTier.BUILDING

    def test_strong_tier(self, tracker: DailyTargetTracker):
        tracker.update_balance(1150.0)
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

    def test_building_ramps_up(self, tracker: DailyTargetTracker):
        tracker.update_balance(1080.0)
        assert tracker.aggression_multiplier() >= 0.8


class TestShouldTrade:
    def test_legendary_blocks_new_entries(self, tracker: DailyTargetTracker):
        tracker.update_balance(2100.0)
        assert tracker.should_trade() is False

    def test_building_allows_trading(self, tracker: DailyTargetTracker):
        tracker.update_balance(1050.0)
        assert tracker.should_trade() is True

    def test_deep_loss_blocks_trading(self, tracker: DailyTargetTracker):
        tracker.update_balance(960.0)  # -4%, threshold is -3%
        assert tracker.should_trade() is False

    def test_moderate_loss_allows_trading(self, tracker: DailyTargetTracker):
        tracker.update_balance(985.0)
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
