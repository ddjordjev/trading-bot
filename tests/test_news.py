"""Tests for news/monitor.py."""
from __future__ import annotations

import pytest

from news.monitor import NewsItem, NewsMonitor, _SYMBOL_PATTERNS


@pytest.fixture()
def settings(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("EXCHANGE", "mexc")
    monkeypatch.setenv("NEWS_ENABLED", "true")
    monkeypatch.setenv("NEWS_SOURCES", "coindesk,cointelegraph")
    from config.settings import Settings
    return Settings()


class TestNewsItem:
    def test_defaults(self):
        item = NewsItem(headline="BTC surges", source="coindesk")
        assert item.sentiment == "neutral"
        assert item.sentiment_score == 0.0
        assert item.matched_symbols == []


class TestNewsMonitor:
    def test_init(self, settings):
        nm = NewsMonitor(settings)
        assert nm.enabled is True
        assert "coindesk" in nm.sources

    def test_disabled(self, monkeypatch):
        monkeypatch.setenv("TRADING_MODE", "paper")
        monkeypatch.setenv("EXCHANGE", "mexc")
        monkeypatch.setenv("NEWS_ENABLED", "false")
        from config.settings import Settings
        nm = NewsMonitor(Settings())
        assert nm.enabled is False

    def test_on_news_callback(self, settings):
        nm = NewsMonitor(settings)
        cb = lambda item: None
        nm.on_news(cb)
        assert len(nm._callbacks) == 1

    def test_extract_symbols_btc(self):
        syms = NewsMonitor._extract_symbols("Bitcoin surges 10%!")
        assert "BTC/USDT" in syms

    def test_extract_symbols_eth(self):
        syms = NewsMonitor._extract_symbols("ETH and Ethereum hit new highs")
        assert "ETH/USDT" in syms

    def test_extract_symbols_multi(self):
        syms = NewsMonitor._extract_symbols("SOL and DOGE rally together")
        assert "SOL/USDT" in syms
        assert "DOGE/USDT" in syms

    def test_extract_symbols_none(self):
        syms = NewsMonitor._extract_symbols("Stock market crashes")
        assert syms == []

    def test_analyze_sentiment_bullish(self):
        sentiment, score = NewsMonitor._analyze_sentiment("Bitcoin rally pump surge gains")
        assert sentiment == "bullish"
        assert score > 0

    def test_analyze_sentiment_bearish(self):
        sentiment, score = NewsMonitor._analyze_sentiment("crypto crash dump plunge hack exploit")
        assert sentiment == "bearish"
        assert score < 0

    def test_analyze_sentiment_neutral(self):
        sentiment, score = NewsMonitor._analyze_sentiment("The weather is nice today")
        assert sentiment == "neutral"
        assert score == 0.0

    def test_correlate_spike_found(self, settings):
        nm = NewsMonitor(settings)
        items = [
            NewsItem(headline="BTC surges", source="coindesk", matched_symbols=["BTC/USDT"]),
            NewsItem(headline="ETH update", source="coindesk", matched_symbols=["ETH/USDT"]),
        ]
        match = nm.correlate_spike("BTC/USDT", items)
        assert match is not None
        assert match.headline == "BTC surges"

    def test_correlate_spike_not_found(self, settings):
        nm = NewsMonitor(settings)
        items = [
            NewsItem(headline="ETH update", source="coindesk", matched_symbols=["ETH/USDT"]),
        ]
        assert nm.correlate_spike("BTC/USDT", items) is None

    @pytest.mark.asyncio
    async def test_start_disabled(self, monkeypatch):
        monkeypatch.setenv("TRADING_MODE", "paper")
        monkeypatch.setenv("EXCHANGE", "mexc")
        monkeypatch.setenv("NEWS_ENABLED", "false")
        from config.settings import Settings
        nm = NewsMonitor(Settings())
        await nm.start()
        assert nm._running is False

    @pytest.mark.asyncio
    async def test_stop(self, settings):
        nm = NewsMonitor(settings)
        nm._running = True
        await nm.stop()
        assert nm._running is False
