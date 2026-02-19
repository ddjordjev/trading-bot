"""Tests for position scaling math.

Wrong scaling = wrong position sizes = either risking too much
or under-allocating on good setups. The $50 initial / $100K cap
logic must be exact.
"""

import pytest

from core.orders.scaler import PositionScaler, ScaleMode, ScalePhase


@pytest.fixture
def scaler():
    return PositionScaler(
        initial_risk_amount=50.0,
        max_notional=100_000.0,
        gambling_budget_pct=2.0,
    )


class TestInitialEntry:
    def test_initial_amount_at_price(self, scaler: PositionScaler):
        sp = scaler.create(
            symbol="BTC/USDT",
            side="long",
            strategy="test",
            leverage=10,
            mode=ScaleMode.PYRAMID,
        )
        amount = sp.get_initial_amount(50000.0)
        raw_cost = amount * 50000.0
        # PYRAMID starts at leverage//5 = 2x, so notional = $50 * 2 = $100
        expected_notional = 50.0 * sp.current_leverage
        assert raw_cost == pytest.approx(expected_notional, rel=0.01)

    def test_initial_phase(self, scaler: PositionScaler):
        sp = scaler.create(
            symbol="ETH/USDT",
            side="long",
            strategy="test",
            leverage=10,
        )
        assert sp.phase == ScalePhase.INITIAL

    def test_pyramid_starts_low_leverage(self, scaler: PositionScaler):
        sp = scaler.create(
            symbol="SOL/USDT",
            side="long",
            strategy="test",
            leverage=50,
            mode=ScaleMode.PYRAMID,
        )
        assert sp.initial_leverage < 50
        assert sp.initial_leverage >= 1


class TestNotionalCap:
    def test_notional_tracks_correctly(self, scaler: PositionScaler):
        sp = scaler.create(
            symbol="BTC/USDT",
            side="long",
            strategy="test",
            leverage=10,
            mode=ScaleMode.PYRAMID,
        )
        sp.current_size = 1.0
        sp.avg_entry_price = 50000.0
        sp.current_leverage = 10
        assert sp.notional_value == 500_000.0

    def test_has_room_to_add(self, scaler: PositionScaler):
        sp = scaler.create(
            symbol="BTC/USDT",
            side="long",
            strategy="test",
            leverage=10,
            mode=ScaleMode.PYRAMID,
        )
        sp.current_size = 0.001
        sp.avg_entry_price = 50000.0
        sp.current_leverage = 10
        assert sp.has_room_to_add is True

    def test_no_room_when_capped(self, scaler: PositionScaler):
        sp = scaler.create(
            symbol="BTC/USDT",
            side="long",
            strategy="test",
            leverage=10,
            mode=ScaleMode.PYRAMID,
        )
        sp.current_size = 200.0
        sp.avg_entry_price = 50000.0
        sp.current_leverage = 10
        assert sp.has_room_to_add is False


class TestGamblingSize:
    def test_gambling_is_tiny(self, scaler: PositionScaler):
        size = scaler.gambling_size(balance=10000.0, price=50000.0, leverage=10)
        notional = size * 50000.0
        # gambling_size: capital = balance * budget_pct, notional = capital * leverage
        max_notional = 10000.0 * 0.02 * 10  # 2% * 10x = $2000
        assert notional <= max_notional
        # but the actual capital risked (notional / leverage) should be small
        capital_at_risk = notional / 10
        assert capital_at_risk <= 10000.0 * 0.02
