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

    def test_bot_id_in_status(self, state):
        status = BotDeploymentStatus(bot_id="momentum", level=DeploymentLevel.ACTIVE)
        state.write_bot_status(status)
        read = state.read_bot_status()
        assert read.bot_id == "momentum"

    def test_multi_bot_data_isolation(self, tmp_path):
        """Two bots write to separate subdirs under the same root."""
        bot1 = SharedState(data_dir=tmp_path / "momentum")
        bot2 = SharedState(data_dir=tmp_path / "swing")
        bot1.write_bot_status(BotDeploymentStatus(bot_id="momentum", open_positions=3))
        bot2.write_bot_status(BotDeploymentStatus(bot_id="swing", open_positions=1))
        assert bot1.read_bot_status().open_positions == 3
        assert bot2.read_bot_status().open_positions == 1
        assert bot1.read_bot_status().bot_id == "momentum"
        assert bot2.read_bot_status().bot_id == "swing"

    def test_shared_intel_readable_from_bot_dir(self, tmp_path):
        """Intel written to root data/ is readable by a bot-specific state."""
        shared = SharedState(data_dir=tmp_path)
        shared.write_intel(IntelSnapshot(regime="risk_on", fear_greed=75))
        bot_state = SharedState(data_dir=tmp_path / "momentum")
        root_intel = shared.read_intel()
        assert root_intel.regime == "risk_on"
        bot_intel = bot_state.read_intel()
        assert bot_intel.regime == "normal"


class TestSharedStateTradeQueue:
    @pytest.fixture
    def state(self, tmp_path):
        return SharedState(data_dir=tmp_path)

    def test_write_and_read_trade_queue(self, state):
        from shared.models import SignalPriority, TradeProposal, TradeQueue

        q = TradeQueue()
        q.add(TradeProposal(priority=SignalPriority.DAILY, symbol="BTC/USDT", side="long", strength=0.8))
        q.add(TradeProposal(priority=SignalPriority.CRITICAL, symbol="ETH/USDT", side="short", strength=0.9))
        state.write_trade_queue(q)
        read_back = state.read_trade_queue()
        assert read_back.total == 2
        syms = {p.symbol for p in read_back.proposals}
        assert "BTC/USDT" in syms
        assert "ETH/USDT" in syms

    def test_write_bot_trade_queue_creates_subdir(self, state):
        from shared.models import SignalPriority, TradeProposal, TradeQueue

        q = TradeQueue()
        q.add(TradeProposal(priority=SignalPriority.DAILY, symbol="SOL/USDT", side="long", strength=0.7))
        state.write_bot_trade_queue("momentum", q)
        bot_file = state._data_dir / "momentum" / "trade_queue.json"
        assert bot_file.exists()
        data = json.loads(bot_file.read_text())
        assert len(data["proposals"]) == 1
        assert data["proposals"][0]["symbol"] == "SOL/USDT"

    def test_apply_trade_queue_updates_removes_consumed(self, state):
        from shared.models import SignalPriority, TradeProposal, TradeQueue

        q = TradeQueue()
        p = TradeProposal(priority=SignalPriority.DAILY, symbol="BTC/USDT", side="long", strength=0.8)
        q.add(p)
        state.write_trade_queue(q)

        state.apply_trade_queue_updates(consumed_ids=[p.id], rejected={})
        updated = state.read_trade_queue()
        assert updated.total == 0

    def test_apply_trade_queue_updates_removes_rejected(self, state):
        from shared.models import SignalPriority, TradeProposal, TradeQueue

        q = TradeQueue()
        p = TradeProposal(priority=SignalPriority.DAILY, symbol="ETH/USDT", side="short", strength=0.6)
        q.add(p)
        state.write_trade_queue(q)

        state.apply_trade_queue_updates(consumed_ids=[], rejected={p.id: "risk limit"})
        updated = state.read_trade_queue()
        assert updated.total == 0

    def test_apply_empty_updates_is_noop(self, state):
        from shared.models import SignalPriority, TradeProposal, TradeQueue

        q = TradeQueue()
        q.add(TradeProposal(priority=SignalPriority.DAILY, symbol="X/USDT", side="long", strength=0.5))
        state.write_trade_queue(q)
        state.apply_trade_queue_updates(consumed_ids=[], rejected={})
        updated = state.read_trade_queue()
        assert updated.total == 1


class TestSharedStateBotDiscovery:
    def test_read_all_bot_statuses_discovers_bots(self, tmp_path):
        root = SharedState(data_dir=tmp_path)
        for name in ("momentum", "meanrev", "swing"):
            sub = SharedState(data_dir=tmp_path / name)
            sub.write_bot_status(BotDeploymentStatus(bot_id=name, open_positions=1))

        statuses = root.read_all_bot_statuses()
        ids = {s.bot_id for s in statuses}
        assert ids == {"momentum", "meanrev", "swing"}

    def test_read_all_bot_statuses_ignores_files(self, tmp_path):
        root = SharedState(data_dir=tmp_path)
        (tmp_path / "random_file.txt").write_text("hi")
        sub = SharedState(data_dir=tmp_path / "momentum")
        sub.write_bot_status(BotDeploymentStatus(bot_id="momentum"))
        statuses = root.read_all_bot_statuses()
        assert len(statuses) == 1

    def test_read_all_bot_statuses_skips_empty_id(self, tmp_path):
        root = SharedState(data_dir=tmp_path)
        sub = SharedState(data_dir=tmp_path / "broken")
        sub.write_bot_status(BotDeploymentStatus(bot_id=""))
        statuses = root.read_all_bot_statuses()
        assert len(statuses) == 0


