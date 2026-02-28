"""Tests for news/monitor.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from news.monitor import NewsItem, NewsMonitor


@pytest.fixture()
def settings(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper_local")
    monkeypatch.setenv("EXCHANGE", "bybit")
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
        monkeypatch.setenv("TRADING_MODE", "paper_local")
        monkeypatch.setenv("EXCHANGE", "bybit")
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
        monkeypatch.setenv("TRADING_MODE", "paper_local")
        monkeypatch.setenv("EXCHANGE", "bybit")
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

    @pytest.mark.asyncio
    async def test_start_enabled_sets_running(self, settings):
        import asyncio

        nm = NewsMonitor(settings)
        with patch.object(nm, "_poll_loop", new_callable=AsyncMock) as mock_loop:
            await nm.start()
            assert nm._running is True
            assert len(nm._background_tasks) == 1
            await asyncio.sleep(0)
            mock_loop.assert_called()
        nm._running = False

    @pytest.mark.asyncio
    async def test_fetch_feed_parses_rss(self, settings):
        nm = NewsMonitor(settings)
        rss_xml = """<?xml version="1.0"?>
        <rss version="2.0"><channel>
        <item><title>Bitcoin surges to new highs</title><link>https://example.com/1</link></item>
        <item><title>Ethereum update released</title><link>https://example.com/2</link></item>
        </channel></rss>"""
        mock_resp = AsyncMock()
        mock_resp.text = AsyncMock(return_value=rss_xml)
        mock_get_cm = MagicMock()
        mock_get_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_get_cm.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_get_cm)

        items = await nm._fetch_feed(mock_session, "test", "https://example.com/rss")
        assert len(items) >= 1
        assert any("BTC/USDT" in i.matched_symbols for i in items)

    @pytest.mark.asyncio
    async def test_fetch_feed_handles_http_error(self, settings):
        nm = NewsMonitor(settings)
        mock_get_cm = MagicMock()
        mock_get_cm.__aenter__ = AsyncMock(side_effect=ConnectionError("down"))
        mock_get_cm.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_get_cm)

        items = await nm._fetch_feed(mock_session, "test", "https://example.com/rss")
        assert items == []

    @pytest.mark.asyncio
    async def test_fetch_all_aggregates_sources(self, settings):
        nm = NewsMonitor(settings)

        async def fake_fetch(session, source, url):
            return [NewsItem(headline=f"From {source}", source=source)]

        with patch.object(nm, "_fetch_feed", side_effect=fake_fetch):
            with patch("news.monitor.aiohttp.ClientSession") as mock_cls:
                mock_sess = MagicMock()
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_sess)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
                items = await nm._fetch_all()
        assert len(items) >= 1

    @pytest.mark.asyncio
    async def test_poll_loop_calls_callbacks(self, settings):
        nm = NewsMonitor(settings)
        nm._running = True
        received = []

        async def cb(item):
            received.append(item)

        nm.on_news(cb)

        async def fake_fetch_all():
            return [NewsItem(headline="Test", source="test", matched_symbols=["BTC/USDT"], sentiment_score=0.5)]

        async def stop_on_sleep(sec):
            nm._running = False

        with patch.object(nm, "_fetch_all", new_callable=AsyncMock, side_effect=fake_fetch_all):
            with patch("news.monitor.asyncio.sleep", side_effect=stop_on_sleep):
                await nm._poll_loop()

        assert len(received) == 1
        assert received[0].headline == "Test"

    @pytest.mark.asyncio
    async def test_poll_loop_handles_callback_error(self, settings):
        nm = NewsMonitor(settings)
        nm._running = True

        async def bad_cb(item):
            raise ValueError("callback error")

        nm.on_news(bad_cb)

        async def fake_fetch():
            return [NewsItem(headline="T", source="t")]

        async def stop_on_sleep(sec):
            nm._running = False

        with patch.object(nm, "_fetch_all", new_callable=AsyncMock, side_effect=fake_fetch):
            with patch("news.monitor.asyncio.sleep", side_effect=stop_on_sleep):
                await nm._poll_loop()
        assert nm._running is False
