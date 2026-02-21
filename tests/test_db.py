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

    def test_insert_and_query_deposit(self, hub: HubDB):
        dep_id = hub.insert_deposit(
            bot_id="momentum",
            amount=500.0,
            exchange="mexc",
            detected_at="2026-02-20T14:00:00",
            balance_before=1000.0,
            balance_after=1500.0,
        )
        assert dep_id > 0
        deposits = hub.get_deposits()
        assert len(deposits) == 1
        assert deposits[0]["amount"] == 500.0
        assert deposits[0]["exchange"] == "mexc"

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
