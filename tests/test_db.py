"""Tests for db/ (models, store, hub_store)."""

from __future__ import annotations

import pytest

from db.hub_store import HubDB
from db.models import ModificationSuggestion, PatternInsight, StrategyScore, TradeRecord
from db.store import TradeDB

# ── DB Models ───────────────────────────────────────────────────────


class TestTradeRecord:
    def test_defaults(self):
        tr = TradeRecord(symbol="BTC/USDT", side="buy", strategy="rsi", action="close")
        assert tr.pnl_usd == 0.0
        assert tr.is_winner is False
        assert tr.fear_greed == 50

    def test_full_record(self):
        tr = TradeRecord(
            symbol="ETH/USDT",
            side="sell",
            strategy="macd",
            action="close",
            pnl_usd=50,
            pnl_pct=2.5,
            is_winner=True,
            leverage=10,
            market_regime="risk_on",
            hour_utc=14,
        )
        assert tr.is_winner is True
        assert tr.leverage == 10


class TestStrategyScore:
    def test_defaults(self):
        ss = StrategyScore(strategy="rsi")
        assert ss.weight == 1.0
        assert ss.total_trades == 0

    def test_custom(self):
        ss = StrategyScore(strategy="macd", win_rate=0.65, profit_factor=2.1)
        assert ss.win_rate == 0.65


class TestPatternInsight:
    def test_creation(self):
        pi = PatternInsight(pattern_type="time_of_day", description="Bad at 3am", severity="warning")
        assert pi.severity == "warning"


class TestModificationSuggestion:
    def test_creation(self):
        ms = ModificationSuggestion(
            strategy="rsi",
            suggestion_type="change_param",
            title="Raise oversold threshold",
            description="RSI 25 instead of 30",
        )
        assert ms.suggestion_type == "change_param"


# ── TradeDB ─────────────────────────────────────────────────────────


