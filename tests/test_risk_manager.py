"""Tests for the risk manager.

This module gates every single trade. If it's broken, the bot either
takes trades it shouldn't (catastrophic) or misses everything (annoying
but safe). We test both directions.
"""

import pytest

from config.settings import Settings
from core.models import OrderSide, Position, Signal, SignalAction
from core.risk.manager import RiskManager


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("TRADING_MODE", "aggressive-test")
    monkeypatch.setenv("EXCHANGE", "bybit")
    monkeypatch.setenv("MAX_POSITION_SIZE_PCT", "5.0")
    monkeypatch.setenv("MAX_DAILY_LOSS_PCT", "100.0")
    monkeypatch.setenv("STOP_LOSS_PCT", "2.0")
    monkeypatch.setenv("TAKE_PROFIT_PCT", "4.0")
    monkeypatch.setenv("MAX_CONCURRENT_POSITIONS", "15")
    monkeypatch.setenv("MIN_SIGNAL_STRENGTH", "0.2")
    monkeypatch.setenv("CONSECUTIVE_LOSS_COOLDOWN", "999")
    monkeypatch.setenv("RISK_ENV_MULTIPLIER", "1.0")
    return Settings(_env_file=None)


@pytest.fixture
def risk(settings: Settings) -> RiskManager:
    rm = RiskManager(settings)
    rm.reset_daily(10000.0)
    return rm


def _signal(action=SignalAction.BUY, strength=0.7, price=100.0) -> Signal:
    return Signal(
        symbol="BTC/USDT",
        action=action,
        strategy="test",
        strength=strength,
        suggested_price=price,
    )


class TestPaperLocalAggressive:
    """Environment-configured aggressive profile uses relaxed risk for bug-finding."""

    def test_daily_loss_does_not_block(self, risk: RiskManager):
        risk.record_pnl(-350.0)  # 3.5% loss — would block in live, not in paper_local
        sig = _signal()
        assert risk.check_signal(sig, 10000.0, []) is True

    def test_no_cooldown_after_losses(self, risk: RiskManager):
        for _ in range(5):
            risk.record_pnl(-10.0)
        assert risk._in_cooldown is False  # cooldown threshold is 999

    def test_weak_signal_accepted(self, risk: RiskManager):
        sig = _signal(strength=0.2)  # min_strength=0.2 in paper_local
        assert risk.check_signal(sig, 10000.0, []) is True

    def test_many_positions_allowed(self, risk: RiskManager):
        positions = [
            Position(symbol=f"COIN{i}/USDT", side=OrderSide.BUY, amount=1.0, entry_price=10.0, current_price=10.0)
            for i in range(5)
        ]
        sig = _signal()
        assert risk.check_signal(sig, 10000.0, positions) is True  # max_concurrent=10

    def test_aggressive_params_set(self, risk: RiskManager):
        assert risk.max_daily_loss_pct == 100.0
        assert risk.max_concurrent == 15
        assert risk.min_strength == 0.2
        assert risk.loss_cooldown_threshold == 999


class TestPaperRelaxedDisabled:
    """Strict profile uses conservative baseline params."""

    @pytest.fixture
    def strict_risk(self, monkeypatch: pytest.MonkeyPatch) -> RiskManager:
        monkeypatch.setenv("EXCHANGE", "bybit")
        monkeypatch.setenv("MAX_DAILY_LOSS_PCT", "3.0")
        monkeypatch.setenv("MAX_CONCURRENT_POSITIONS", "5")
        monkeypatch.setenv("MIN_SIGNAL_STRENGTH", "0.4")
        monkeypatch.setenv("CONSECUTIVE_LOSS_COOLDOWN", "3")
        monkeypatch.setenv("RISK_ENV_MULTIPLIER", "1.0")
        s = Settings(_env_file=None)
        rm = RiskManager(s)
        rm.reset_daily(10000.0)
        return rm

    def test_uses_prod_params(self, strict_risk: RiskManager):
        assert strict_risk.max_daily_loss_pct == 3.0
        assert strict_risk.max_concurrent == 5
        assert strict_risk.min_strength == 0.4
        assert strict_risk.loss_cooldown_threshold == 3

    def test_daily_loss_blocks(self, strict_risk: RiskManager):
        strict_risk.record_pnl(-350.0)
        sig = _signal()
        assert strict_risk.check_signal(sig, 10000.0, []) is False

    def test_cooldown_activates(self, strict_risk: RiskManager):
        strict_risk.record_pnl(-10.0)
        strict_risk.record_pnl(-10.0)
        strict_risk.record_pnl(-10.0)
        assert strict_risk._in_cooldown is True


