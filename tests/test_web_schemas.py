"""Tests for web/schemas.py."""

from __future__ import annotations

from web.schemas import (
    ActionResponse,
    AnalyticsSnapshot,
    BotStatus,
    DailyReportData,
    FullSnapshot,
    IntelSnapshot,
    ModificationSuggestionInfo,
    ModuleStatus,
    PatternInsightInfo,
    PositionInfo,
    StrategyInfo,
    StrategyScoreInfo,
    TradeRecord,
    TrendingCoinInfo,
    WickScalpInfo,
)


class TestBotStatus:
    def test_defaults(self):
        s = BotStatus()
        assert s.running is False
        assert s.trading_mode == "paper_local"
        assert s.balance == 0.0

    def test_custom(self):
        s = BotStatus(running=True, balance=5000, daily_pnl=200, tier="strong")
        assert s.running is True
        assert s.tier == "strong"


class TestPositionInfo:
    def test_creation(self):
        p = PositionInfo(
            symbol="BTC/USDT", side="buy", amount=1.0, entry_price=100, current_price=110, pnl_pct=10, pnl_usd=100
        )
        assert p.symbol == "BTC/USDT"
        assert p.pnl_pct == 10


class TestTradeRecord:
    def test_creation(self):
        t = TradeRecord(
            timestamp="2024-01-01", symbol="BTC/USDT", side="buy", action="close", amount=1.0, price=100, strategy="rsi"
        )
        assert t.pnl == 0.0


class TestIntelSnapshot:
    def test_defaults(self):
        s = IntelSnapshot()
        assert s.regime == "normal"
        assert s.fear_greed == 50


class TestTrendingCoinInfo:
    def test_creation(self):
        t = TrendingCoinInfo(symbol="DOGE/USDT", change_24h=15.0)
        assert t.change_24h == 15.0


class TestStrategyInfo:
    def test_creation(self):
        s = StrategyInfo(name="rsi", symbol="BTC/USDT", market_type="futures", leverage=10)
        assert s.mode == "pyramid"


class TestModuleStatus:
    def test_creation(self):
        m = ModuleStatus(name="intel", enabled=True, display_name="Market Intel")
        assert m.enabled is True


class TestDailyReportData:
    def test_defaults(self):
        d = DailyReportData()
        assert d.winning_days == 0
        assert d.history == []


class TestWickScalpInfo:
    def test_creation(self):
        w = WickScalpInfo(symbol="BTC/USDT", scalp_side="short", entry_price=100, amount=0.5, age_minutes=2.0)
        assert w.max_hold_minutes == 5


class TestFullSnapshot:
    def test_creation(self):
        s = FullSnapshot(status=BotStatus(), positions=[])
        assert s.positions == []


class TestStrategyScoreInfo:
    def test_defaults(self):
        s = StrategyScoreInfo(strategy="rsi")
        assert s.weight == 1.0
        assert s.total_trades == 0


class TestPatternInsightInfo:
    def test_creation(self):
        p = PatternInsightInfo(pattern_type="time_of_day", description="Bad at 3am")
        assert p.severity == "info"


class TestModificationSuggestionInfo:
    def test_creation(self):
        m = ModificationSuggestionInfo(strategy="rsi", suggestion_type="change_param", title="Test", description="desc")
        assert m.confidence == 0.0


class TestAnalyticsSnapshot:
    def test_defaults(self):
        a = AnalyticsSnapshot()
        assert a.total_trades_logged == 0
        assert a.strategy_scores == []


class TestActionResponse:
    def test_creation(self):
        r = ActionResponse(success=True, message="OK")
        assert r.success is True
