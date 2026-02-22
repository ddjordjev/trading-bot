"""Tests for hub/state.py — in-memory state management."""

from __future__ import annotations

import pytest

from hub.state import HubState
from shared.models import (
    AnalyticsSnapshot,
    BotDeploymentStatus,
    ExtremeWatchlist,
    IntelSnapshot,
    SignalPriority,
    StrategyWeightEntry,
    TradeProposal,
    TradeQueue,
)


@pytest.fixture
def state(tmp_path):
    return HubState(data_dir=tmp_path)


class TestHubStateIntel:
    def test_write_and_read_intel(self, state):
        snap = IntelSnapshot(sources_active=["fear_greed"], fear_greed=42)
        state.write_intel(snap)
        read = state.read_intel()
        assert read.fear_greed == 42
        assert read.updated_at is not None

    def test_intel_age_fresh(self, state):
        snap = IntelSnapshot()
        state.write_intel(snap)
        age = state.intel_age_seconds()
        assert age < 5

    def test_intel_age_default_is_fresh(self, state):
        """Default IntelSnapshot sets updated_at to now, so age should be tiny."""
        age = state.intel_age_seconds()
        assert age < 5


class TestHubStateAnalytics:
    def test_write_and_read_analytics(self, state):
        snap = AnalyticsSnapshot()
        state.write_analytics(snap)
        read = state.read_analytics()
        assert read.updated_at is not None

    def test_analytics_persisted_to_disk(self, tmp_path):
        s1 = HubState(data_dir=tmp_path)
        snap = AnalyticsSnapshot(
            weights=[
                StrategyWeightEntry(strategy="momentum", weight=1.2, win_rate=0.65, total_trades=40, total_pnl=120.0),
                StrategyWeightEntry(strategy="meanrev", weight=0.8, win_rate=0.45, total_trades=20, total_pnl=-15.0),
            ],
            patterns=[{"pattern_type": "time_of_day", "description": "test"}],
            suggestions=[{"strategy": "meanrev", "suggestion_type": "reduce_weight"}],
            total_trades_logged=60,
        )
        s1.write_analytics(snap)

        assert (tmp_path / "analytics_state.json").exists()

        s2 = HubState(data_dir=tmp_path)
        loaded = s2.read_analytics()
        assert len(loaded.weights) == 2
        assert loaded.weights[0].strategy == "momentum"
        assert loaded.weights[0].weight == 1.2
        assert loaded.weights[1].strategy == "meanrev"
        assert len(loaded.patterns) == 1
        assert len(loaded.suggestions) == 1
        assert loaded.total_trades_logged == 60

    def test_analytics_loads_empty_when_no_file(self, tmp_path):
        s = HubState(data_dir=tmp_path)
        loaded = s.read_analytics()
        assert loaded.weights == []
        assert loaded.total_trades_logged == 0

    def test_analytics_handles_corrupt_file(self, tmp_path):
        (tmp_path / "analytics_state.json").write_text("NOT VALID JSON {{{{")
        s = HubState(data_dir=tmp_path)
        loaded = s.read_analytics()
        assert loaded.weights == []


class TestHubStateExtremeWatchlist:
    def test_write_and_read_watchlist(self, state):
        wl = ExtremeWatchlist()
        state.write_extreme_watchlist(wl)
        read = state.read_extreme_watchlist()
        assert read.updated_at is not None


class TestHubStateBotStatus:
    def test_write_and_read_single_bot(self, state):
        bs = BotDeploymentStatus(bot_id="momentum")
        state.write_bot_status(bs)
        read = state.read_bot_status()
        assert read.bot_id == "momentum"

    def test_read_all_bot_statuses(self, state):
        state.write_bot_status(BotDeploymentStatus(bot_id="momentum"))
        state.write_bot_status(BotDeploymentStatus(bot_id="swing"))
        all_statuses = state.read_all_bot_statuses()
        assert len(all_statuses) == 2

    def test_read_bot_status_default(self, state):
        bs = state.read_bot_status()
        assert isinstance(bs, BotDeploymentStatus)