class TestSharedStateIntelAge:
    def test_intel_age_missing_file(self, tmp_path):
        state = SharedState(data_dir=tmp_path)
        assert state.intel_age_seconds() > 999998

    def test_intel_age_fresh(self, tmp_path):
        from datetime import UTC, datetime

        state = SharedState(data_dir=tmp_path)
        snap = IntelSnapshot(regime="normal")
        snap.updated_at = datetime.now(UTC).isoformat()
        state.write_intel(snap)
        age = state.intel_age_seconds()
        assert age < 5

    def test_intel_age_no_updated_at(self, tmp_path):
        state = SharedState(data_dir=tmp_path)
        path = tmp_path / "intel_state.json"
        path.write_text(json.dumps({"regime": "normal", "updated_at": ""}))
        age = state.intel_age_seconds()
        assert age > 999998


class TestSharedStateAnalytics:
    def test_write_and_read_analytics(self, tmp_path):
        state = SharedState(data_dir=tmp_path)
        snap = AnalyticsSnapshot(weights=[StrategyWeightEntry(strategy="momentum", weight=1.2)])
        state.write_analytics(snap)
        read = state.read_analytics()
        assert len(read.weights) == 1
        assert read.weights[0].strategy == "momentum"
        assert read.weights[0].weight == 1.2

    def test_read_analytics_missing_returns_default(self, tmp_path):
        state = SharedState(data_dir=tmp_path)
        read = state.read_analytics()
        assert read.weights == []


class TestHubDBExchangeSymbols:
    """Tests for HubDB.save_exchange_symbols / load_all_exchange_symbols."""

    @pytest.fixture
    def hub(self, tmp_path):
        from db.hub_store import HubDB

        db = HubDB(path=tmp_path / "test_sym.db")
        db.connect()
        yield db
        db.close()

    def test_save_and_load(self, hub):
        hub.save_exchange_symbols("BINANCE", {"BTC/USDT", "ETH/USDT"})
        result = hub.load_all_exchange_symbols()
        assert "BINANCE" in result
        assert "BTC/USDT" in result["BINANCE"]
        assert "ETH/USDT" in result["BINANCE"]

    def test_upsert_replaces(self, hub):
        hub.save_exchange_symbols("BINANCE", {"BTC/USDT"})
        hub.save_exchange_symbols("BINANCE", {"ETH/USDT", "SOL/USDT"})
        result = hub.load_all_exchange_symbols()
        assert result["BINANCE"] == {"ETH/USDT", "SOL/USDT"}

    def test_multiple_exchanges(self, hub):
        hub.save_exchange_symbols("BINANCE", {"BTC/USDT"})
        hub.save_exchange_symbols("MEXC", {"BTC/USDT", "PEPE/USDT"})
        result = hub.load_all_exchange_symbols()
        assert "BINANCE" in result
        assert "MEXC" in result
        assert "PEPE/USDT" not in result["BINANCE"]
        assert "PEPE/USDT" in result["MEXC"]

    def test_load_empty_returns_empty(self, hub):
        result = hub.load_all_exchange_symbols()
        assert result == {}


class TestHubBotEnabled:
    """Tests for HubDB.set_bot_enabled / is_bot_enabled / get_all_bot_enabled."""

    @pytest.fixture
    def hub(self, tmp_path):
        from db.hub_store import HubDB

        db = HubDB(path=tmp_path / "test_hub.db")
        db.connect()
        yield db
        db.close()

    def test_default_enabled(self, hub):
        assert hub.is_bot_enabled("nonexistent") is True

    def test_set_and_read_enabled(self, hub):
        hub.set_bot_enabled("momentum", True)
        assert hub.is_bot_enabled("momentum") is True

    def test_set_and_read_disabled(self, hub):
        hub.set_bot_enabled("meanrev", False)
        assert hub.is_bot_enabled("meanrev") is False

    def test_toggle_overwrites(self, hub):
        hub.set_bot_enabled("bot1", True)
        assert hub.is_bot_enabled("bot1") is True
        hub.set_bot_enabled("bot1", False)
        assert hub.is_bot_enabled("bot1") is False

    def test_get_all_bot_enabled(self, hub):
        hub.set_bot_enabled("a", True)
        hub.set_bot_enabled("b", False)
        hub.set_bot_enabled("c", True)
        result = hub.get_all_bot_enabled()
        assert result == {"a": True, "b": False, "c": True}

    def test_get_all_empty(self, hub):
        assert hub.get_all_bot_enabled() == {}
