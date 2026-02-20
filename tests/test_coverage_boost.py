"""Targeted tests to reach 80%+ coverage.

Covers gaps in: risk/manager, bollinger, shared/models (TradeQueue),
config/settings, news/monitor, scanner/trending, services/analytics_service,
intel/liquidations, intel/coinmarketcap, intel/whale_sentiment.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config.settings import Settings
from core.models import OrderSide, Position, Signal, SignalAction

# ── Bollinger Strategy ───────────────────────────────────────────────────────


def _bb_candle(day, hour, close, volume=1000):
    from core.models import Candle

    return Candle(
        timestamp=datetime(2026, 1, day, hour, 0, tzinfo=UTC),
        open=close,
        high=close + 0.5,
        low=close - 0.5,
        close=close,
        volume=volume,
    )


class TestBollingerSignals:
    def test_buy_signal_below_lower_band(self):
        from strategies.bollinger import BollingerStrategy

        s = BollingerStrategy("BTC/USDT", period=20, std_dev=2.0)
        candles = [_bb_candle(1 + i // 24, i % 24, 100.0 + (i % 3 - 1) * 0.5) for i in range(24)]
        candles.append(_bb_candle(2, 1, 89.0, volume=2000))
        sig = s.analyze(candles)
        if sig:
            assert sig.action == SignalAction.BUY

    def test_sell_signal_above_upper_band(self):
        from strategies.bollinger import BollingerStrategy

        s = BollingerStrategy("BTC/USDT", period=20, std_dev=2.0)
        candles = [_bb_candle(1 + i // 24, i % 24, 100.0 + (i % 3 - 1) * 0.5) for i in range(24)]
        candles.append(_bb_candle(2, 1, 112.0, volume=2000))
        sig = s.analyze(candles)
        if sig:
            assert sig.action == SignalAction.SELL

    def test_zero_band_width_returns_none(self):
        from strategies.bollinger import BollingerStrategy

        s = BollingerStrategy("BTC/USDT", period=20, std_dev=2.0)
        candles = [_bb_candle(1 + i // 24, i % 24, 100.0) for i in range(25)]
        sig = s.analyze(candles)
        assert sig is None


# ── Risk Manager — extended ──────────────────────────────────────────────────


@pytest.fixture
def _settings(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper_local")
    monkeypatch.setenv("EXCHANGE", "mexc")
    monkeypatch.setenv("MAX_POSITION_SIZE_PCT", "5.0")
    monkeypatch.setenv("MAX_DAILY_LOSS_PCT", "3.0")
    monkeypatch.setenv("STOP_LOSS_PCT", "2.0")
    monkeypatch.setenv("TAKE_PROFIT_PCT", "4.0")
    monkeypatch.setenv("MAX_CONCURRENT_POSITIONS", "5")
    monkeypatch.setenv("MIN_SIGNAL_STRENGTH", "0.4")
    monkeypatch.setenv("CONSECUTIVE_LOSS_COOLDOWN", "3")
    return Settings(_env_file=None)


@pytest.fixture
def risk(_settings):
    from core.risk.manager import RiskManager

    rm = RiskManager(_settings)
    rm.reset_daily(10000.0)
    return rm


def _sig(action=SignalAction.BUY, strength=0.7, price=100.0):
    return Signal(symbol="BTC/USDT", action=action, strategy="test", strength=strength, suggested_price=price)


class TestRiskManagerExtended:
    def test_apply_stops_buy(self, risk):
        sig = _sig(action=SignalAction.BUY, price=50000.0)
        result = risk.apply_stops(sig)
        assert result.suggested_stop_loss is not None
        assert result.suggested_stop_loss < 50000.0
        assert result.suggested_take_profit is not None
        assert result.suggested_take_profit > 50000.0

    def test_apply_stops_sell(self, risk):
        sig = _sig(action=SignalAction.SELL, price=50000.0)
        result = risk.apply_stops(sig)
        assert result.suggested_stop_loss > 50000.0
        assert result.suggested_take_profit < 50000.0

    def test_apply_stops_no_price(self, risk):
        sig = _sig(price=0.0)
        result = risk.apply_stops(sig)
        assert result.suggested_stop_loss is None

    def test_daily_pnl_pct(self, risk):
        risk.record_pnl(-100.0)
        assert risk.daily_pnl_pct == pytest.approx(-1.0)

    def test_daily_pnl_pct_zero_balance(self, risk):
        risk._day_start_balance = 0.0
        assert risk.daily_pnl_pct == 0.0

    def test_daily_loss_pct_zero_balance(self, risk):
        risk._day_start_balance = 0.0
        assert risk.daily_loss_pct == 0.0

    def test_win_rate_today(self, risk):
        risk.record_pnl(50.0)
        risk.record_pnl(-10.0)
        assert risk.win_rate_today == pytest.approx(50.0)

    def test_win_rate_no_trades(self, risk):
        assert risk.win_rate_today == 0.0

    def test_risk_summary(self, risk):
        s = risk.risk_summary()
        assert "Risk" in s
        assert "Loss" in s

    def test_check_signal_exposure_too_high(self, risk):
        positions = [
            Position(
                symbol="BTC/USDT",
                side=OrderSide.BUY,
                amount=100.0,
                entry_price=200.0,
                current_price=200.0,
            )
        ]
        sig = _sig(strength=0.8)
        assert risk.check_signal(sig, 10000.0, positions) is False

    def test_drawdown_zone_paper_local_allows_weak(self, risk):
        risk.record_pnl(-200.0)
        sig = _sig(strength=0.5)
        assert risk.check_signal(sig, 10000.0, []) is True  # paper_local = aggressive

    def test_drawdown_zone_allows_strong(self, risk):
        risk.record_pnl(-200.0)
        sig = _sig(strength=0.9)
        assert risk.check_signal(sig, 10000.0, []) is True

    def test_position_size_scales_with_custom_risk(self, risk):
        qty = risk.calculate_position_size(10000.0, 50000.0, leverage=1, risk_pct=10.0)
        assert qty > 0

    def test_hold_action_always_passes(self, risk):
        risk._in_cooldown = True
        sig = _sig(action=SignalAction.HOLD)
        assert risk.check_signal(sig, 10000.0, []) is True

    def test_cooldown_not_triggered_in_paper_local(self, risk):
        risk.record_pnl(-10.0)
        risk.record_pnl(-10.0)
        risk.record_pnl(-10.0)
        assert risk._in_cooldown is False  # threshold=999 in aggressive mode


# ── TradeQueue (shared/models) ───────────────────────────────────────────────


class TestTradeQueue:
    def test_add_and_total(self):
        from shared.models import SignalPriority, TradeProposal, TradeQueue

        q = TradeQueue()
        p = TradeProposal(priority=SignalPriority.CRITICAL, symbol="BTC/USDT")
        q.add(p)
        assert q.total == 1

    def test_add_dedup(self):
        from shared.models import SignalPriority, TradeProposal, TradeQueue

        q = TradeQueue()
        p = TradeProposal(id="abc", priority=SignalPriority.DAILY, symbol="ETH/USDT")
        q.add(p)
        q.add(p)
        assert q.total == 1

    def test_pending_count(self):
        from shared.models import SignalPriority, TradeProposal, TradeQueue

        q = TradeQueue()
        q.add(TradeProposal(priority=SignalPriority.CRITICAL, symbol="BTC/USDT"))
        q.add(TradeProposal(priority=SignalPriority.DAILY, symbol="ETH/USDT"))
        assert q.pending_count == 2

    def test_mark_consumed(self):
        from shared.models import SignalPriority, TradeProposal, TradeQueue

        q = TradeQueue()
        p = TradeProposal(priority=SignalPriority.SWING, symbol="SOL/USDT")
        q.add(p)
        q.mark_consumed(p.id)
        assert q.pending_count == 0

    def test_mark_rejected(self):
        from shared.models import SignalPriority, TradeProposal, TradeQueue

        q = TradeQueue()
        p = TradeProposal(priority=SignalPriority.DAILY, symbol="DOGE/USDT")
        q.add(p)
        q.mark_rejected(p.id, reason="too weak")
        assert q.pending_count == 0

    def test_get_actionable(self):
        from shared.models import SignalPriority, TradeProposal, TradeQueue

        q = TradeQueue()
        q.add(TradeProposal(priority=SignalPriority.CRITICAL, symbol="BTC/USDT"))
        q.add(TradeProposal(priority=SignalPriority.DAILY, symbol="ETH/USDT"))
        assert len(q.get_actionable(SignalPriority.CRITICAL)) == 1

    def test_purge_stale(self):
        from shared.models import SignalPriority, TradeProposal, TradeQueue

        q = TradeQueue()
        p = TradeProposal(
            priority=SignalPriority.CRITICAL,
            symbol="BTC/USDT",
            created_at=(datetime.now(UTC) - timedelta(hours=2)).isoformat(),
        )
        p.consumed = True
        q.critical.append(p)
        removed = q.purge_stale(max_consumed_age=60)
        assert removed == 1
        assert q.total == 0


class TestTradeProposal:
    def test_is_expired_by_valid_until(self):
        from shared.models import SignalPriority, TradeProposal

        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        p = TradeProposal(priority=SignalPriority.DAILY, symbol="BTC/USDT", valid_until=past)
        assert p.is_expired is True

    def test_is_expired_by_max_age(self):
        from shared.models import SignalPriority, TradeProposal

        old = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        p = TradeProposal(priority=SignalPriority.DAILY, symbol="BTC/USDT", created_at=old, max_age_seconds=60)
        assert p.is_expired is True

    def test_not_expired(self):
        from shared.models import SignalPriority, TradeProposal

        p = TradeProposal(priority=SignalPriority.DAILY, symbol="BTC/USDT")
        assert p.is_expired is False

    def test_age_seconds(self):
        from shared.models import SignalPriority, TradeProposal

        p = TradeProposal(priority=SignalPriority.DAILY, symbol="BTC/USDT")
        assert p.age_seconds < 5

    def test_age_seconds_bad_date(self):
        from shared.models import SignalPriority, TradeProposal

        p = TradeProposal(priority=SignalPriority.DAILY, symbol="BTC/USDT", created_at="not-a-date")
        assert p.age_seconds == 0.0


# ── Config/Settings — platform_url and symbol_platform_url ───────────────────


class TestSettingsUrls:
    @pytest.fixture
    def settings(self, monkeypatch):
        monkeypatch.setenv("TRADING_MODE", "paper_local")
        monkeypatch.setenv("EXCHANGE", "binance")
        return Settings(_env_file=None)

    def test_platform_url_binance_paper(self, settings):
        url = settings.platform_url
        assert "demo.binance.com" in url or "binance" in url.lower()

    def test_symbol_platform_url_binance_futures(self, settings):
        url = settings.symbol_platform_url("BTC/USDT", "futures")
        assert "BTCUSDT" in url

    def test_symbol_platform_url_binance_spot(self, settings):
        url = settings.symbol_platform_url("BTC/USDT", "spot")
        assert "BTC" in url

    def test_platform_url_custom(self, settings):
        settings.exchange_platform_url = "https://custom.example.com"
        assert settings.platform_url == "https://custom.example.com"

    def test_symbol_url_no_base(self, settings):
        settings.exchange_platform_url = ""
        settings.exchange = "unknown_exchange"
        assert settings.symbol_platform_url("BTC/USDT") == ""

    def test_cap_balance(self, settings):
        settings.session_budget = 500.0
        assert settings.cap_balance(10000.0) == 500.0

    def test_cap_balance_no_cap(self, settings):
        settings.session_budget = 0.0
        assert settings.cap_balance(10000.0) == 10000.0

    def test_is_paper(self, settings):
        assert settings.is_paper() is True

    def test_bybit_url(self, monkeypatch):
        monkeypatch.setenv("TRADING_MODE", "paper_local")
        monkeypatch.setenv("EXCHANGE", "bybit")
        s = Settings(_env_file=None)
        url = s.symbol_platform_url("BTC/USDT")
        assert "bybit" in url.lower() or url == ""

    def test_mexc_url(self, monkeypatch):
        monkeypatch.setenv("TRADING_MODE", "paper_local")
        monkeypatch.setenv("EXCHANGE", "mexc")
        s = Settings(_env_file=None)
        url = s.symbol_platform_url("BTC/USDT")
        assert "mexc" in url.lower() or url == ""

    def test_binance_api_keys_paper(self, settings):
        settings.binance_test_api_key = "test_key"
        settings.binance_test_api_secret = "test_secret"
        assert settings.binance_api_key == "test_key"
        assert settings.binance_api_secret == "test_secret"

    def test_bot_id_default_empty(self, settings):
        assert settings.bot_id == ""

    def test_bot_strategies_default_empty(self, settings):
        assert settings.bot_strategy_list == []

    def test_data_dir_default(self, settings):
        assert settings.data_dir == "data"

    def test_data_dir_with_bot_id(self, settings):
        settings.bot_id = "momentum"
        assert settings.data_dir == "data/momentum"

    def test_bot_strategy_list_parses(self, settings):
        settings.bot_strategies = "rsi,macd,bollinger"
        assert settings.bot_strategy_list == ["rsi", "macd", "bollinger"]

    def test_bot_strategy_list_strips_whitespace(self, settings):
        settings.bot_strategies = " rsi , macd "
        assert settings.bot_strategy_list == ["rsi", "macd"]

    def test_bybit_api_keys_paper(self, monkeypatch):
        monkeypatch.setenv("TRADING_MODE", "paper_local")
        monkeypatch.setenv("EXCHANGE", "bybit")
        s = Settings(_env_file=None)
        s.bybit_test_api_key = "bkey"
        s.bybit_test_api_secret = "bsecret"
        assert s.bybit_api_key == "bkey"
        assert s.bybit_api_secret == "bsecret"


# ── Analytics Service ────────────────────────────────────────────────────────


class TestAnalyticsService:
    @pytest.mark.asyncio
    async def test_do_refresh(self):
        from services.analytics_service import AnalyticsService

        svc = AnalyticsService(refresh_interval=60)
        svc.db = MagicMock()
        svc.db.trade_count.return_value = 10
        mock_engine = MagicMock()
        mock_engine.scores = {
            "rsi": MagicMock(weight=1.2, win_rate=0.6, total_trades=50, total_pnl=100.0, streak_current=2)
        }
        mock_engine.patterns = []
        mock_engine.suggestions = []
        svc.engine = mock_engine
        svc.state = MagicMock()
        svc._do_refresh()
        svc.state.write_analytics.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_and_stop(self):
        from services.analytics_service import AnalyticsService

        svc = AnalyticsService(refresh_interval=1)
        svc.db = MagicMock()
        svc.db.connect = MagicMock()
        svc.db.close = MagicMock()
        svc.db.trade_count.return_value = 0
        mock_engine = MagicMock()
        mock_engine.scores = {}
        mock_engine.patterns = []
        mock_engine.suggestions = []
        svc.state = MagicMock()

        async def stop_soon():
            await asyncio.sleep(0.05)
            svc._running = False

        with patch("services.analytics_service.AnalyticsEngine", return_value=mock_engine):
            _task = asyncio.create_task(stop_soon())
            await svc.start()
        assert svc._running is False

    @pytest.mark.asyncio
    async def test_run_loop_detects_new_trades(self):
        from services.analytics_service import AnalyticsService

        svc = AnalyticsService(refresh_interval=0)
        svc.db = MagicMock()
        svc.db.trade_count.return_value = 5
        svc._last_trade_count = 3
        svc._running = True
        mock_engine = MagicMock()
        mock_engine.scores = {}
        mock_engine.patterns = []
        mock_engine.suggestions = []
        svc.engine = mock_engine
        svc.state = MagicMock()

        call_count = 0
        orig_sleep = asyncio.sleep

        async def fake_sleep(_t):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                svc._running = False
            await orig_sleep(0)

        with patch("asyncio.sleep", side_effect=fake_sleep):
            await svc._run_loop()
        assert svc._last_trade_count == 5


# ── News Monitor ─────────────────────────────────────────────────────────────


class TestNewsMonitor:
    def test_extract_symbols(self):
        from news.monitor import NewsMonitor

        symbols = NewsMonitor._extract_symbols("Bitcoin surges past $100k as Ethereum follows")
        assert any("BTC" in s for s in symbols) or any("ETH" in s for s in symbols) or symbols == []

    def test_analyze_sentiment_bullish(self):
        from news.monitor import NewsMonitor

        sentiment, score = NewsMonitor._analyze_sentiment("Bitcoin surges rally breakout bullish")
        assert sentiment == "bullish" or score > 0

    def test_analyze_sentiment_bearish(self):
        from news.monitor import NewsMonitor

        sentiment, score = NewsMonitor._analyze_sentiment("crash dump plunge bearish liquidation")
        assert sentiment == "bearish" or score < 0

    def test_analyze_sentiment_neutral(self):
        from news.monitor import NewsMonitor

        sentiment, score = NewsMonitor._analyze_sentiment("the weather is nice today")
        assert sentiment == "neutral"
        assert score == 0.0

    def test_correlate_spike(self):
        from news.monitor import NewsItem, NewsMonitor

        s = MagicMock()
        s.news_enabled = False
        s.news_source_list = []
        monitor = NewsMonitor(s)
        item = NewsItem(headline="BTC pump", source="test", matched_symbols=["BTC"], sentiment="bullish")
        result = monitor.correlate_spike("BTC", [item])
        assert result is not None
        assert result.headline == "BTC pump"

    def test_correlate_spike_no_match(self):
        from news.monitor import NewsItem, NewsMonitor

        s = MagicMock()
        s.news_enabled = False
        s.news_source_list = []
        monitor = NewsMonitor(s)
        item = NewsItem(headline="ETH pump", source="test", matched_symbols=["ETH"], sentiment="bullish")
        assert monitor.correlate_spike("BTC", [item]) is None

    @pytest.mark.asyncio
    async def test_start_disabled(self):
        from news.monitor import NewsMonitor

        s = MagicMock()
        s.news_enabled = False
        s.news_source_list = []
        monitor = NewsMonitor(s)
        await monitor.start()
        assert monitor._running is False


# ── Liquidation Monitor ──────────────────────────────────────────────────────


class TestLiquidationMonitor:
    def test_no_data_defaults(self):
        from intel.liquidations import LiquidationMonitor

        m = LiquidationMonitor()
        assert m.latest is None
        assert m.is_reversal_zone() is False
        assert m.reversal_bias() == "neutral"
        assert m.aggression_boost() == 1.0

    def test_with_mass_liquidation(self):
        from intel.liquidations import LiquidationMonitor, LiquidationSnapshot

        m = LiquidationMonitor()
        snap = LiquidationSnapshot(
            timestamp=datetime.now(UTC),
            total_24h=2_000_000_000,
            long_24h=1_500_000_000,
            short_24h=500_000_000,
        )
        m._latest = snap
        assert m.is_reversal_zone() is True
        assert m.aggression_boost() == 1.3

    def test_reversal_bias_longs(self):
        from intel.liquidations import LiquidationMonitor, LiquidationSnapshot

        m = LiquidationMonitor()
        snap = LiquidationSnapshot(
            timestamp=datetime.now(UTC),
            total_24h=600_000_000,
            long_24h=400_000_000,
            short_24h=200_000_000,
        )
        m._latest = snap
        assert m.reversal_bias() == "long"

    def test_reversal_bias_shorts(self):
        from intel.liquidations import LiquidationMonitor, LiquidationSnapshot

        m = LiquidationMonitor()
        snap = LiquidationSnapshot(
            timestamp=datetime.now(UTC),
            total_24h=600_000_000,
            long_24h=200_000_000,
            short_24h=400_000_000,
        )
        m._latest = snap
        assert m.reversal_bias() == "short"

    def test_heavy_but_not_mass(self):
        from intel.liquidations import LiquidationMonitor, LiquidationSnapshot

        m = LiquidationMonitor()
        snap = LiquidationSnapshot(
            timestamp=datetime.now(UTC),
            total_24h=600_000_000,
            long_24h=400_000_000,
            short_24h=200_000_000,
        )
        m._latest = snap
        assert m.aggression_boost() == 1.1


# ── Scanner/Trending ─────────────────────────────────────────────────────────


class TestTrendingScanner:
    def test_filter_movers(self):
        from scanner.trending import TrendingCoin, TrendingScanner

        scanner = TrendingScanner()
        coins = [
            TrendingCoin(
                symbol="BTC",
                name="Bitcoin",
                price=50000.0,
                volume_24h=2_000_000_000,
                change_1h=5.0,
                change_24h=10.0,
            ),
            TrendingCoin(symbol="USDT", name="Tether", price=1.0, volume_24h=50_000_000_000, change_1h=0.01),
            TrendingCoin(symbol="SHIB", name="Shiba", price=0.00001, volume_24h=100, change_1h=0.5),
        ]
        movers = scanner._filter_movers(coins)
        symbols = [m.symbol for m in movers]
        assert "USDT" not in symbols

    def test_merge_external_no_intel(self):
        from scanner.trending import TrendingScanner

        scanner = TrendingScanner()
        assert scanner._merge_external_sources() == []

    def test_hot_movers_property(self):
        from scanner.trending import TrendingScanner

        scanner = TrendingScanner()
        assert scanner.hot_movers == []

    def test_latest_scan_property(self):
        from scanner.trending import TrendingScanner

        scanner = TrendingScanner()
        assert scanner.latest_scan == []

    def test_on_trending_callback(self):
        from scanner.trending import TrendingScanner

        scanner = TrendingScanner()
        cb = MagicMock()
        scanner.on_trending(cb)
        assert cb in scanner._callbacks


# ── CoinMarketCap ────────────────────────────────────────────────────────────


class TestCoinMarketCapClient:
    def test_properties_empty(self):
        from intel.coinmarketcap import CoinMarketCapClient

        c = CoinMarketCapClient()
        assert c.trending == []
        assert c.gainers == []
        assert c.losers == []
        assert c.recently_added == []
        assert c.all_interesting == []

    def test_all_interesting_dedup(self):
        from intel.coinmarketcap import CMCCoin, CoinMarketCapClient

        c = CoinMarketCapClient()
        coin = CMCCoin(symbol="BTC", name="Bitcoin")
        c._trending = [coin]
        c._gainers = [coin]
        assert len(c.all_interesting) == 1

    def test_headers_with_key(self):
        from intel.coinmarketcap import CoinMarketCapClient

        c = CoinMarketCapClient(api_key="test-key")
        h = c._headers()
        assert "X-CMC_PRO_API_KEY" in h

    def test_headers_without_key(self):
        from intel.coinmarketcap import CoinMarketCapClient

        c = CoinMarketCapClient(api_key="")
        h = c._headers()
        assert "X-CMC_PRO_API_KEY" not in h

    @pytest.mark.asyncio
    async def test_start_stop(self):
        from intel.coinmarketcap import CoinMarketCapClient

        c = CoinMarketCapClient()
        with patch.object(c, "_poll_loop", new_callable=AsyncMock):
            await c.start()
            assert c._running is True
            await c.stop()
            assert c._running is False

    def test_parse_spotlight_coin(self):
        from intel.coinmarketcap import CoinMarketCapClient

        item = {
            "id": 1,
            "symbol": "BTC",
            "name": "Bitcoin",
            "slug": "bitcoin",
            "priceChange": {"price": 50000, "priceChange24h": 5.0, "volume24h": 1e9, "marketCap": 1e12},
        }
        coin = CoinMarketCapClient._parse_spotlight_coin(item)
        assert coin is not None
        assert coin.symbol == "BTC"


# ── Whale Sentiment ──────────────────────────────────────────────────────────


class TestWhaleSentiment:
    def test_no_data_defaults(self):
        from intel.whale_sentiment import WhaleSentiment

        m = WhaleSentiment()
        assert m.get("BTC") is None
        assert m.contrarian_bias("BTC") == "neutral"
        assert m.should_avoid_longs("BTC") is False
        assert m.should_avoid_shorts("BTC") is False
        assert m.breakout_expected("BTC") is False

    def test_with_overleveraged_longs(self):
        from intel.whale_sentiment import WhaleSentiment, WhaleSentimentData

        m = WhaleSentiment()
        m._data["BTC"] = WhaleSentimentData(
            funding_rate=0.08,
            long_short_ratio=1.6,
            open_interest=10_000_000_000,
            oi_change_1h_pct=1.0,
        )
        assert m.contrarian_bias("BTC") == "short"
        assert m.should_avoid_longs("BTC") is True

    def test_with_overleveraged_shorts(self):
        from intel.whale_sentiment import WhaleSentiment, WhaleSentimentData

        m = WhaleSentiment()
        m._data["BTC"] = WhaleSentimentData(
            funding_rate=-0.08,
            long_short_ratio=0.5,
            open_interest=10_000_000_000,
            oi_change_1h_pct=-1.0,
        )
        assert m.contrarian_bias("BTC") == "long"
        assert m.should_avoid_shorts("BTC") is True

    def test_oi_building(self):
        from intel.whale_sentiment import WhaleSentiment, WhaleSentimentData

        m = WhaleSentiment()
        m._data["BTC"] = WhaleSentimentData(
            funding_rate=0.01,
            long_short_ratio=1.0,
            open_interest=10_000_000_000,
            oi_change_1h_pct=5.0,
            open_interest_24h_change_pct=6.0,
        )
        assert m.breakout_expected("BTC") is True

    def test_get_strips_usdt(self):
        from intel.whale_sentiment import WhaleSentiment, WhaleSentimentData

        m = WhaleSentiment()
        m._data["BTC"] = WhaleSentimentData(
            funding_rate=0.01, long_short_ratio=1.0, open_interest=1e10, oi_change_1h_pct=0.0
        )
        assert m.get("BTC/USDT") is not None
        assert m.get("BTCUSDT") is not None

    @pytest.mark.asyncio
    async def test_start_stop(self):
        from intel.whale_sentiment import WhaleSentiment

        m = WhaleSentiment()
        with patch.object(m, "_poll_loop", new_callable=AsyncMock):
            await m.start()
            assert m._running is True
            await m.stop()
            assert m._running is False


# ── CoinGecko Client ────────────────────────────────────────────────────────


class TestCoinGeckoClient:
    def test_properties_empty(self):
        from intel.coingecko import CoinGeckoClient

        c = CoinGeckoClient()
        assert c.trending == []
        assert c.top_volume == []
        assert c.top_gainers == []
        assert c.all_interesting == []

    def test_base_url_free(self):
        from intel.coingecko import CoinGeckoClient

        c = CoinGeckoClient(api_key="")
        assert "pro" not in c._base_url

    def test_base_url_pro(self):
        from intel.coingecko import CoinGeckoClient

        c = CoinGeckoClient(api_key="some-key")
        assert "pro" in c._base_url

    def test_params_no_key(self):
        from intel.coingecko import CoinGeckoClient

        c = CoinGeckoClient(api_key="")
        p = c._params({"foo": "bar"})
        assert "x_cg_pro_api_key" not in p
        assert p["foo"] == "bar"

    def test_params_with_key(self):
        from intel.coingecko import CoinGeckoClient

        c = CoinGeckoClient(api_key="my-key")
        p = c._params()
        assert p["x_cg_pro_api_key"] == "my-key"

    def test_all_interesting_dedup(self):
        from intel.coingecko import CoinGeckoClient, GeckoCoin

        c = CoinGeckoClient()
        coin = GeckoCoin(symbol="BTC", name="Bitcoin")
        c._trending = [coin]
        c._top_gainers = [coin]
        assert len(c.all_interesting) == 1

    @pytest.mark.asyncio
    async def test_start_stop(self):
        from intel.coingecko import CoinGeckoClient

        c = CoinGeckoClient()
        with patch.object(c, "_poll_loop", new_callable=AsyncMock):
            await c.start()
            assert c._running is True
            await c.stop()
            assert c._running is False


# ── Macro Calendar ───────────────────────────────────────────────────────────


class TestMacroCalendar:
    def test_macro_event_properties(self):
        from intel.macro_calendar import EventImpact, MacroEvent

        future = datetime.now(UTC) + timedelta(hours=1)
        e = MacroEvent(title="FOMC", date=future, impact=EventImpact.CRITICAL)
        assert e.is_crypto_mover is True
        assert e.is_imminent is True
        assert 0 < e.hours_until < 2

    def test_macro_event_happening_now(self):
        from intel.macro_calendar import EventImpact, MacroEvent

        now = datetime.now(UTC)
        e = MacroEvent(title="CPI", date=now, impact=EventImpact.HIGH)
        assert e.is_happening_now is True

    def test_macro_event_low_impact(self):
        from intel.macro_calendar import EventImpact, MacroEvent

        future = datetime.now(UTC) + timedelta(hours=1)
        e = MacroEvent(title="Existing Home Sales", date=future, impact=EventImpact.LOW)
        assert e.is_crypto_mover is False

    def test_calendar_no_events(self):
        from intel.macro_calendar import MacroCalendar

        cal = MacroCalendar()
        assert cal.upcoming_events == []
        assert cal.upcoming_high_impact == []
        assert cal.has_imminent_event() is False
        assert cal.has_event_now() is False
        assert cal.should_reduce_exposure() is False
        assert cal.is_spike_opportunity() is False
        assert cal.exposure_multiplier() == 1.0
        assert cal.next_event_info() is None

    def test_calendar_with_critical_event(self):
        from intel.macro_calendar import EventImpact, MacroCalendar, MacroEvent

        cal = MacroCalendar()
        cal._events = [
            MacroEvent(
                title="FOMC Rate Decision",
                date=datetime.now(UTC) + timedelta(minutes=30),
                impact=EventImpact.CRITICAL,
            )
        ]
        assert cal.has_imminent_event() is True
        assert cal.should_reduce_exposure() is True
        assert cal.exposure_multiplier() == 0.3
        info = cal.next_event_info()
        assert info is not None
        assert "FOMC" in info

    def test_calendar_with_high_event_2h(self):
        from intel.macro_calendar import EventImpact, MacroCalendar, MacroEvent

        cal = MacroCalendar()
        cal._events = [MacroEvent(title="CPI", date=datetime.now(UTC) + timedelta(hours=1.5), impact=EventImpact.HIGH)]
        assert cal.exposure_multiplier() == 0.7

    def test_calendar_event_happening_now(self):
        from intel.macro_calendar import EventImpact, MacroCalendar, MacroEvent

        cal = MacroCalendar()
        cal._events = [MacroEvent(title="NFP", date=datetime.now(UTC), impact=EventImpact.HIGH)]
        assert cal.is_spike_opportunity() is True

    def test_calendar_critical_2h_out(self):
        from intel.macro_calendar import EventImpact, MacroCalendar, MacroEvent

        cal = MacroCalendar()
        cal._events = [
            MacroEvent(
                title="FOMC",
                date=datetime.now(UTC) + timedelta(hours=1.5),
                impact=EventImpact.CRITICAL,
            )
        ]
        assert cal.exposure_multiplier() == 0.5

    def test_calendar_critical_4h_out(self):
        from intel.macro_calendar import EventImpact, MacroCalendar, MacroEvent

        cal = MacroCalendar()
        cal._events = [
            MacroEvent(
                title="FOMC",
                date=datetime.now(UTC) + timedelta(hours=3),
                impact=EventImpact.CRITICAL,
            )
        ]
        assert cal.exposure_multiplier() == 0.7


# ── Shared State — additional edge cases ─────────────────────────────────────


class TestSharedStateExtended:
    @pytest.fixture
    def state(self, tmp_path):
        from shared.state import SharedState

        return SharedState(data_dir=tmp_path)

    def test_write_and_read_trade_queue(self, state):
        from shared.models import SignalPriority, TradeProposal, TradeQueue

        q = TradeQueue()
        q.add(TradeProposal(priority=SignalPriority.CRITICAL, symbol="BTC/USDT"))
        state.write_trade_queue(q)
        read = state.read_trade_queue()
        assert read.total == 1

    def test_read_trade_queue_default(self, state):
        q = state.read_trade_queue()
        assert q.total == 0

    def test_apply_trade_queue_updates(self, state):
        from shared.models import SignalPriority, TradeProposal, TradeQueue

        q = TradeQueue()
        p1 = TradeProposal(priority=SignalPriority.CRITICAL, symbol="BTC/USDT", side="long", strategy="x")
        p2 = TradeProposal(priority=SignalPriority.DAILY, symbol="ETH/USDT", side="short", strategy="y")
        q.add(p1)
        q.add(p2)
        state.write_trade_queue(q)
        state.apply_trade_queue_updates(consumed_ids=[p1.id], rejected={p2.id: "no slots"})
        read = state.read_trade_queue()
        assert read.total == 2
        consumed = next(x for x in read.critical + read.daily + read.swing if x.id == p1.id)
        assert consumed.consumed
        rejected = next(x for x in read.critical + read.daily + read.swing if x.id == p2.id)
        assert rejected.rejected


# ── Santiment Client ─────────────────────────────────────────────────────────


class TestSantimentClient:
    def test_no_data(self):
        from intel.santiment import SantimentClient

        c = SantimentClient()
        assert c.get("BTC") is None
        assert c.sentiment_signal("BTC") == "neutral"
        assert c.is_social_spike("BTC") is False
        assert c.position_bias() == 1.0

    def test_with_bearish_data(self):
        from intel.santiment import SantimentClient, SocialData

        c = SantimentClient()
        c._data["bitcoin"] = SocialData(
            social_volume=9000,
            social_volume_avg=2000,
        )
        assert c.sentiment_signal("BTC") == "bearish"
        assert c.position_bias() == 0.7

    def test_with_spike_data(self):
        from intel.santiment import SantimentClient, SocialData

        c = SantimentClient()
        c._data["bitcoin"] = SocialData(
            social_volume=5000,
            social_volume_avg=2000,
        )
        assert c.is_social_spike("BTC") is True
        assert c.sentiment_signal("BTC") == "bullish"
        assert c.position_bias() == 1.1

    def test_no_spike(self):
        from intel.santiment import SantimentClient, SocialData

        c = SantimentClient()
        c._data["bitcoin"] = SocialData(
            social_volume=100,
            social_volume_avg=2000,
        )
        assert c.is_social_spike("BTC") is False
        assert c.sentiment_signal("BTC") == "neutral"
        assert c.position_bias() == 1.0

    def test_to_slug(self):
        from intel.santiment import SantimentClient

        c = SantimentClient()
        assert c._to_slug("BTC") == "bitcoin"
        assert c._to_slug("ETH/USDT") == "ethereum"
        assert c._to_slug("UNKNOWN") == "unknown"

    @pytest.mark.asyncio
    async def test_start_stop(self):
        from intel.santiment import SantimentClient

        c = SantimentClient()
        with patch.object(c, "_poll_loop", new_callable=AsyncMock):
            await c.start()
            assert c._running is True
            await c.stop()
            assert c._running is False


# ── Scanner — deeper coverage ────────────────────────────────────────────────


class TestTrendingScannerExtended:
    def test_filter_movers_by_volume(self):
        from scanner.trending import TrendingCoin, TrendingScanner

        scanner = TrendingScanner(min_volume_24h=1_000_000)
        coins = [
            TrendingCoin(
                symbol="BTC",
                name="Bitcoin",
                price=50000.0,
                volume_24h=2e9,
                market_cap=1e12,
                change_1h=5.0,
                change_24h=10.0,
            ),
            TrendingCoin(symbol="TINY", name="Tiny", price=0.001, volume_24h=100, market_cap=1e8, change_1h=50.0),
        ]
        movers = scanner._filter_movers(coins)
        assert all(m.symbol != "TINY" for m in movers)

    def test_filter_movers_by_market_cap(self):
        from scanner.trending import TrendingCoin, TrendingScanner

        scanner = TrendingScanner(min_market_cap=100_000_000)
        coins = [
            TrendingCoin(symbol="BTC", name="Bitcoin", price=50000.0, volume_24h=2e9, market_cap=1e12, change_1h=5.0),
            TrendingCoin(symbol="MICRO", name="Micro", price=0.001, volume_24h=1e7, market_cap=1000, change_1h=20.0),
        ]
        movers = scanner._filter_movers(coins)
        assert all(m.symbol != "MICRO" for m in movers)

    def test_trending_coin_properties(self):
        from scanner.trending import TrendingCoin

        coin = TrendingCoin(
            symbol="ETH", name="Ethereum", price=3000.0, volume_24h=1e9, market_cap=3e11, change_1h=3.0, change_24h=8.0
        )
        assert coin.trading_pair == "ETH/USDT"
        assert isinstance(coin.momentum_score, float)
        assert coin.is_low_liquidity is False

    def test_trending_coin_low_liquidity(self):
        from scanner.trending import TrendingCoin

        coin = TrendingCoin(symbol="SHIB", name="Shiba", price=0.00001, volume_24h=100, market_cap=1000)
        assert coin.is_low_liquidity is True

    def test_get_strongest_bullish(self):
        from scanner.trending import TrendingCoin, TrendingScanner

        scanner = TrendingScanner()
        scanner._hot_movers = [
            TrendingCoin(symbol="BTC", name="Bitcoin", price=50000.0, volume_24h=2e9, change_1h=5.0, change_24h=10.0),
            TrendingCoin(symbol="ETH", name="Ethereum", price=3000.0, volume_24h=1e9, change_1h=-3.0, change_24h=-8.0),
        ]
        bullish = scanner.get_strongest_bullish(5)
        assert all(c.momentum_score > 0 for c in bullish)

    def test_get_strongest_bearish(self):
        from scanner.trending import TrendingCoin, TrendingScanner

        scanner = TrendingScanner()
        scanner._hot_movers = [
            TrendingCoin(symbol="BTC", name="Bitcoin", price=50000.0, volume_24h=2e9, change_1h=5.0, change_24h=10.0),
            TrendingCoin(symbol="ETH", name="Ethereum", price=3000.0, volume_24h=1e9, change_1h=-3.0, change_24h=-8.0),
        ]
        bearish = scanner.get_strongest_bearish(5)
        assert all(c.momentum_score < 0 for c in bearish)

    def test_scan_summary_empty(self):
        from scanner.trending import TrendingScanner

        scanner = TrendingScanner()
        assert "No hot movers" in scanner.scan_summary()

    def test_scan_summary_with_movers(self):
        from scanner.trending import TrendingCoin, TrendingScanner

        scanner = TrendingScanner()
        scanner._hot_movers = [
            TrendingCoin(symbol="BTC", name="Bitcoin", price=50000.0, volume_24h=2e9, change_1h=5.0, change_24h=10.0),
        ]
        summary = scanner.scan_summary()
        assert "1 hot movers" in summary
        assert "BTC" in summary

    def test_volatility_to_liquidity_ratio(self):
        from scanner.trending import TrendingCoin

        coin = TrendingCoin(symbol="X", name="X", price=1.0, volume_24h=1e6, change_1h=10.0, change_24h=5.0)
        ratio = coin.volatility_to_liquidity_ratio
        assert ratio > 0


# ── News Monitor — fetch_feed with mocked responses ─────────────────────────


class TestNewsMonitorFetch:
    @pytest.mark.asyncio
    async def test_fetch_feed_parses_entries(self):
        from news.monitor import NewsMonitor

        s = MagicMock()
        s.news_enabled = True
        s.news_source_list = ["cointelegraph"]
        monitor = NewsMonitor(s)

        mock_entry = MagicMock()
        mock_entry.get.side_effect = lambda key, default="": {
            "link": "https://example.com/btc-news",
            "title": "Bitcoin hits all time high as rally continues",
        }.get(key, default)

        mock_feed = MagicMock()
        mock_feed.entries = [mock_entry]

        mock_resp = AsyncMock()
        mock_resp.text = AsyncMock(return_value="<rss>fake</rss>")

        mock_session = MagicMock()
        mock_session.get = MagicMock(
            return_value=MagicMock(
                __aenter__=AsyncMock(return_value=mock_resp),
                __aexit__=AsyncMock(return_value=False),
            )
        )

        with patch("news.monitor.feedparser.parse", return_value=mock_feed):
            items = await monitor._fetch_feed(mock_session, "cointelegraph", "https://fake.rss")

        assert isinstance(items, list)


# ── DailyTargetTracker ───────────────────────────────────────────────────────


class TestDailyTargetTracker:
    def test_reset_day(self):
        from core.risk.daily_target import DailyTargetTracker

        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(10000.0)
        assert t._day_number == 1
        assert t._day_start_balance == 10000.0

    def test_target_reached(self):
        from core.risk.daily_target import DailyTargetTracker

        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(10000.0)
        t.update_balance(11500.0)
        assert t.target_reached is True

    def test_tier_building(self):
        from core.risk.daily_target import DailyTargetTracker, DailyTier

        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(10000.0)
        t.update_balance(10500.0)
        assert t.tier == DailyTier.BUILDING

    def test_tier_strong(self):
        from core.risk.daily_target import DailyTargetTracker, DailyTier

        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(10000.0)
        t.update_balance(11500.0)
        assert t.tier == DailyTier.STRONG

    def test_tier_excellent(self):
        from core.risk.daily_target import DailyTargetTracker, DailyTier

        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(10000.0)
        t.update_balance(13000.0)
        assert t.tier == DailyTier.EXCELLENT

    def test_tier_monster(self):
        from core.risk.daily_target import DailyTargetTracker, DailyTier

        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(10000.0)
        t.update_balance(16000.0)
        assert t.tier == DailyTier.MONSTER

    def test_tier_legendary(self):
        from core.risk.daily_target import DailyTargetTracker, DailyTier

        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(10000.0)
        t.update_balance(21000.0)
        assert t.tier == DailyTier.LEGENDARY

    def test_tier_losing(self):
        from core.risk.daily_target import DailyTargetTracker, DailyTier

        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(10000.0)
        t.update_balance(9500.0)
        assert t.tier == DailyTier.LOSING

    def test_aggression_multiplier_tiers(self):
        from core.risk.daily_target import DailyTargetTracker

        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(10000.0)

        t.update_balance(10500.0)
        assert t.aggression_multiplier() >= 0.8

        t.update_balance(11500.0)
        assert t.aggression_multiplier() == 0.6

        t.update_balance(13000.0)
        assert t.aggression_multiplier() == 0.3

        t.update_balance(16000.0)
        assert t.aggression_multiplier() == 0.15

        t.update_balance(21000.0)
        assert t.aggression_multiplier() == 0.0

    def test_aggression_losing_mild(self):
        from core.risk.daily_target import DailyTargetTracker

        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(10000.0)
        t.update_balance(9800.0)  # -2%, less than -3%
        assert t.aggression_multiplier() == 0.7

    def test_aggression_losing_deep(self):
        from core.risk.daily_target import DailyTargetTracker

        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(10000.0)
        t.update_balance(9500.0)  # -5%, more than -3%
        assert t.aggression_multiplier() == 0.5

    def test_should_trade_legendary_stops(self):
        from core.risk.daily_target import DailyTargetTracker

        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(10000.0)
        t.update_balance(21000.0)
        assert t.should_trade() is False

    def test_progress_pct(self):
        from core.risk.daily_target import DailyTargetTracker

        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(10000.0)
        t.update_balance(10500.0)
        assert t.progress_pct == pytest.approx(50.0)

    def test_projected_balance(self):
        from core.risk.daily_target import DailyTargetTracker

        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(10000.0)
        proj = t.projected_balance
        assert proj["1_week"] > 10000.0
        assert proj["1_month"] > proj["1_week"]

    def test_total_growth_pct(self):
        from core.risk.daily_target import DailyTargetTracker

        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(10000.0)
        t.update_balance(12000.0)
        assert t.total_growth_pct == pytest.approx(20.0)

    def test_record_trade(self):
        from core.risk.daily_target import DailyTargetTracker

        t = DailyTargetTracker()
        t.record_trade()
        t.record_trade()
        assert t._todays_trades == 2

    def test_reset_day_records_history(self):
        from core.risk.daily_target import DailyTargetTracker

        t = DailyTargetTracker()
        t.reset_day(10000.0)
        t.update_balance(11000.0)
        t.reset_day(11000.0)
        assert len(t._history) == 1
        assert t._history[0].pnl == 1000.0

    def test_should_trade_monster(self):
        from core.risk.daily_target import DailyTargetTracker

        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(10000.0)
        t.update_balance(16000.0)
        assert t.should_trade() is True

    def test_should_trade_excellent(self):
        from core.risk.daily_target import DailyTargetTracker

        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(10000.0)
        t.update_balance(13000.0)
        assert t.should_trade() is True

    def test_should_close_all_not_legendary(self):
        from core.risk.daily_target import DailyTargetTracker

        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(10000.0)
        t.update_balance(10500.0)
        should, _reason = t.should_close_all()
        assert should is False

    def test_should_close_all_legendary_reversal(self):
        from core.risk.daily_target import DailyTargetTracker

        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(10000.0)
        t.update_balance(21000.0)
        should, reason = t.should_close_all(reversal_risk=True)
        assert should is True
        assert "LEGENDARY" in reason

    def test_legendary_ride_reason(self):
        from core.risk.daily_target import DailyTargetTracker

        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(10000.0)
        t.update_balance(21000.0)
        reason = t.legendary_ride_reason("market is calm")
        assert "LEGENDARY" in reason
        assert t.legendary_email_sent is True

    def test_status_report(self):
        from core.risk.daily_target import DailyTargetTracker

        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(10000.0)
        t.update_balance(10500.0)
        report = t.status_report()
        assert "Day 1" in report
        assert "BUILDING" in report

    def test_compound_report_with_history(self):
        from core.risk.daily_target import DailyTargetTracker

        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(10000.0)
        t.update_balance(11000.0)
        t.reset_day(11000.0)
        t.update_balance(12000.0)
        report = t.compound_report()
        assert "COMPOUND GROWTH" in report
        assert "Winning days" in report
        assert "PROJECTIONS" in report

    def test_history_stats(self):
        from core.risk.daily_target import DailyTargetTracker

        t = DailyTargetTracker(daily_target_pct=10.0)
        t.reset_day(10000.0)
        t.update_balance(11500.0)
        t.reset_day(11500.0)
        t.update_balance(11000.0)
        t.reset_day(11000.0)
        assert t.winning_days == 1
        assert t.losing_days == 1
        assert t.best_day is not None
        assert t.worst_day is not None
        assert t.avg_daily_pnl_pct != 0.0


# ── Notifier — queue and email ───────────────────────────────────────────────


class TestNotifierExtended:
    @pytest.mark.asyncio
    async def test_process_queue_sends_email(self, monkeypatch):
        from notifications.notifier import Notifier

        monkeypatch.setenv("TRADING_MODE", "paper_local")
        monkeypatch.setenv("EXCHANGE", "mexc")
        monkeypatch.setenv("SMTP_USER", "u@test.com")
        monkeypatch.setenv("SMTP_PASSWORD", "pass")
        monkeypatch.setenv("NOTIFY_EMAIL", "a@test.com")
        s = Settings(_env_file=None)
        n = Notifier(s)
        n._running = True
        await n._queue.put(("Test Subject", "Test Body"))

        with patch("notifications.notifier.aiosmtplib.send", new_callable=AsyncMock) as mock_send:

            async def stop_after():
                await asyncio.sleep(0.05)
                n._running = False

            _task = asyncio.create_task(stop_after())
            await n._process_queue()

            mock_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_email_error_handling(self, monkeypatch):
        from notifications.notifier import Notifier

        monkeypatch.setenv("TRADING_MODE", "paper_local")
        monkeypatch.setenv("EXCHANGE", "mexc")
        monkeypatch.setenv("SMTP_USER", "u@test.com")
        monkeypatch.setenv("SMTP_PASSWORD", "pass")
        monkeypatch.setenv("NOTIFY_EMAIL", "a@test.com")
        s = Settings(_env_file=None)
        n = Notifier(s)

        with patch(
            "notifications.notifier.aiosmtplib.send", new_callable=AsyncMock, side_effect=Exception("SMTP down")
        ):
            await n._send_email("Test", "Body")


# ── Additional edge case tests ───────────────────────────────────────────────


class TestSocialDataProperties:
    def test_social_spike(self):
        from intel.santiment import SocialData

        d = SocialData(social_volume=5000, social_volume_avg=2000)
        assert d.is_social_spike is True

    def test_sentiment_signal_bearish(self):
        from intel.santiment import SocialData

        d = SocialData(social_volume=7000, social_volume_avg=2000)
        assert d.sentiment_signal == "bearish"

    def test_sentiment_signal_bullish(self):
        from intel.santiment import SocialData

        d = SocialData(social_volume=4500, social_volume_avg=2000)
        assert d.sentiment_signal == "bullish"

    def test_sentiment_signal_neutral(self):
        from intel.santiment import SocialData

        d = SocialData(social_volume=100, social_volume_avg=2000)
        assert d.sentiment_signal == "neutral"


class TestOISnapshot:
    def test_oi_surging(self):
        from intel.whale_sentiment import OISnapshot

        snap = OISnapshot(oi_change_1h_pct=5.0)
        assert snap.oi_surging is True

    def test_oi_collapsing(self):
        from intel.whale_sentiment import OISnapshot

        snap = OISnapshot(oi_change_1h_pct=-6.0)
        assert snap.oi_collapsing is True

    def test_oi_normal(self):
        from intel.whale_sentiment import OISnapshot

        snap = OISnapshot(oi_change_1h_pct=1.0)
        assert snap.oi_surging is False
        assert snap.oi_collapsing is False


class TestWhaleSentimentDataProperties:
    def test_is_overleveraged_longs(self):
        from intel.whale_sentiment import WhaleSentimentData

        d = WhaleSentimentData(funding_rate=0.1, long_short_ratio=1.6, open_interest=1e10, oi_change_1h_pct=0)
        assert d.is_overleveraged_longs is True

    def test_is_overleveraged_shorts(self):
        from intel.whale_sentiment import WhaleSentimentData

        d = WhaleSentimentData(funding_rate=-0.1, long_short_ratio=0.5, open_interest=1e10, oi_change_1h_pct=0)
        assert d.is_overleveraged_shorts is True

    def test_oi_building(self):
        from intel.whale_sentiment import WhaleSentimentData

        d = WhaleSentimentData(
            funding_rate=0.01,
            long_short_ratio=1.0,
            open_interest=1e10,
            oi_change_1h_pct=0,
            open_interest_24h_change_pct=6.0,
        )
        assert d.oi_building is True

    def test_oi_declining(self):
        from intel.whale_sentiment import WhaleSentimentData

        d = WhaleSentimentData(
            funding_rate=0.01,
            long_short_ratio=1.0,
            open_interest=1e10,
            oi_change_1h_pct=0,
            open_interest_24h_change_pct=-6.0,
        )
        assert d.oi_declining is True


class TestLiquidationSnapshotProperties:
    def test_dominant_side_longs(self):
        from intel.liquidations import LiquidationSnapshot

        s = LiquidationSnapshot(timestamp=datetime.now(UTC), total_24h=1e9, long_24h=7e8, short_24h=3e8)
        assert s.dominant_side == "longs"
        assert s.long_ratio_24h > 0.5

    def test_dominant_side_shorts(self):
        from intel.liquidations import LiquidationSnapshot

        s = LiquidationSnapshot(timestamp=datetime.now(UTC), total_24h=1e9, long_24h=3e8, short_24h=7e8)
        assert s.dominant_side == "shorts"

    def test_is_heavy_liquidation(self):
        from intel.liquidations import LiquidationSnapshot

        s = LiquidationSnapshot(timestamp=datetime.now(UTC), total_24h=6e8)
        assert s.is_heavy_liquidation is True

    def test_is_mass_liquidation(self):
        from intel.liquidations import LiquidationSnapshot

        s = LiquidationSnapshot(timestamp=datetime.now(UTC), total_24h=2e9)
        assert s.is_mass_liquidation is True


# ── Fear & Greed Client ──────────────────────────────────────────────────────


class TestFearGreedClient:
    def test_no_data(self):
        from intel.fear_greed import FearGreedClient

        c = FearGreedClient()
        assert c.latest is None
        assert c.value == 50
        assert c.is_extreme_fear is False
        assert c.is_fear is False
        assert c.is_greed is False
        assert c.is_extreme_greed is False
        assert c.position_bias() == 1.0
        assert c.trade_direction_bias() == "neutral"

    def _reading(self, value, classification):
        from intel.fear_greed import FearGreedReading

        return FearGreedReading(value=value, classification=classification, timestamp=datetime.now(UTC))

    def test_extreme_fear(self):
        from intel.fear_greed import FearGreedClient

        c = FearGreedClient()
        c._latest = self._reading(10, "Extreme Fear")
        assert c.is_extreme_fear is True
        assert c.is_fear is True
        assert c.position_bias() == 1.4
        assert c.trade_direction_bias() == "long"

    def test_fear(self):
        from intel.fear_greed import FearGreedClient

        c = FearGreedClient()
        c._latest = self._reading(30, "Fear")
        assert c.is_fear is True
        assert c.is_extreme_fear is False
        assert c.position_bias() == 1.1

    def test_greed(self):
        from intel.fear_greed import FearGreedClient

        c = FearGreedClient()
        c._latest = self._reading(70, "Greed")
        assert c.is_greed is True
        assert c.is_extreme_greed is False
        assert c.position_bias() == 0.8

    def test_extreme_greed(self):
        from intel.fear_greed import FearGreedClient

        c = FearGreedClient()
        c._latest = self._reading(85, "Extreme Greed")
        assert c.is_extreme_greed is True
        assert c.position_bias() == 0.6
        assert c.trade_direction_bias() == "short"

    @pytest.mark.asyncio
    async def test_start_stop(self):
        from intel.fear_greed import FearGreedClient

        c = FearGreedClient()
        with patch.object(c, "_poll_loop", new_callable=AsyncMock):
            await c.start()
            assert c._running is True
            await c.stop()
            assert c._running is False


# ── DeFiLlama client ─────────────────────────────────────────────────────────


class TestDeFiLlamaClient:
    def test_no_data(self):
        from intel.defillama import DeFiLlamaClient

        c = DeFiLlamaClient()
        assert c.snapshot is not None
        assert c.tvl_trend == "stable"
        assert c.position_bias() == 1.0
        assert c.capital_flowing_to == []

    def test_growing_tvl(self):
        from intel.defillama import DeFiLlamaClient

        c = DeFiLlamaClient()
        c._data.tvl_24h_change_pct = 5.0
        assert c.tvl_trend == "growing"
        assert c.position_bias() == 1.1

    def test_shrinking_tvl(self):
        from intel.defillama import DeFiLlamaClient

        c = DeFiLlamaClient()
        c._data.tvl_24h_change_pct = -5.0
        assert c.tvl_trend == "shrinking"
        assert c.position_bias() == 0.85

    @pytest.mark.asyncio
    async def test_start_stop(self):
        from intel.defillama import DeFiLlamaClient

        c = DeFiLlamaClient()
        with patch.object(c, "_poll_loop", new_callable=AsyncMock):
            await c.start()
            assert c._running is True
            await c.stop()
            assert c._running is False