class TestTradeDB:
    @pytest.fixture()
    def db(self, tmp_path):
        db = TradeDB(path=tmp_path / "test_trades.db")
        db.connect()
        yield db
        db.close()

    def _log_trade(self, db, symbol="BTC/USDT", strategy="rsi", is_winner=True, pnl_usd=10, pnl_pct=2.0, hour=14):
        tr = TradeRecord(
            symbol=symbol,
            side="buy",
            strategy=strategy,
            action="close",
            pnl_usd=pnl_usd,
            pnl_pct=pnl_pct,
            is_winner=is_winner,
            hour_utc=hour,
            market_regime="normal",
        )
        return db.log_trade(tr)

    def test_log_and_get_all(self, db):
        self._log_trade(db)
        trades = db.get_all_trades()
        assert len(trades) == 1
        assert trades[0].symbol == "BTC/USDT"

    def test_log_returns_id(self, db):
        tid = self._log_trade(db)
        assert tid > 0

    def test_get_trades_by_strategy(self, db):
        self._log_trade(db, strategy="rsi")
        self._log_trade(db, strategy="macd")
        trades = db.get_trades_by_strategy("rsi")
        assert len(trades) == 1

    def test_get_trades_by_symbol(self, db):
        self._log_trade(db, symbol="BTC/USDT")
        self._log_trade(db, symbol="ETH/USDT")
        trades = db.get_trades_by_symbol("ETH/USDT")
        assert len(trades) == 1

    def test_get_losing_trades(self, db):
        self._log_trade(db, is_winner=True, pnl_usd=10)
        self._log_trade(db, is_winner=False, pnl_usd=-5)
        losers = db.get_losing_trades()
        assert len(losers) == 1
        assert losers[0].pnl_usd == -5

    def test_get_strategy_names(self, db):
        self._log_trade(db, strategy="rsi")
        self._log_trade(db, strategy="macd")
        names = db.get_strategy_names()
        assert "rsi" in names
        assert "macd" in names

    def test_get_strategy_stats(self, db):
        self._log_trade(db, strategy="rsi", is_winner=True, pnl_usd=10)
        self._log_trade(db, strategy="rsi", is_winner=False, pnl_usd=-3)
        stats = db.get_strategy_stats("rsi")
        assert stats["total"] == 2
        assert stats["winners"] == 1

    def test_get_strategy_stats_with_symbol(self, db):
        self._log_trade(db, strategy="rsi", symbol="BTC/USDT")
        self._log_trade(db, strategy="rsi", symbol="ETH/USDT")
        stats = db.get_strategy_stats("rsi", symbol="BTC/USDT")
        assert stats["total"] == 1

    def test_get_hourly_performance(self, db):
        self._log_trade(db, hour=14)
        self._log_trade(db, hour=14)
        self._log_trade(db, hour=3)
        perf = db.get_hourly_performance()
        assert len(perf) == 2

    def test_get_hourly_performance_by_strategy(self, db):
        self._log_trade(db, strategy="rsi", hour=14)
        self._log_trade(db, strategy="macd", hour=3)
        perf = db.get_hourly_performance("rsi")
        assert len(perf) == 1

    def test_get_regime_performance(self, db):
        self._log_trade(db)
        perf = db.get_regime_performance()
        assert len(perf) == 1

    def test_get_recent_streak_wins(self, db):
        for _ in range(3):
            self._log_trade(db, is_winner=True, pnl_usd=10)
        streak = db.get_recent_streak("rsi")
        assert streak == 3

    def test_get_recent_streak_losses(self, db):
        for _ in range(4):
            self._log_trade(db, is_winner=False, pnl_usd=-5)
        streak = db.get_recent_streak("rsi")
        assert streak == -4

    def test_get_recent_streak_no_trades(self, db):
        assert db.get_recent_streak("rsi") == 0

    def test_get_max_loss_streak(self, db):
        self._log_trade(db, is_winner=True, pnl_usd=10)
        for _ in range(3):
            self._log_trade(db, is_winner=False, pnl_usd=-5)
        self._log_trade(db, is_winner=True, pnl_usd=10)
        assert db.get_max_loss_streak("rsi") == 3

    def test_trade_count(self, db):
        assert db.trade_count() == 0
        self._log_trade(db)
        self._log_trade(db)
        assert db.trade_count() == 2

    def test_close(self, db):
        db.close()
        assert db._conn is None

    def test_open_trade_inserts_row(self, db):
        tr = TradeRecord(
            symbol="BTC/USDT",
            side="buy",
            strategy="momentum",
            action="open",
            entry_price=50000,
            amount=0.01,
            leverage=5,
            opened_at="2026-02-20T10:00:00",
        )
        row_id = db.open_trade(tr)
        assert row_id > 0
        found = db.find_open_trade("BTC/USDT")
        assert found is not None
        assert found.action == "open"
        assert found.closed_at == ""

    def test_close_trade_updates_row(self, db):
        tr = TradeRecord(
            symbol="ETH/USDT",
            side="buy",
            strategy="rsi",
            action="open",
            entry_price=3000,
            amount=0.5,
            leverage=3,
            opened_at="2026-02-20T10:00:00",
        )
        row_id = db.open_trade(tr)

        close_record = TradeRecord(
            symbol="ETH/USDT",
            side="buy",
            strategy="rsi",
            action="close",
            entry_price=3000,
            exit_price=3150,
            amount=0.5,
            leverage=3,
            pnl_usd=75.0,
            pnl_pct=5.0,
            is_winner=True,
            hold_minutes=120.5,
            dca_count=1,
            max_drawdown_pct=2.0,
            opened_at="2026-02-20T10:00:00",
            closed_at="2026-02-20T12:00:30",
        )
        db.close_trade(row_id, close_record)

        trades = db.get_all_trades()
        assert len(trades) == 1
        t = trades[0]
        assert t.action == "close"
        assert t.exit_price == 3150
        assert t.pnl_usd == 75.0
        assert t.is_winner is True
        assert t.hold_minutes == 120.5
        assert t.closed_at == "2026-02-20T12:00:30"
        assert t.opened_at == "2026-02-20T10:00:00"

    def test_find_open_trade_returns_none_when_closed(self, db):
        tr = TradeRecord(
            symbol="SOL/USDT",
            side="buy",
            strategy="swing",
            action="open",
            entry_price=100,
            amount=1,
            opened_at="2026-02-20T10:00:00",
        )
        row_id = db.open_trade(tr)
        close_record = TradeRecord(
            symbol="SOL/USDT",
            side="buy",
            strategy="swing",
            action="close",
            exit_price=110,
            amount=1,
            pnl_usd=10,
            pnl_pct=10,
            is_winner=True,
            closed_at="2026-02-20T11:00:00",
        )
        db.close_trade(row_id, close_record)
        assert db.find_open_trade("SOL/USDT") is None

    def test_find_open_trade_returns_most_recent(self, db):
        tr1 = TradeRecord(
            symbol="BTC/USDT",
            side="buy",
            strategy="rsi",
            action="open",
            entry_price=40000,
            opened_at="2026-02-20T08:00:00",
        )
        db.open_trade(tr1)
        tr2 = TradeRecord(
            symbol="BTC/USDT",
            side="buy",
            strategy="macd",
            action="open",
            entry_price=41000,
            opened_at="2026-02-20T09:00:00",
        )
        db.open_trade(tr2)
        found = db.find_open_trade("BTC/USDT")
        assert found is not None
        assert found.entry_price == 41000
        assert found.strategy == "macd"

    def test_close_trade_preserves_open_fields(self, db):
        """Verify that opening context (regime, fear_greed, etc.) survives close update."""
        tr = TradeRecord(
            symbol="DOGE/USDT",
            side="buy",
            strategy="grid",
            action="open",
            entry_price=0.15,
            amount=100,
            market_regime="risk_on",
            fear_greed=80,
            daily_tier="strong",
            signal_strength=0.85,
            opened_at="2026-02-20T10:00:00",
        )
        row_id = db.open_trade(tr)
        close_record = TradeRecord(
            symbol="DOGE/USDT",
            side="buy",
            strategy="grid",
            action="close",
            exit_price=0.16,
            amount=100,
            pnl_usd=1.0,
            pnl_pct=6.67,
            is_winner=True,
            closed_at="2026-02-20T11:00:00",
        )
        db.close_trade(row_id, close_record)
        t = db.get_all_trades()[0]
        assert t.market_regime == "risk_on"
        assert t.fear_greed == 80
        assert t.daily_tier == "strong"
        assert t.signal_strength == 0.85


