"""Tests for the analytics engine weight calculations.

Wrong weights = wrong position sizing = losing money on bad strategies
or missing out on good ones.
"""
import tempfile
from pathlib import Path

import pytest

from db.store import TradeDB
from db.models import TradeRecord
from analytics.engine import AnalyticsEngine


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.db"
        store = TradeDB(path)
        store.connect()
        yield store
        store.close()


@pytest.fixture
def engine(db: TradeDB):
    return AnalyticsEngine(db)


def _make_trade(strategy: str, pnl: float, winner: bool, **kwargs) -> TradeRecord:
    return TradeRecord(
        symbol=kwargs.get("symbol", "BTC/USDT"),
        side="buy", strategy=strategy, action="close",
        pnl_usd=pnl, pnl_pct=pnl / 10, is_winner=winner,
        hour_utc=kwargs.get("hour", 12),
        market_regime=kwargs.get("regime", "normal"),
        signal_strength=0.7,
        hold_minutes=kwargs.get("hold", 30),
    )


class TestWeightCalculation:
    def test_default_weight_with_few_trades(self, db: TradeDB, engine: AnalyticsEngine):
        for i in range(5):
            db.log_trade(_make_trade("test_strat", 10, True))
        engine.refresh()
        assert engine.get_weight("test_strat") == 1.0  # not enough data

    def test_good_strategy_gets_boosted(self, db: TradeDB, engine: AnalyticsEngine):
        for _ in range(12):
            db.log_trade(_make_trade("good_strat", 50, True))
        for _ in range(3):
            db.log_trade(_make_trade("good_strat", -20, False))
        engine.refresh()
        assert engine.get_weight("good_strat") > 1.0

    def test_bad_strategy_gets_penalized(self, db: TradeDB, engine: AnalyticsEngine):
        for _ in range(3):
            db.log_trade(_make_trade("bad_strat", 10, True))
        for _ in range(12):
            db.log_trade(_make_trade("bad_strat", -30, False))
        engine.refresh()
        assert engine.get_weight("bad_strat") < 0.5

    def test_weight_never_zero(self, db: TradeDB, engine: AnalyticsEngine):
        for _ in range(20):
            db.log_trade(_make_trade("terrible", -50, False))
        engine.refresh()
        assert engine.get_weight("terrible") > 0

    def test_unknown_strategy_returns_default(self, engine: AnalyticsEngine):
        assert engine.get_weight("nonexistent") == 1.0


class TestPatternDetection:
    def test_detects_bad_hour(self, db: TradeDB, engine: AnalyticsEngine):
        for _ in range(8):
            db.log_trade(_make_trade("strat", -20, False, hour=3))
        for _ in range(2):
            db.log_trade(_make_trade("strat", 10, True, hour=3))
        engine.refresh()
        time_patterns = [p for p in engine.patterns if p.pattern_type == "time_of_day"]
        assert len(time_patterns) >= 1
        assert any(p.data.get("hour") == 3 for p in time_patterns)

    def test_detects_bad_strategy_symbol_combo(self, db: TradeDB, engine: AnalyticsEngine):
        # Need 8+ trades total for the combo, and >= 0.65 loss rate with negative pnl
        for _ in range(9):
            db.log_trade(_make_trade("momentum", -15, False, symbol="DOGE/USDT"))
        for _ in range(2):
            db.log_trade(_make_trade("momentum", 10, True, symbol="DOGE/USDT"))
        # Also need 10+ total trades overall for pattern detection min threshold
        engine.refresh()
        combo_patterns = [p for p in engine.patterns if p.pattern_type == "strategy_symbol"]
        assert len(combo_patterns) >= 1

    def test_no_patterns_with_few_trades(self, db: TradeDB, engine: AnalyticsEngine):
        for _ in range(3):
            db.log_trade(_make_trade("strat", -10, False))
        engine.refresh()
        assert len(engine.patterns) == 0


class TestSuggestions:
    def test_suggests_disable_for_terrible_strategy(self, db: TradeDB, engine: AnalyticsEngine):
        for _ in range(4):
            db.log_trade(_make_trade("loser", 5, True))
        for _ in range(16):
            db.log_trade(_make_trade("loser", -20, False))
        engine.refresh()
        disable_suggestions = [s for s in engine.suggestions if s.suggestion_type == "disable"]
        assert any(s.strategy == "loser" for s in disable_suggestions)

    def test_no_suggestions_for_good_strategy(self, db: TradeDB, engine: AnalyticsEngine):
        # Interleave wins and losses so we don't create a losing streak
        for _ in range(5):
            db.log_trade(_make_trade("winner", 30, True))
            db.log_trade(_make_trade("winner", 30, True))
            db.log_trade(_make_trade("winner", 30, True))
            db.log_trade(_make_trade("winner", -10, False))
        engine.refresh()
        strat_suggestions = [s for s in engine.suggestions if s.strategy == "winner"]
        assert len(strat_suggestions) == 0


class TestTradeDB:
    def test_log_and_retrieve(self, db: TradeDB):
        trade = _make_trade("test", 42.0, True)
        tid = db.log_trade(trade)
        assert tid > 0
        trades = db.get_all_trades()
        assert len(trades) == 1
        assert trades[0].pnl_usd == 42.0

    def test_strategy_stats(self, db: TradeDB):
        for _ in range(3):
            db.log_trade(_make_trade("s1", 10, True))
        db.log_trade(_make_trade("s1", -5, False))
        stats = db.get_strategy_stats("s1")
        assert stats["total"] == 4
        assert stats["winners"] == 3

    def test_streak_tracking(self, db: TradeDB):
        for _ in range(4):
            db.log_trade(_make_trade("s1", -10, False))
        streak = db.get_recent_streak("s1")
        assert streak == -4

    def test_trade_count(self, db: TradeDB):
        assert db.trade_count() == 0
        db.log_trade(_make_trade("s1", 10, True))
        assert db.trade_count() == 1
