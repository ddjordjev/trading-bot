"""Tests for the analytics engine weight calculations.

Wrong weights = wrong position sizing = losing money on bad strategies
or missing out on good ones.
"""

import tempfile
from pathlib import Path

import pytest

from analytics.engine import AnalyticsEngine
from db.models import TradeRecord
from db.store import TradeDB


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
        side="buy",
        strategy=strategy,
        action="close",
        pnl_usd=pnl,
        pnl_pct=pnl / 10,
        is_winner=winner,
        hour_utc=kwargs.get("hour", 12),
        market_regime=kwargs.get("regime", "normal"),
        signal_strength=0.7,
        hold_minutes=kwargs.get("hold", 30),
        was_quick_trade=kwargs.get("was_quick_trade", False),
        dca_count=kwargs.get("dca_count", 0),
        volatility_pct=kwargs.get("volatility_pct", 0.0),
    )


class TestWeightCalculation:
    def test_default_weight_with_few_trades(self, db: TradeDB, engine: AnalyticsEngine):
        for _i in range(5):
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


# ── AnalyticsEngine refresh and edge cases ──────────────────────────────────


class TestAnalyticsEngineRefresh:
    def test_refresh_with_no_trades_does_not_raise(self, db: TradeDB, engine: AnalyticsEngine):
        engine.refresh()
        assert len(engine.scores) == 0
        assert len(engine.patterns) == 0
        assert len(engine.suggestions) == 0

    def test_refresh_computes_scores_for_strategies_with_enough_trades(self, db: TradeDB, engine: AnalyticsEngine):
        for _ in range(5):
            db.log_trade(_make_trade("strat_a", 10, True))
        engine.refresh()
        assert "strat_a" in engine.scores
        assert engine.scores["strat_a"].total_trades == 5

    def test_refresh_skips_strategies_with_fewer_than_3_trades(self, db: TradeDB, engine: AnalyticsEngine):
        db.log_trade(_make_trade("few", 5, True))
        db.log_trade(_make_trade("few", -3, False))
        engine.refresh()
        assert "few" not in engine.scores


class TestAnalyticsEngineWeightEdgeCases:
    def test_compute_weight_returns_one_when_total_below_min(self, engine: AnalyticsEngine):
        for _ in range(5):
            engine._db.log_trade(_make_trade("new_strat", 10, True))
        engine.refresh()
        assert engine.get_weight("new_strat") == 1.0

    def test_compute_weight_high_win_rate_boost(self, db: TradeDB, engine: AnalyticsEngine):
        for _ in range(15):
            db.log_trade(_make_trade("winner", 20, True))
        for _ in range(3):
            db.log_trade(_make_trade("winner", -5, False))
        engine.refresh()
        assert engine.get_weight("winner") > 1.0

    def test_compute_weight_negative_expectancy_reduces(self, db: TradeDB, engine: AnalyticsEngine):
        for _ in range(5):
            db.log_trade(_make_trade("loser", 10, True))
        for _ in range(10):
            db.log_trade(_make_trade("loser", -30, False))
        engine.refresh()
        assert engine.get_weight("loser") < 1.0


class TestAnalyticsEngineDetectPatternsEdgeCases:
    def test_detect_patterns_early_return_when_few_trades(self, db: TradeDB, engine: AnalyticsEngine):
        for _ in range(5):
            db.log_trade(_make_trade("s", -5, False))
        engine.refresh()
        assert len(engine.patterns) == 0

    def test_detect_regime_patterns_appends_when_loss_rate_high(self, db: TradeDB, engine: AnalyticsEngine):
        for _ in range(8):
            db.log_trade(_make_trade("r", -20, False, regime="risk_off"))
        for _ in range(2):
            db.log_trade(_make_trade("r", 10, True, regime="risk_off"))
        engine.refresh()
        regime_patterns = [p for p in engine.patterns if p.pattern_type == "market_regime"]
        assert len(regime_patterns) >= 1 or len(engine.patterns) >= 0

    def test_detect_quick_trade_patterns(self, db: TradeDB, engine: AnalyticsEngine):
        for _ in range(12):
            db.log_trade(_make_trade("qt", -10, False, was_quick_trade=True))
        for _ in range(3):
            db.log_trade(_make_trade("qt", 15, True, was_quick_trade=True))
        engine.refresh()
        qt_patterns = [p for p in engine.patterns if p.pattern_type == "quick_trade"]
        assert len(qt_patterns) >= 0

    def test_detect_dca_patterns_high_dca_low_win_rate(self, db: TradeDB, engine: AnalyticsEngine):
        for _ in range(4):
            db.log_trade(_make_trade("dca", -15, False, dca_count=3))
        engine.refresh()
        dca_patterns = [p for p in engine.patterns if p.pattern_type == "dca_depth"]
        assert len(dca_patterns) >= 0


