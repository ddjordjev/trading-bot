from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import aiohttp
import feedparser
from loguru import logger
from pydantic import BaseModel, Field

from config.settings import Settings

# Common crypto symbols to scan for in headlines
_SYMBOL_PATTERNS = [
    (r"\bBTC\b|Bitcoin", "BTC/USDT"),
    (r"\bETH\b|Ethereum", "ETH/USDT"),
    (r"\bSOL\b|Solana", "SOL/USDT"),
    (r"\bXRP\b|Ripple", "XRP/USDT"),
    (r"\bDOGE\b|Dogecoin", "DOGE/USDT"),
    (r"\bADA\b|Cardano", "ADA/USDT"),
    (r"\bAVAX\b|Avalanche", "AVAX/USDT"),
    (r"\bLINK\b|Chainlink", "LINK/USDT"),
    (r"\bDOT\b|Polkadot", "DOT/USDT"),
    (r"\bMATIC\b|Polygon", "MATIC/USDT"),
    (r"\bARB\b|Arbitrum", "ARB/USDT"),
    (r"\bOP\b|Optimism", "OP/USDT"),
]

# Sentiment keywords
_BULLISH = {
    "surge",
    "soar",
    "rally",
    "pump",
    "bull",
    "gain",
    "rise",
    "jump",
    "spike",
    "breakout",
    "adoption",
    "approval",
    "partnership",
    "launch",
    "upgrade",
    "ath",
    "high",
}
_BEARISH = {
    "crash",
    "dump",
    "plunge",
    "drop",
    "bear",
    "loss",
    "fall",
    "decline",
    "hack",
    "exploit",
    "ban",
    "regulation",
    "lawsuit",
    "sec",
    "fraud",
    "liquidat",
}


RSS_FEEDS = {
    "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "cointelegraph": "https://cointelegraph.com/rss",
    "cryptopanic": "https://cryptopanic.com/news/rss/",
}


class NewsItem(BaseModel):
    headline: str
    source: str
    url: str = ""
    published: datetime = Field(default_factory=lambda: datetime.now(UTC))
    matched_symbols: list[str] = []
    sentiment: str = "neutral"  # "bullish", "bearish", "neutral"
    sentiment_score: float = 0.0  # -1.0 to 1.0


class NewsMonitor:
    """Monitors crypto news RSS feeds for market-moving headlines."""

    def __init__(self, settings: Settings):
        self.enabled = settings.news_enabled
        self.sources = settings.news_source_list
        self._seen_urls: set[str] = set()
        self._callbacks: list[Callable[..., Any]] = []
        self._poll_interval = 60
        self._running = False
        self._background_tasks: list[asyncio.Task[None]] = []

    def on_news(self, callback: Callable[..., Any]) -> None:
        self._callbacks.append(callback)

    async def start(self) -> None:
        if not self.enabled:
            logger.info("News monitoring disabled")
            return
        self._running = True
        self._background_tasks.append(asyncio.create_task(self._poll_loop()))
        logger.info("News monitoring started (sources: {})", ", ".join(self.sources))

    async def stop(self) -> None:
        self._running = False

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                items = await self._fetch_all()
                for item in items:
                    for cb in self._callbacks:
                        try:
                            await cb(item)
                        except Exception as e:
                            logger.error("News callback error: {}", e)
            except Exception as e:
                logger.error("News poll error: {}", e)

            await asyncio.sleep(self._poll_interval)

    async def _fetch_all(self) -> list[NewsItem]:
        items: list[NewsItem] = []
        async with aiohttp.ClientSession() as session:
            for source in self.sources:
                url = RSS_FEEDS.get(source)
                if not url:
                    continue
                try:
                    fetched = await self._fetch_feed(session, source, url)
                    items.extend(fetched)
                except Exception as e:
                    logger.warning("Failed to fetch {}: {}", source, e)
        return items

    async def _fetch_feed(self, session: aiohttp.ClientSession, source: str, url: str) -> list[NewsItem]:
        items: list[NewsItem] = []
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                text = await resp.text()
        except Exception as e:
            logger.warning("HTTP error fetching {}: {}", source, e)
            return []

        feed = feedparser.parse(text)

        for entry in feed.entries[:20]:
            link = entry.get("link", "")
            if link in self._seen_urls:
                continue
            self._seen_urls.add(link)

            headline = entry.get("title", "")
            if not headline:
                continue

            symbols = self._extract_symbols(headline)
            sentiment, score = self._analyze_sentiment(headline)

            if symbols or abs(score) > 0.3:
                items.append(
                    NewsItem(
                        headline=headline,
                        source=source,
                        url=link,
                        matched_symbols=symbols,
                        sentiment=sentiment,
                        sentiment_score=score,
                    )
                )

        return items

    @staticmethod
    def _extract_symbols(text: str) -> list[str]:
        symbols = []
        for pattern, symbol in _SYMBOL_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                symbols.append(symbol)
        return symbols

    @staticmethod
    def _analyze_sentiment(text: str) -> tuple[str, float]:
        words = set(text.lower().split())
        bull_count = len(words & _BULLISH)
        bear_count = len(words & _BEARISH)
        total = bull_count + bear_count

        if total == 0:
            return "neutral", 0.0

        score = (bull_count - bear_count) / total
        if score > 0.2:
            return "bullish", score
        if score < -0.2:
            return "bearish", score
        return "neutral", score

    def correlate_spike(self, symbol: str, recent_news: list[NewsItem]) -> NewsItem | None:
        """Check if any recent news item matches a spiking symbol."""
        for item in reversed(recent_news):
            if symbol in item.matched_symbols:
                return item
        return None