class TestConservativeMode:
    """Strict profile enforces conservative risk limits."""

    @pytest.fixture
    def live_risk(self, monkeypatch: pytest.MonkeyPatch) -> RiskManager:
        monkeypatch.setenv("EXCHANGE", "bybit")
        monkeypatch.setenv("MAX_DAILY_LOSS_PCT", "3.0")
        monkeypatch.setenv("MAX_CONCURRENT_POSITIONS", "5")
        monkeypatch.setenv("MIN_SIGNAL_STRENGTH", "0.4")
        monkeypatch.setenv("CONSECUTIVE_LOSS_COOLDOWN", "3")
        monkeypatch.setenv("RISK_ENV_MULTIPLIER", "1.0")
        s = Settings(_env_file=None)
        rm = RiskManager(s)
        rm.reset_daily(10000.0)
        return rm

    def test_daily_loss_blocks(self, live_risk: RiskManager):
        live_risk.record_pnl(-350.0)
        sig = _signal()
        assert live_risk.check_signal(sig, 10000.0, []) is False

    def test_cooldown_activates(self, live_risk: RiskManager):
        live_risk.record_pnl(-10.0)
        live_risk.record_pnl(-10.0)
        live_risk.record_pnl(-10.0)
        assert live_risk._in_cooldown is True

    def test_weak_signal_rejected(self, live_risk: RiskManager):
        sig = _signal(strength=0.2)
        assert live_risk.check_signal(sig, 10000.0, []) is False

    def test_max_positions_blocks(self, live_risk: RiskManager):
        positions = [
            Position(symbol=f"COIN{i}/USDT", side=OrderSide.BUY, amount=1.0, entry_price=10.0, current_price=10.0)
            for i in range(5)
        ]
        sig = _signal()
        assert live_risk.check_signal(sig, 10000.0, positions) is False

    def test_allows_when_within_limits(self, live_risk: RiskManager):
        sig = _signal(strength=0.8)
        assert live_risk.check_signal(sig, 10000.0, []) is True

    def test_resets_on_win(self, live_risk: RiskManager):
        live_risk.record_pnl(-10.0)
        live_risk.record_pnl(-10.0)
        live_risk.record_pnl(50.0)
        assert live_risk._in_cooldown is False
        assert live_risk._consecutive_losses == 0


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


class TestProfitBufferExpandsLimit:
    """Profit buffer from a big day should expand the daily loss limit."""

    @pytest.fixture
    def live_risk(self, monkeypatch: pytest.MonkeyPatch) -> RiskManager:
        monkeypatch.setenv("EXCHANGE", "bybit")
        monkeypatch.setenv("MAX_DAILY_LOSS_PCT", "3.0")
        monkeypatch.setenv("RISK_ENV_MULTIPLIER", "1.0")
        s = Settings(_env_file=None)
        rm = RiskManager(s)
        rm.reset_daily(10000.0)
        return rm

    def test_no_buffer_uses_base(self, live_risk: RiskManager):
        live_risk.reset_daily(10000.0, profit_buffer_pct=0.0)
        assert live_risk.max_daily_loss_pct == pytest.approx(3.0)

    def test_buffer_expands_limit(self, live_risk: RiskManager):
        live_risk.reset_daily(10000.0, profit_buffer_pct=8.0)
        assert live_risk.max_daily_loss_pct == pytest.approx(3.0 + 8.0 * 0.5)

    def test_buffer_capped_at_2x_base(self, live_risk: RiskManager):
        live_risk.reset_daily(10000.0, profit_buffer_pct=100.0)
        assert live_risk.max_daily_loss_pct == pytest.approx(3.0 + 6.0)

    def test_base_preserved(self, live_risk: RiskManager):
        live_risk.reset_daily(10000.0, profit_buffer_pct=10.0)
        assert live_risk._base_max_daily_loss_pct == pytest.approx(3.0)


class TestLiquidationCheck:
    def test_detects_near_liquidation(self, risk: RiskManager):
        # 10x leverage: liq threshold = -100/10 * 0.9 = -9%
        # pnl_pct at entry=50000, current=45400, 10x = (45400-50000)/50000*100*10 = -9.2%
        pos = Position(
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            amount=1.0,
            entry_price=50000.0,
            current_price=45400.0,
            leverage=10,
        )
        assert risk.check_liquidation(pos, 10000.0) is True

    def test_no_liquidation_at_1x(self, risk: RiskManager):
        # 1x leverage: liq threshold = -100/1 * 0.9 = -90%, so -50% is fine
        pos = Position(
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            amount=1.0,
            entry_price=50000.0,
            current_price=25000.0,
            leverage=1,
        )
        assert risk.check_liquidation(pos, 10000.0) is False