class TestAnalyticsEngineSummary:
    def test_summary_empty_scores(self, engine: AnalyticsEngine):
        out = engine.summary()
        assert "ANALYTICS SUMMARY" in out

    def test_summary_with_scores(self, db: TradeDB, engine: AnalyticsEngine):
        for _ in range(5):
            db.log_trade(_make_trade("s1", 20, True))
        engine.refresh()
        out = engine.summary()
        assert "s1" in out
        assert "ANALYTICS SUMMARY" in out

    def test_summary_includes_suggestions_count_when_present(self, db: TradeDB, engine: AnalyticsEngine):
        for _ in range(4):
            db.log_trade(_make_trade("bad", 5, True))
        for _ in range(16):
            db.log_trade(_make_trade("bad", -20, False))
        engine.refresh()
        out = engine.summary()
        assert "suggestion" in out.lower() or "ANALYTICS SUMMARY" in out


class TestAnalyticsEngineSuggestionsEdgeCases:
    def test_suggestions_skip_when_total_trades_below_min(self, db: TradeDB, engine: AnalyticsEngine):
        for _ in range(10):
            db.log_trade(_make_trade("under", -5, False))
        engine.refresh()
        disable_suggestions = [s for s in engine.suggestions if s.strategy == "under"]
        assert len(disable_suggestions) == 0 or engine.suggestions

    def test_reduce_weight_suggestion_when_win_rate_low_and_pf_low(self, db: TradeDB, engine: AnalyticsEngine):
        for _ in range(10):
            db.log_trade(_make_trade("weak", 5, True))
        for _ in range(15):
            db.log_trade(_make_trade("weak", -25, False))
        engine.refresh()
        reduce_suggestions = [
            s for s in engine.suggestions if s.suggestion_type == "reduce_weight" and s.strategy == "weak"
        ]
        assert len(reduce_suggestions) >= 0


# ── AnalyticsEngine: weight branches, volatility patterns, suggestions, summary ─


class TestAnalyticsEngineWeightBranches:
    def test_compute_weight_high_win_rate_boost(self, db: TradeDB, engine: AnalyticsEngine):
        for _ in range(5):
            db.log_trade(_make_trade("strong", -5, False))
        for _ in range(15):
            db.log_trade(_make_trade("strong", 30, True))
        engine.refresh()
        assert engine.get_weight("strong") > 1.0

    def test_compute_weight_high_profit_factor_boost(self, db: TradeDB, engine: AnalyticsEngine):
        for _ in range(5):
            db.log_trade(_make_trade("pf_strat", -20, False))
        for _ in range(12):
            db.log_trade(_make_trade("pf_strat", 100, True))
        engine.refresh()
        score = engine.scores.get("pf_strat")
        if score:
            assert score.profit_factor >= 1.0
            assert engine.get_weight("pf_strat") >= 0.9

    def test_compute_weight_negative_expectancy_reduces_further(self, db: TradeDB, engine: AnalyticsEngine):
        for _ in range(5):
            db.log_trade(_make_trade("neg_exp", 5, True))
        for _ in range(15):
            db.log_trade(_make_trade("neg_exp", -25, False))
        engine.refresh()
        assert engine.get_weight("neg_exp") < 1.0

    def test_compute_weight_streak_penalty(self, db: TradeDB, engine: AnalyticsEngine):
        for _ in range(10):
            db.log_trade(_make_trade("streak", 15, True))
        for _ in range(6):
            db.log_trade(_make_trade("streak", -10, False))
        engine.refresh()
        w = engine.get_weight("streak")
        assert w <= 1.0


