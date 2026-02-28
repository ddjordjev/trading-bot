"""Tests for volatility/detector.py."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.models import Ticker
from volatility.detector import SpikeEvent, VolatilityDetector


@pytest.fixture()
def settings(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper_local")
    monkeypatch.setenv("EXCHANGE", "bybit")
    monkeypatch.setenv("SPIKE_THRESHOLD_PCT", "3.0")
    monkeypatch.setenv("VOLATILITY_LOOKBACK_MINUTES", "5")
    from config.settings import Settings

    return Settings()


def _ticker(symbol="BTC/USDT", last=100.0, ts=None) -> Ticker:
    return Ticker(
        symbol=symbol,
        bid=last - 0.5,
        ask=last + 0.5,
        last=last,
        volume_24h=1e6,
        change_pct_24h=1.0,
        timestamp=ts or datetime.now(UTC),
    )


class TestSpikeEvent:
    def test_creation(self):
        e = SpikeEvent(symbol="BTC/USDT", direction="up", change_pct=5.0, price=105, volume_24h=1e6, window_seconds=60)
        assert e.direction == "up"
        assert e.confirmed_by_news is False


class TestVolatilityDetector:
    def test_first_update_no_spike(self, settings):
        vd = VolatilityDetector(settings)
        result = vd.update(_ticker(last=100))
        assert result is None

    def test_spike_detected_up(self, settings):
        vd = VolatilityDetector(settings)
        now = datetime.now(UTC)
        vd.update(_ticker(last=100, ts=now - timedelta(seconds=10)))
        result = vd.update(_ticker(last=104, ts=now))
        assert result is not None
        assert result.direction == "up"
        assert result.change_pct >= 3.0

    def test_spike_detected_down(self, settings):
        vd = VolatilityDetector(settings)
        now = datetime.now(UTC)
        vd.update(_ticker(last=100, ts=now - timedelta(seconds=10)))
        result = vd.update(_ticker(last=96, ts=now))
        assert result is not None
        assert result.direction == "down"

    def test_no_spike_small_move(self, settings):
        vd = VolatilityDetector(settings)
        now = datetime.now(UTC)
        vd.update(_ticker(last=100, ts=now - timedelta(seconds=10)))
        result = vd.update(_ticker(last=101, ts=now))
        assert result is None

    def test_cooldown_prevents_duplicate(self, settings):
        vd = VolatilityDetector(settings)
        now = datetime.now(UTC)
        vd.update(_ticker(last=100, ts=now - timedelta(seconds=10)))
        vd.update(_ticker(last=104, ts=now))
        result = vd.update(_ticker(last=108, ts=now + timedelta(seconds=5)))
        assert result is None

    def test_zero_oldest_price(self, settings):
        vd = VolatilityDetector(settings)
        now = datetime.now(UTC)
        vd.update(_ticker(last=0, ts=now - timedelta(seconds=10)))
        result = vd.update(_ticker(last=104, ts=now))
        assert result is None

    def test_get_recent_spikes(self, settings):
        vd = VolatilityDetector(settings)
        now = datetime.now(UTC)
        vd.update(_ticker(last=100, ts=now - timedelta(seconds=10)))
        vd.update(_ticker(last=104, ts=now))
        spikes = vd.get_recent_spikes()
        assert len(spikes) == 1

    def test_is_volatile(self, settings):
        vd = VolatilityDetector(settings)
        now = datetime.now(UTC)
        vd.update(_ticker(last=100, ts=now - timedelta(seconds=1)))
        vd.update(_ticker(last=110, ts=now))
        assert vd.is_volatile("BTC/USDT") is True

    def test_is_not_volatile_no_data(self, settings):
        vd = VolatilityDetector(settings)
        assert vd.is_volatile("BTC/USDT") is False

    def test_is_not_volatile_flat(self, settings):
        vd = VolatilityDetector(settings)
        now = datetime.now(UTC)
        vd.update(_ticker(last=100, ts=now - timedelta(seconds=1)))
        vd.update(_ticker(last=100.01, ts=now))
        assert vd.is_volatile("BTC/USDT") is False

    def test_is_volatile_custom_threshold(self, settings):
        vd = VolatilityDetector(settings)
        now = datetime.now(UTC)
        vd.update(_ticker(last=100, ts=now - timedelta(seconds=1)))
        vd.update(_ticker(last=101, ts=now))
        assert vd.is_volatile("BTC/USDT", threshold_pct=0.5) is True

    def test_is_volatile_zero_min(self, settings):
        vd = VolatilityDetector(settings)
        now = datetime.now(UTC)
        vd.update(_ticker(last=0, ts=now - timedelta(seconds=1)))
        vd.update(_ticker(last=100, ts=now))
        assert vd.is_volatile("BTC/USDT") is False
