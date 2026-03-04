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

    def test_analytics_persisted_to_db_and_reloaded(self, tmp_path):
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
        row = s1._hub_db.conn.execute(
            "SELECT COUNT(*) AS c FROM analytics_snapshots",
        ).fetchone()
        assert row is not None
        assert int(row["c"]) == 1

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

    def test_analytics_handles_corrupt_db_snapshot(self, tmp_path):
        s1 = HubState(data_dir=tmp_path)
        assert s1._hub_db.conn is not None
        s1._hub_db.conn.execute(
            """
            INSERT INTO analytics_snapshots (snapshot_json, total_trades_logged, updated_at)
            VALUES (?, ?, ?)
            """,
            ("NOT VALID JSON {{{{", 0, "2026-03-04T00:00:00+00:00"),
        )
        s1._hub_db.conn.commit()

        s = HubState(data_dir=tmp_path)
        loaded = s.read_analytics()
        assert loaded.weights == []

    def test_analytics_imports_legacy_json_once_when_db_empty(self, tmp_path):
        legacy = AnalyticsSnapshot(
            weights=[StrategyWeightEntry(strategy="momentum", weight=1.1)],
            total_trades_logged=7,
        )
        (tmp_path / "analytics_state.json").write_text(legacy.model_dump_json())

        s = HubState(data_dir=tmp_path)
        loaded = s.read_analytics()
        assert len(loaded.weights) == 1
        assert loaded.total_trades_logged == 7

        row = s._hub_db.conn.execute(
            "SELECT snapshot_json FROM analytics_snapshots ORDER BY id DESC LIMIT 1",
        ).fetchone()
        assert row is not None
        assert "momentum" in str(row["snapshot_json"])


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