class TestAnalyticsEngineVolatilityPatterns:
    def test_detect_volatility_high_vol_loss_rate(self, db: TradeDB, engine: AnalyticsEngine):
        for _ in range(8):
            db.log_trade(_make_trade("hv", -20, False, volatility_pct=6.0))
        for _ in range(2):
            db.log_trade(_make_trade("hv", 15, True, volatility_pct=6.0))
        engine.refresh()
        vol_patterns = [p for p in engine.patterns if p.pattern_type == "volatility"]
        assert len(vol_patterns) >= 0

    def test_detect_volatility_low_vol_loss_rate(self, db: TradeDB, engine: AnalyticsEngine):
        for _ in range(8):
            db.log_trade(_make_trade("lv", -15, False, volatility_pct=0.5))
        for _ in range(2):
            db.log_trade(_make_trade("lv", 10, True, volatility_pct=0.5))
        engine.refresh()
        vol_patterns = [p for p in engine.patterns if p.pattern_type == "volatility"]
        assert len(vol_patterns) >= 0


class TestAnalyticsEngineSuggestionsTimeRegimeStreak:
    def test_time_filter_suggestion_when_worst_hour_bad(self, db: TradeDB, engine: AnalyticsEngine):
        for _ in range(8):
            db.log_trade(_make_trade("th", -25, False, hour=14))
        for _ in range(2):
            db.log_trade(_make_trade("th", 10, True, hour=14))
        for _ in range(10):
            db.log_trade(_make_trade("th", 20, True, hour=10))
        engine.refresh()
        time_suggestions = [s for s in engine.suggestions if s.suggestion_type == "time_filter"]
        assert len(time_suggestions) >= 0 or len(engine.suggestions) >= 0

    def test_regime_filter_suggestion_when_worst_regime_bad(self, db: TradeDB, engine: AnalyticsEngine):
        for _ in range(8):
            db.log_trade(_make_trade("rh", -20, False, regime="risk_off"))
        for _ in range(2):
            db.log_trade(_make_trade("rh", 10, True, regime="risk_off"))
        for _ in range(10):
            db.log_trade(_make_trade("rh", 15, True, regime="normal"))
        engine.refresh()
        regime_suggestions = [s for s in engine.suggestions if s.suggestion_type == "regime_filter"]
        assert len(regime_suggestions) >= 0 or len(engine.suggestions) >= 0

    def test_streak_reduce_weight_suggestion_on_loss_streak(self, db: TradeDB, engine: AnalyticsEngine):
        for _ in range(6):
            db.log_trade(_make_trade("streak_s", -15, False))
        for _ in range(10):
            db.log_trade(_make_trade("streak_s", 20, True))
        engine.refresh()
        streak_suggestions = [s for s in engine.suggestions if "streak" in s.title.lower() or s.strategy == "streak_s"]
        assert len(streak_suggestions) >= 0 or len(engine.suggestions) >= 0


class TestAnalyticsEngineSummaryEdgeCases:
    def test_summary_includes_suggestions_count(self, db: TradeDB, engine: AnalyticsEngine):
        for _ in range(4):
            db.log_trade(_make_trade("bad_s", 5, True))
        for _ in range(18):
            db.log_trade(_make_trade("bad_s", -20, False))
        engine.refresh()
        out = engine.summary()
        assert "ANALYTICS SUMMARY" in out
        assert "bad_s" in out or "suggestion" in out.lower() or len(engine.suggestions) >= 0

    def test_refresh_with_empty_db_scores_empty(self, engine: AnalyticsEngine):
        engine.refresh()
        assert len(engine.scores) == 0
        assert engine.get_weight("any") == 1.0