class TestHubStateExchangeSymbols:
    def test_write_and_read_symbols(self, state):
        state.write_exchange_symbols("bot1", "binance", ["BTC/USDT", "ETH/USDT"])
        result = state.read_all_exchange_symbols()
        assert "BINANCE" in result
        assert "BTC/USDT" in result["BINANCE"]

    def test_merge_symbols_from_multiple_bots(self, state):
        state.write_exchange_symbols("bot1", "binance", ["BTC/USDT"])
        state.write_exchange_symbols("bot2", "binance", ["ETH/USDT"])
        result = state.read_all_exchange_symbols()
        assert result["BINANCE"] == {"BTC/USDT", "ETH/USDT"}


class TestHubStateTradeQueue:
    def test_write_and_read_queue(self, state):
        q = TradeQueue()
        q.critical.append(
            TradeProposal(
                priority=SignalPriority.CRITICAL,
                symbol="BTC/USDT",
                side="long",
                strategy="momentum",
                reason="test",
                strength=0.9,
                market_type="futures",
            )
        )
        state.write_trade_queue(q)
        read = state.read_trade_queue()
        assert read.pending_count == 1

    def test_apply_trade_queue_updates_consumes(self, state):
        p = TradeProposal(
            priority=SignalPriority.CRITICAL,
            symbol="BTC/USDT",
            side="long",
            strategy="m",
            reason="r",
            strength=0.8,
            market_type="futures",
        )
        q = TradeQueue()
        q.critical.append(p)
        state.write_trade_queue(q)

        state.apply_trade_queue_updates(consumed_ids=[p.id], rejected={})
        read = state.read_trade_queue()
        assert read.pending_count == 0

    def test_read_queue_for_bot_style_filters(self, state):
        p1 = TradeProposal(
            priority=SignalPriority.CRITICAL,
            symbol="BTC/USDT",
            side="long",
            strategy="m",
            reason="r",
            strength=0.8,
            market_type="futures",
            target_bot="momentum,extreme",
        )
        p2 = TradeProposal(
            priority=SignalPriority.CRITICAL,
            symbol="ETH/USDT",
            side="long",
            strategy="s",
            reason="r",
            strength=0.6,
            market_type="futures",
            target_bot="swing",
        )
        q = TradeQueue()
        q.critical.extend([p1, p2])
        state.write_trade_queue(q)

        momentum_q = state.read_queue_for_bot_style("momentum")
        assert len(momentum_q.critical) == 1
        assert momentum_q.critical[0].symbol == "BTC/USDT"

        swing_q = state.read_queue_for_bot_style("swing")
        assert len(swing_q.critical) == 1
        assert swing_q.critical[0].symbol == "ETH/USDT"

    def test_read_queue_for_bot_style_includes_untagged(self, state):
        p = TradeProposal(
            priority=SignalPriority.DAILY,
            symbol="SOL/USDT",
            side="long",
            strategy="x",
            reason="r",
            strength=0.5,
            market_type="futures",
            target_bot="",
        )
        q = TradeQueue()
        q.daily.append(p)
        state.write_trade_queue(q)

        result = state.read_queue_for_bot_style("momentum")
        assert len(result.daily) == 1

    def test_bot_queue_updates_affect_shared_queue(self, state):
        p = TradeProposal(
            priority=SignalPriority.CRITICAL,
            symbol="BTC/USDT",
            side="long",
            strategy="m",
            reason="r",
            strength=0.8,
            market_type="futures",
        )
        q = TradeQueue()
        q.critical.append(p)
        state.write_trade_queue(q)
        state.write_bot_trade_queue("bot1", TradeQueue(critical=[p]))

        state.apply_bot_queue_updates("bot1", consumed_ids=[p.id], rejected={})
        assert state.read_trade_queue().pending_count == 0