# ── HubDB ────────────────────────────────────────────────────────────


class TestHubDB:
    @pytest.fixture
    def hub(self, tmp_path):
        h = HubDB(path=tmp_path / "hub.db")
        h.connect()
        yield h
        h.close()

    def test_insert_and_query_trade(self, hub: HubDB):
        row_id = hub.insert_trade(
            "momentum",
            {
                "symbol": "BTC/USDT",
                "side": "long",
                "strategy": "rsi",
                "action": "open",
                "entry_price": 50000,
                "amount": 0.01,
                "leverage": 10,
                "opened_at": "2026-02-20T10:00:00",
            },
        )
        assert row_id > 0
        assert hub.trade_count() == 1
        trades = hub.get_all_trades()
        assert len(trades) == 1
        assert trades[0].symbol == "BTC/USDT"
        assert trades[0].strategy == "rsi"

    def test_update_trade_close(self, hub: HubDB):
        hub.insert_trade(
            "meanrev",
            {
                "symbol": "ETH/USDT",
                "side": "long",
                "strategy": "bollinger",
                "action": "open",
                "entry_price": 3000,
                "amount": 0.5,
                "opened_at": "2026-02-20T11:00:00",
            },
        )
        updated = hub.update_trade_close(
            "meanrev",
            "2026-02-20T11:00:00",
            {
                "exit_price": 3100,
                "amount": 0.5,
                "pnl_usd": 50,
                "pnl_pct": 3.33,
                "is_winner": True,
                "hold_minutes": 120,
                "closed_at": "2026-02-20T13:00:00",
            },
        )
        assert updated is True
        trades = hub.get_all_trades()
        assert trades[0].closed_at == "2026-02-20T13:00:00"
        assert trades[0].pnl_usd == 50.0

    def test_update_trade_close_no_match(self, hub: HubDB):
        updated = hub.update_trade_close("ghost", "2026-01-01T00:00:00", {"closed_at": "now"})
        assert updated is False

    def test_update_trade_runtime(self, hub: HubDB):
        hub.insert_trade(
            "momentum",
            {
                "symbol": "BTC/USDT",
                "side": "long",
                "strategy": "rsi",
                "action": "open",
                "entry_price": 50000,
                "amount": 0.01,
                "opened_at": "2026-02-20T10:00:00",
            },
        )
        updated = hub.update_trade_runtime(
            "momentum",
            "2026-02-20T10:00:00",
            {
                "planned_stop_loss": 49000,
                "planned_tp1": 52000,
                "exchange_stop_loss": 49100,
                "bot_stop_loss": 49200,
                "effective_stop_loss": 49100,
                "effective_take_profit": 52000,
                "stop_source": "exchange",
                "tp_source": "bot",
            },
        )
        assert updated is True
        trade = hub.get_all_trades()[0]
        assert trade.exchange_stop_loss == 49100
        assert trade.effective_stop_loss == 49100
        assert trade.stop_source == "exchange"

    def test_strategy_stats(self, hub: HubDB):
        for i in range(3):
            hub.insert_trade(
                "bot1",
                {
                    "symbol": "BTC/USDT",
                    "side": "long",
                    "strategy": "rsi",
                    "action": "close",
                    "pnl_usd": 10.0 if i < 2 else -5.0,
                    "pnl_pct": 1.0 if i < 2 else -0.5,
                    "is_winner": i < 2,
                    "closed_at": f"2026-02-20T{10 + i}:00:00",
                },
            )
        stats = hub.get_strategy_stats("rsi")
        assert stats["total"] == 3
        assert stats["winners"] == 2

    def test_hourly_performance(self, hub: HubDB):
        hub.insert_trade(
            "bot1",
            {
                "symbol": "X",
                "side": "long",
                "strategy": "s",
                "action": "close",
                "pnl_usd": 5,
                "hour_utc": 14,
                "is_winner": True,
                "closed_at": "2026-02-20T14:00:00",
            },
        )
        hourly = hub.get_hourly_performance()
        assert len(hourly) == 1
        assert hourly[0]["hour_utc"] == 14

    def test_conn_property(self, hub: HubDB):
        assert hub.conn is not None

    def test_request_key_dedup(self, hub: HubDB):
        row1 = hub.insert_trade(
            "bot1",
            {"symbol": "A", "side": "l", "strategy": "s", "action": "open", "opened_at": "2026-01-01"},
            request_key="rk-1",
        )
        row2 = hub.insert_trade(
            "bot1",
            {"symbol": "A", "side": "l", "strategy": "s", "action": "open", "opened_at": "2026-01-01"},
            request_key="rk-1",
        )
        assert row1 == row2
        assert hub.trade_count() == 1

    def test_get_open_trades_for_bot(self, hub: HubDB):
        hub.insert_trade(
            "bot1",
            {"symbol": "BTC/USDT", "side": "long", "strategy": "rsi", "action": "open", "opened_at": "2026-01-01"},
        )
        hub.insert_trade(
            "bot1",
            {
                "symbol": "ETH/USDT",
                "side": "long",
                "strategy": "macd",
                "action": "close",
                "opened_at": "2026-01-02",
                "closed_at": "2026-01-03",
            },
        )
        hub.insert_trade(
            "bot2",
            {"symbol": "SOL/USDT", "side": "long", "strategy": "rsi", "action": "open", "opened_at": "2026-01-04"},
        )
        open_trades = hub.get_open_trades_for_bot("bot1")
        assert len(open_trades) == 1
        assert open_trades[0].symbol == "BTC/USDT"

    def test_get_strategy_stats_for_bot(self, hub: HubDB):
        for i in range(2):
            hub.insert_trade(
                "b1",
                {
                    "symbol": "X",
                    "side": "l",
                    "strategy": "rsi",
                    "action": "close",
                    "pnl_usd": 10,
                    "is_winner": True,
                    "closed_at": f"2026-01-0{i + 1}",
                },
            )
        hub.insert_trade(
            "b2",
            {
                "symbol": "X",
                "side": "l",
                "strategy": "rsi",
                "action": "close",
                "pnl_usd": -5,
                "is_winner": False,
                "closed_at": "2026-01-05",
            },
        )
        stats = hub.get_strategy_stats_for_bot("b1", "rsi")
        assert stats["total"] == 2
        assert stats["winners"] == 2

    def test_get_all_strategy_stats_for_bot(self, hub: HubDB):
        hub.insert_trade(
            "b1",
            {
                "symbol": "BTC/USDT",
                "side": "l",
                "strategy": "rsi",
                "action": "close",
                "pnl_usd": 10,
                "is_winner": True,
                "closed_at": "2026-01-01",
            },
        )
        hub.insert_trade(
            "b1",
            {
                "symbol": "ETH/USDT",
                "side": "l",
                "strategy": "macd",
                "action": "close",
                "pnl_usd": -3,
                "is_winner": False,
                "closed_at": "2026-01-02",
            },
        )
        all_stats = hub.get_all_strategy_stats_for_bot("b1")
        assert "rsi:BTC/USDT" in all_stats
        assert "macd:ETH/USDT" in all_stats

    def test_mark_recovery_close(self, hub: HubDB):
        hub.insert_trade(
            "bot1",
            {
                "symbol": "BTC/USDT",
                "side": "long",
                "strategy": "rsi",
                "action": "open",
                "opened_at": "2026-01-01T10:00:00",
            },
        )
        updated = hub.mark_recovery_close("bot1", "2026-01-01T10:00:00")
        assert updated is True
        open_trades = hub.get_open_trades_for_bot("bot1")
        assert len(open_trades) == 0

    def test_drain_confirmed_keys(self, hub: HubDB):
        hub.insert_trade(
            "bot1",
            {"symbol": "X", "side": "l", "strategy": "s", "action": "open", "opened_at": "2026-01-01"},
            request_key="k1",
        )
        hub.insert_trade(
            "bot1",
            {"symbol": "Y", "side": "l", "strategy": "s", "action": "open", "opened_at": "2026-01-02"},
            request_key="k2",
        )
        keys = hub.drain_confirmed_keys("bot1")
        assert set(keys) == {"k1", "k2"}
        assert hub.drain_confirmed_keys("bot1") == []

    def test_recovery_close_excluded_from_stats(self, hub: HubDB):
        hub.insert_trade(
            "b1", {"symbol": "X", "side": "l", "strategy": "rsi", "action": "open", "opened_at": "2026-01-01T10:00:00"}
        )
        hub.mark_recovery_close("b1", "2026-01-01T10:00:00")
        stats = hub.get_strategy_stats_for_bot("b1", "rsi")
        assert stats.get("total", 0) == 0

    def test_binance_snapshots_roundtrip(self, hub: HubDB):
        rows = [
            {
                "timestamp": "2026-02-23T10:00:00+00:00",
                "symbol": "BTC/USDT",
                "price": 50000.0,
                "quote_volume": 1000000000.0,
                "change_24h": 2.5,
                "funding_rate": 0.0001,
            },
            {
                "timestamp": "2026-02-23T10:01:00+00:00",
                "symbol": "BTC/USDT",
                "price": 50050.0,
                "quote_volume": 1001000000.0,
                "change_24h": 2.6,
                "funding_rate": 0.00011,
            },
        ]
        hub.save_binance_snapshots(rows)
        loaded = hub.load_binance_snapshots_since("2026-02-23T09:59:00+00:00")
        assert len(loaded) == 2
        assert loaded[0]["symbol"] == "BTC/USDT"

    def test_binance_snapshots_cleanup(self, hub: HubDB):
        hub.save_binance_snapshots(
            [
                {
                    "timestamp": "2026-02-20T10:00:00+00:00",
                    "symbol": "ETH/USDT",
                    "price": 3000.0,
                    "quote_volume": 500000000.0,
                    "change_24h": 1.2,
                    "funding_rate": 0.0002,
                },
                {
                    "timestamp": "2026-02-23T10:00:00+00:00",
                    "symbol": "ETH/USDT",
                    "price": 3050.0,
                    "quote_volume": 510000000.0,
                    "change_24h": 1.6,
                    "funding_rate": 0.00025,
                },
            ]
        )
        removed = hub.cleanup_binance_snapshots_before("2026-02-22T00:00:00+00:00")
        assert removed == 1
        loaded = hub.load_binance_snapshots_since("2026-02-20T00:00:00+00:00")
        assert len(loaded) == 1
        assert loaded[0]["timestamp"] == "2026-02-23T10:00:00+00:00"

    def test_binance_symbol_states_roundtrip(self, hub: HubDB):
        hub.save_binance_symbol_states(
            [
                {
                    "symbol": "BTC/USDT",
                    "updated_at": "2026-02-23T10:00:00+00:00",
                    "first_seen_at": "2026-02-23T09:00:00+00:00",
                    "sample_count": 61,
                    "last_price": 50100.0,
                    "last_quote_volume": 1200000000.0,
                    "last_change_24h": 2.8,
                    "last_funding_rate": 0.00012,
                    "avg_quote_volume": 1000000000.0,
                    "vol_accel": 1.2,
                    "confidence": 1.0,
                    "score": 8.5,
                    "chg_1m": 0.1,
                    "chg_5m": 0.6,
                    "chg_1h": 1.4,
                    "chg_4h": 2.1,
                    "chg_1d": 2.8,
                    "chg_1w": 0.0,
                    "chg_3w": 0.0,
                    "chg_1mo": 0.0,
                    "chg_3mo": 0.0,
                    "chg_1y": 0.0,
                    "anchor_1m_ts": "2026-02-23T09:59:00+00:00",
                    "anchor_5m_ts": "2026-02-23T09:55:00+00:00",
                    "anchor_1h_ts": "2026-02-23T09:00:00+00:00",
                    "anchor_4h_ts": "2026-02-23T06:00:00+00:00",
                    "anchor_1d_ts": "2026-02-22T10:00:00+00:00",
                    "anchor_1w_ts": "2026-02-16T10:00:00+00:00",
                    "anchor_3w_ts": "2026-02-02T10:00:00+00:00",
                    "anchor_1mo_ts": "2026-01-23T10:00:00+00:00",
                    "anchor_3mo_ts": "2025-11-23T10:00:00+00:00",
                    "anchor_1y_ts": "2025-02-23T10:00:00+00:00",
                    "anchor_1m_price": 50050.0,
                    "anchor_5m_price": 49800.0,
                    "anchor_1h_price": 49400.0,
                    "anchor_4h_price": 49000.0,
                    "anchor_1d_price": 48750.0,
                    "anchor_1w_price": 48000.0,
                    "anchor_3w_price": 47000.0,
                    "anchor_1mo_price": 45000.0,
                    "anchor_3mo_price": 40000.0,
                    "anchor_1y_price": 25000.0,
                }
            ]
        )
        loaded = hub.load_binance_symbol_states()
        assert len(loaded) == 1
        assert loaded[0]["symbol"] == "BTC/USDT"
        assert loaded[0]["sample_count"] == 61
        assert loaded[0]["chg_1h"] == pytest.approx(1.4)

    def test_openclaw_report_and_suggestion_lifecycle(self, hub: HubDB):
        report_id = hub.insert_openclaw_daily_report(
            report_day="2026-02-24",
            run_kind="startup",
            requested_at="2026-02-24T00:00:00+00:00",
            completed_at="2026-02-24T00:00:05+00:00",
            lane_used="paid",
            source_url="http://openclaw-bridge:18080/daily-review",
            context_payload={"k": "v"},
            response_payload={"summary": "ok", "suggestions": []},
            status="ok",
            error_text="",
        )
        assert report_id > 0

        latest = hub.get_latest_openclaw_daily_report()
        assert latest is not None
        assert latest["lane_used"] == "paid"
        assert latest["response"]["summary"] == "ok"

        sid = hub.upsert_openclaw_suggestion(
            {
                "suggestion_type": "reduce_weight",
                "title": "Reduce momentum",
                "description": "Too volatile in current regime",
                "strategy": "momentum",
                "symbol": "BTC/USDT",
                "confidence": 0.7,
                "suggested_value": "weight=0.7",
                "based_on_trades": 42,
            },
            report_id=report_id,
        )
        assert sid > 0
        rows = hub.list_openclaw_suggestions()
        assert len(rows) == 1
        assert rows[0]["status"] == "new"

        updated = hub.mark_openclaw_suggestion_status(sid, "implemented", notes="done")
        assert updated is True
        rows2 = hub.list_openclaw_suggestions(include_removed=True)
        assert rows2[0]["status"] == "implemented"
        assert rows2[0]["implemented_at"] != ""
