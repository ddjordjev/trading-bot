"""Tests for the risk manager.

This module gates every single trade. If it's broken, the bot either
takes trades it shouldn't (catastrophic) or misses everything (annoying
but safe). We test both directions.
"""
import pytest
from core.models import Signal, SignalAction, Position, OrderSide
from core.risk.manager import RiskManager
from config.settings import Settings


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("EXCHANGE", "mexc")
    monkeypatch.setenv("MAX_POSITION_SIZE_PCT", "5.0")
    monkeypatch.setenv("MAX_DAILY_LOSS_PCT", "3.0")
    monkeypatch.setenv("STOP_LOSS_PCT", "2.0")
    monkeypatch.setenv("TAKE_PROFIT_PCT", "4.0")
    monkeypatch.setenv("MAX_CONCURRENT_POSITIONS", "5")
    monkeypatch.setenv("MIN_SIGNAL_STRENGTH", "0.4")
    monkeypatch.setenv("CONSECUTIVE_LOSS_COOLDOWN", "3")
    return Settings(_env_file=None)


@pytest.fixture
def risk(settings: Settings) -> RiskManager:
    rm = RiskManager(settings)
    rm.reset_daily(10000.0)
    return rm


def _signal(action=SignalAction.BUY, strength=0.7, price=100.0) -> Signal:
    return Signal(
        symbol="BTC/USDT", action=action, strategy="test",
        strength=strength, suggested_price=price,
    )


class TestDailyLossLimit:
    def test_blocks_when_exceeded(self, risk: RiskManager):
        risk.record_pnl(-350.0)  # 3.5% of 10000 > 3% limit
        sig = _signal()
        assert risk.check_signal(sig, 10000.0, []) is False

    def test_allows_when_within_limit(self, risk: RiskManager):
        risk.record_pnl(-100.0)
        sig = _signal()
        assert risk.check_signal(sig, 10000.0, []) is True


class TestCooldown:
    def test_activates_after_consecutive_losses(self, risk: RiskManager):
        risk.record_pnl(-10.0)
        risk.record_pnl(-10.0)
        risk.record_pnl(-10.0)  # 3 consecutive losses
        assert risk._in_cooldown is True
        sig = _signal()
        assert risk.check_signal(sig, 10000.0, []) is False

    def test_resets_on_win(self, risk: RiskManager):
        risk.record_pnl(-10.0)
        risk.record_pnl(-10.0)
        risk.record_pnl(50.0)  # win breaks the streak
        assert risk._in_cooldown is False
        assert risk._consecutive_losses == 0


class TestSignalStrength:
    def test_rejects_weak_signal(self, risk: RiskManager):
        sig = _signal(strength=0.2)
        assert risk.check_signal(sig, 10000.0, []) is False

    def test_accepts_strong_signal(self, risk: RiskManager):
        sig = _signal(strength=0.8)
        assert risk.check_signal(sig, 10000.0, []) is True


class TestConcurrentPositions:
    def test_blocks_at_max_positions(self, risk: RiskManager):
        positions = [
            Position(symbol=f"COIN{i}/USDT", side=OrderSide.BUY,
                     amount=1.0, entry_price=10.0, current_price=10.0)
            for i in range(5)
        ]
        sig = _signal()
        assert risk.check_signal(sig, 10000.0, positions) is False

    def test_allows_below_max(self, risk: RiskManager):
        positions = [
            Position(symbol="ETH/USDT", side=OrderSide.BUY,
                     amount=1.0, entry_price=10.0, current_price=10.0)
        ]
        sig = _signal()
        assert risk.check_signal(sig, 10000.0, positions) is True


class TestCloseAlwaysAllowed:
    def test_close_passes_even_in_cooldown(self, risk: RiskManager):
        risk._in_cooldown = True
        sig = _signal(action=SignalAction.CLOSE)
        assert risk.check_signal(sig, 10000.0, []) is True


class TestPositionSizing:
    def test_scales_down_with_losses(self, risk: RiskManager):
        size_before = risk.calculate_position_size(10000.0, 50000.0, leverage=10)
        risk.record_pnl(-200.0)  # 2% daily loss
        size_after = risk.calculate_position_size(10000.0, 50000.0, leverage=10)
        assert size_after < size_before

    def test_zero_price_returns_zero(self, risk: RiskManager):
        assert risk.calculate_position_size(10000.0, 0.0) == 0.0

    def test_leverage_increases_size(self, risk: RiskManager):
        size_1x = risk.calculate_position_size(10000.0, 50000.0, leverage=1)
        size_10x = risk.calculate_position_size(10000.0, 50000.0, leverage=10)
        assert size_10x > size_1x


class TestLiquidationCheck:
    def test_detects_near_liquidation(self, risk: RiskManager):
        # 10x leverage: liq threshold = -100/10 * 0.9 = -9%
        # pnl_pct at entry=50000, current=45400, 10x = (45400-50000)/50000*100*10 = -9.2%
        pos = Position(
            symbol="BTC/USDT", side=OrderSide.BUY,
            amount=1.0, entry_price=50000.0, current_price=45400.0,
            leverage=10,
        )
        assert risk.check_liquidation(pos, 10000.0) is True

    def test_no_liquidation_at_1x(self, risk: RiskManager):
        # 1x leverage: liq threshold = -100/1 * 0.9 = -90%, so -50% is fine
        pos = Position(
            symbol="BTC/USDT", side=OrderSide.BUY,
            amount=1.0, entry_price=50000.0, current_price=25000.0,
            leverage=1,
        )
        assert risk.check_liquidation(pos, 10000.0) is False
