"""Tests for shared/ (models, state)."""

from __future__ import annotations

import json

import pytest

from shared.models import (
    AnalyticsSnapshot,
    BotDeploymentStatus,
    DeploymentLevel,
    IntelSnapshot,
    StrategyWeightEntry,
    TrendingSnapshot,
    TVSymbolSnapshot,
)
from shared.state import SharedState

# ── Shared Models ───────────────────────────────────────────────────


class TestBotDeploymentStatus:
    def test_defaults(self):
        s = BotDeploymentStatus()
        assert s.level == DeploymentLevel.HUNTING
        assert s.has_capacity is True
        assert s.is_idle is True

    def test_has_capacity_false(self):
        s = BotDeploymentStatus(open_positions=3, max_positions=3)
        assert s.has_capacity is False

    def test_not_idle(self):
        s = BotDeploymentStatus(open_positions=1)
        assert s.is_idle is False


class TestIntelSnapshot:
    def test_defaults(self):
        snap = IntelSnapshot()
        assert snap.regime == "normal"
        assert snap.fear_greed == 50
        assert snap.tv_analyses == []

    def test_with_tv_analyses(self):
        tv = TVSymbolSnapshot(symbol="BTC/USDT", rating="STRONG_BUY", confidence=0.8)
        snap = IntelSnapshot(tv_analyses=[tv])
        assert len(snap.tv_analyses) == 1
        assert snap.tv_analyses[0].rating == "STRONG_BUY"


class TestAnalyticsSnapshot:
    def test_defaults(self):
        snap = AnalyticsSnapshot()
        assert snap.weights == []
        assert snap.total_trades_logged == 0

    def test_with_weights(self):
        w = StrategyWeightEntry(strategy="rsi", weight=1.2, win_rate=0.6, total_trades=50)
        snap = AnalyticsSnapshot(weights=[w])
        assert snap.weights[0].weight == 1.2


class TestTrendingSnapshot:
    def test_creation(self):
        t = TrendingSnapshot(symbol="DOGE/USDT", change_24h=15.0, source="cmc")
        assert t.source == "cmc"
        assert t.change_24h == 15.0


class TestDeploymentLevel:
    def test_values(self):
        assert DeploymentLevel.HUNTING == "hunting"
        assert DeploymentLevel.STRESSED == "stressed"


# ── SharedState ─────────────────────────────────────────────────────


class TestSharedState:
    @pytest.fixture()
    def state(self, tmp_path):
        return SharedState(data_dir=tmp_path)

    def test_write_and_read_bot_status(self, state):
        status = BotDeploymentStatus(level=DeploymentLevel.ACTIVE, open_positions=2)
        state.write_bot_status(status)
        read = state.read_bot_status()
        assert read.level == DeploymentLevel.ACTIVE
        assert read.open_positions == 2

    def test_read_bot_status_default(self, state):
        s = state.read_bot_status()
        assert s.level == DeploymentLevel.HUNTING

    def test_write_and_read_intel(self, state):
        intel = IntelSnapshot(regime="risk_off", fear_greed=20)
        state.write_intel(intel)
        read = state.read_intel()
        assert read.regime == "risk_off"
        assert read.fear_greed == 20

    def test_read_intel_default(self, state):
        i = state.read_intel()
        assert i.regime == "normal"

    def test_write_and_read_analytics(self, state):
        analytics = AnalyticsSnapshot(total_trades_logged=42)
        state.write_analytics(analytics)
        read = state.read_analytics()
        assert read.total_trades_logged == 42

    def test_read_analytics_default(self, state):
        a = state.read_analytics()
        assert a.total_trades_logged == 0

    def test_intel_age_seconds_no_file(self, state):
        age = state.intel_age_seconds()
        assert age >= 999999

    def test_intel_age_seconds_recent(self, state):
        intel = IntelSnapshot()
        state.write_intel(intel)
        age = state.intel_age_seconds()
        assert age < 5

    def test_corrupt_file_returns_none(self, state):
        path = state._data_dir / "bot_status.json"
        path.write_text("not json{{{")
        s = state.read_bot_status()
        assert s.level == DeploymentLevel.HUNTING

    def test_atomic_write(self, state):
        status = BotDeploymentStatus(level=DeploymentLevel.DEPLOYED)
        state.write_bot_status(status)
        raw = (state._data_dir / "bot_status.json").read_text()
        data = json.loads(raw)
        assert data["level"] == "deployed"
