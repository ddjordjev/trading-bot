"""Tests for db/ (models, store)."""

from __future__ import annotations

import pytest

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
