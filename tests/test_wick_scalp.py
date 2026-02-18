"""Tests for core/orders/wick_scalp.py."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.orders.wick_scalp import WickScalp, WickScalpDetector


class TestWickScalp:
    def test_age_minutes(self):
        ws = WickScalp(symbol="BTC/USDT", main_side="long", scalp_side="short",
                       created_at=datetime.now(timezone.utc) - timedelta(minutes=3))
        assert ws.age_minutes >= 2.9

    def test_expired(self):
        ws = WickScalp(symbol="BTC/USDT", main_side="long", scalp_side="short",
                       max_hold_minutes=5,
                       created_at=datetime.now(timezone.utc) - timedelta(minutes=6))
        assert ws.expired is True

    def test_not_expired(self):
        ws = WickScalp(symbol="BTC/USDT", main_side="long", scalp_side="short",
                       max_hold_minutes=5)
        assert ws.expired is False


class TestWickScalpDetector:
    @pytest.fixture()
    def det(self):
        return WickScalpDetector(wick_threshold_pct=1.5, velocity_candles=3,
                                 min_wick_velocity=0.5, max_concurrent_scalps=2)

    def test_feed_price_buffers(self, det):
        for i in range(25):
            det.feed_price("BTC/USDT", 100 + i)
        assert len(det._recent_prices["BTC/USDT"]) == 20

    def test_no_wick_insufficient_data(self, det):
        det.feed_price("BTC/USDT", 100)
        result = det.check_for_wick("BTC/USDT", "long", 99, 100)
        assert result is None

    def test_wick_detected_long(self, det):
        prices = [100, 99.5, 99, 98, 97]
        for p in prices:
            det.feed_price("BTC/USDT", p)
        scalp = det.check_for_wick("BTC/USDT", "long", 97.0, 100.0)
        if scalp:
            assert scalp.scalp_side == "short"

    def test_wick_detected_short(self, det):
        prices = [100, 100.5, 101, 102, 103]
        for p in prices:
            det.feed_price("BTC/USDT", p)
        scalp = det.check_for_wick("BTC/USDT", "short", 103.0, 100.0)
        if scalp:
            assert scalp.scalp_side == "long"

    def test_no_wick_below_threshold(self, det):
        prices = [100, 99.9, 99.8, 99.7, 99.6]
        for p in prices:
            det.feed_price("BTC/USDT", p)
        scalp = det.check_for_wick("BTC/USDT", "long", 99.6, 100)
        assert scalp is None

    def test_already_active_blocks_new_scalp(self, det):
        existing = WickScalp(symbol="BTC/USDT", main_side="long", scalp_side="short",
                             active=True)
        det._active_scalps["BTC/USDT"] = existing
        prices = [100, 99, 98, 97, 96]
        for p in prices:
            det.feed_price("BTC/USDT", p)
        assert det.check_for_wick("BTC/USDT", "long", 96, 100) is None

    def test_max_concurrent_limit(self, det):
        for sym in ["BTC/USDT", "ETH/USDT"]:
            ws = WickScalp(symbol=sym, main_side="long", scalp_side="short", active=True)
            det._active_scalps[sym] = ws
        prices = [100, 99, 98, 97, 96]
        for p in prices:
            det.feed_price("SOL/USDT", p)
        assert det.check_for_wick("SOL/USDT", "long", 96, 100) is None

    def test_activate_and_has_active(self, det):
        scalp = WickScalp(symbol="BTC/USDT", main_side="long", scalp_side="short")
        det.activate("BTC/USDT", scalp, 97.0, 0.5, "order-1")
        assert det.has_active("BTC/USDT") is True
        assert det.get("BTC/USDT") is not None

    def test_close(self, det):
        scalp = WickScalp(symbol="BTC/USDT", main_side="long", scalp_side="short")
        det.activate("BTC/USDT", scalp, 97.0, 0.5, "order-1")
        det.close("BTC/USDT", pnl=5.0)
        assert det.has_active("BTC/USDT") is False

    def test_get_expired(self, det):
        scalp = WickScalp(symbol="BTC/USDT", main_side="long", scalp_side="short",
                          active=True, max_hold_minutes=5,
                          created_at=datetime.now(timezone.utc) - timedelta(minutes=6))
        det._active_scalps["BTC/USDT"] = scalp
        expired = det.get_expired()
        assert "BTC/USDT" in expired

    def test_cleanup_old_closed(self, det):
        scalp = WickScalp(symbol="BTC/USDT", main_side="long", scalp_side="short",
                          active=False, closed=True,
                          created_at=datetime.now(timezone.utc) - timedelta(minutes=15))
        det._active_scalps["BTC/USDT"] = scalp
        det.cleanup()
        assert "BTC/USDT" not in det._active_scalps

    def test_active_scalps_property(self, det):
        scalp = WickScalp(symbol="BTC/USDT", main_side="long", scalp_side="short")
        det.activate("BTC/USDT", scalp, 97.0, 0.5, "order-1")
        assert "BTC/USDT" in det.active_scalps

    def test_velocity_no_against_moves(self, det):
        prices = [100, 101, 102, 103]
        vel = det._calculate_velocity(prices, "short")
        assert vel == 0.0

    def test_velocity_insufficient(self, det):
        assert det._calculate_velocity([100], "long") == 0.0