class TestHubStateTradeQueue:
    def test_write_and_read_queue(self, state):
        q = TradeQueue()
        q.add(
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

    def test_serve_proposal_and_consume(self, state):
        """serve_proposal_to_bot picks and locks; handle_consume clears all exchanges."""
        p = TradeProposal(
            priority=SignalPriority.CRITICAL,
            symbol="BTC/USDT",
            side="long",
            strategy="m",
            reason="r",
            strength=0.8,
            market_type="futures",
            target_bot="momentum",
            supported_exchanges=["BINANCE", "BYBIT"],
        )
        q = TradeQueue()
        q.add(p)
        state.write_trade_queue(q)

        served = state.serve_proposal_to_bot("momentum", "bot1", "BINANCE")
        assert served is not None
        assert served.symbol == "BTC/USDT"
        assert state.read_trade_queue().proposals[0].is_locked

        state.handle_consume(p.id, "BINANCE", "bot1")
        remaining = state.read_trade_queue()
        assert remaining.total == 0

    def test_consume_removes_proposal_from_queue(self, state):
        """After consume, proposal is deleted — symbol protection shifts to open_db_symbols + active_symbols."""
        p = TradeProposal(
            priority=SignalPriority.CRITICAL,
            symbol="BTC/USDT",
            side="long",
            strategy="m",
            reason="r",
            strength=0.8,
            market_type="futures",
            supported_exchanges=["BINANCE"],
        )
        q = TradeQueue()
        q.add(p)
        state.write_trade_queue(q)

        state.handle_consume(p.id, "BINANCE", "bot1")
        remaining = state.read_trade_queue()
        assert remaining.total == 0
        assert not remaining.has_symbol("BTC/USDT")

    def test_serve_respects_bot_style_target(self, state):
        p1 = TradeProposal(
            priority=SignalPriority.CRITICAL,
            symbol="BTC/USDT",
            side="long",
            strategy="m",
            reason="r",
            strength=0.8,
            market_type="futures",
            target_bot="momentum",
            supported_exchanges=["BINANCE"],
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
            supported_exchanges=["BINANCE"],
        )
        q = TradeQueue()
        q.add(p1)
        q.add(p2)
        state.write_trade_queue(q)

        served = state.serve_proposal_to_bot("momentum", "bot1", "BINANCE")
        assert served is not None
        assert served.symbol == "BTC/USDT"

        state.handle_consume(p1.id, "BINANCE", "bot1")

        nothing = state.serve_proposal_to_bot("momentum", "bot2", "BINANCE")
        assert nothing is None

        swing = state.serve_proposal_to_bot("swing", "bot3", "BINANCE")
        assert swing is not None
        assert swing.symbol == "ETH/USDT"

    def test_serve_respects_allowed_priorities(self, state):
        p_daily = TradeProposal(
            priority=SignalPriority.DAILY,
            symbol="BTC/USDT",
            side="long",
            strategy="x",
            reason="r",
            strength=0.5,
            market_type="futures",
            supported_exchanges=["BINANCE"],
        )
        q = TradeQueue()
        q.add(p_daily)
        state.write_trade_queue(q)

        only_critical = state.serve_proposal_to_bot(
            "momentum", "bot1", "BINANCE", allowed_priorities=[SignalPriority.CRITICAL]
        )
        assert only_critical is None

        daily_ok = state.serve_proposal_to_bot("momentum", "bot1", "BINANCE", allowed_priorities=[SignalPriority.DAILY])
        assert daily_ok is not None

    def test_serve_filters_active_symbols(self, state):
        p = TradeProposal(
            priority=SignalPriority.CRITICAL,
            symbol="BTC/USDT",
            side="long",
            strategy="m",
            reason="r",
            strength=0.8,
            market_type="futures",
            supported_exchanges=["BINANCE"],
        )
        q = TradeQueue()
        q.add(p)
        state.write_trade_queue(q)

        state.update_bot_positions("other_bot", "BINANCE", {"BTC/USDT"})

        served = state.serve_proposal_to_bot("momentum", "bot1", "BINANCE")
        assert served is None

    def test_serve_filters_open_db_symbols(self, state):
        p = TradeProposal(
            priority=SignalPriority.CRITICAL,
            symbol="BTC/USDT",
            side="long",
            strategy="m",
            reason="r",
            strength=0.8,
            market_type="futures",
            supported_exchanges=["BINANCE"],
        )
        q = TradeQueue()
        q.add(p)
        state.write_trade_queue(q)

        served = state.serve_proposal_to_bot("momentum", "bot1", "BINANCE", open_db_symbols={"BTC/USDT"})
        assert served is None

    def test_rejection_tracking(self, state):
        p = TradeProposal(
            priority=SignalPriority.CRITICAL,
            symbol="BTC/USDT",
            side="long",
            strategy="m",
            reason="r",
            strength=0.8,
            market_type="futures",
            target_bot="momentum",
            supported_exchanges=["BINANCE"],
        )
        q = TradeQueue()
        q.add(p)
        state.write_trade_queue(q)

        state.handle_reject(p.id, "BINANCE", "bot1", "no free slots")
        history = state.get_rejection_history()
        assert "BTC/USDT|m" in history
        assert history["BTC/USDT|m"].count == 1

    def test_serve_blocks_locked_symbol(self, state):
        """When Bot A locks a ZRO/USDT proposal, Bot B cannot get any ZRO/USDT proposal."""
        p1 = TradeProposal(
            priority=SignalPriority.CRITICAL,
            symbol="ZRO/USDT",
            side="short",
            strategy="trending_momentum",
            reason="r",
            strength=0.8,
            market_type="futures",
            supported_exchanges=["BINANCE"],
        )
        p2 = TradeProposal(
            priority=SignalPriority.DAILY,
            symbol="ZRO/USDT",
            side="short",
            strategy="rsi",
            reason="r",
            strength=0.6,
            market_type="futures",
            supported_exchanges=["BINANCE"],
        )
        p3 = TradeProposal(
            priority=SignalPriority.CRITICAL,
            symbol="ETH/USDT",
            side="long",
            strategy="m",
            reason="r",
            strength=0.7,
            market_type="futures",
            supported_exchanges=["BINANCE"],
        )
        q = TradeQueue()
        q.add(p1)
        q.add(p2)
        q.add(p3)
        state.write_trade_queue(q)

        served_a = state.serve_proposal_to_bot("momentum", "bot-a", "BINANCE")
        assert served_a is not None
        assert served_a.symbol == "ZRO/USDT"

        served_b = state.serve_proposal_to_bot("momentum", "bot-b", "BINANCE")
        assert served_b is not None
        assert served_b.symbol == "ETH/USDT", "Bot B must NOT get ZRO/USDT while it's locked"

    def test_untagged_proposals_match_any_style(self, state):
        p = TradeProposal(
            priority=SignalPriority.DAILY,
            symbol="SOL/USDT",
            side="long",
            strategy="x",
            reason="r",
            strength=0.5,
            market_type="futures",
            target_bot="",
            supported_exchanges=["BINANCE"],
        )
        q = TradeQueue()
        q.add(p)
        state.write_trade_queue(q)

        result = state.serve_proposal_to_bot("momentum", "bot1", "BINANCE")
        assert result is not None
        assert result.symbol == "SOL/USDT"

    def test_target_bot_can_match_bot_id(self, state):
        p = TradeProposal(
            priority=SignalPriority.CRITICAL,
            symbol="NEWT/USDT",
            side="long",
            strategy="extreme_mover",
            reason="r",
            strength=0.9,
            market_type="futures",
            target_bot="extreme",
            supported_exchanges=["BINANCE"],
        )
        q = TradeQueue()
        q.add(p)
        state.write_trade_queue(q)

        # Extreme bot uses style=momentum, but its bot_id is "extreme".
        result = state.serve_proposal_to_bot("momentum", "extreme", "BINANCE")
        assert result is not None
        assert result.symbol == "NEWT/USDT"
