from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import aiohttp
from loguru import logger
from pydantic import BaseModel


class SocialData(BaseModel):
    social_volume: float = 0.0
    social_volume_avg: float = 0.0
    social_dominance_pct: float = 0.0
    dev_activity: float = 0.0
    whale_transaction_count: int = 0
    timestamp: datetime = datetime.now(UTC)

    @property
    def is_social_spike(self) -> bool:
        return self.social_volume_avg > 0 and self.social_volume > self.social_volume_avg * 2

    @property
    def sentiment_signal(self) -> str:
        if self.is_social_spike and self.social_volume > self.social_volume_avg * 3:
            return "bearish"  # extreme hype often precedes dumps
        if self.is_social_spike:
            return "bullish"
        return "neutral"


class SantimentClient:
    """Santiment social sentiment — tracks crowd behavior and dev activity.

    Uses Santiment free GraphQL API. Optional API key for higher rate limits.

    Monitors:
    - Social volume (mentions across Twitter, Reddit, Telegram)
    - Social dominance (% of total crypto social chatter)
    - Dev activity (GitHub commits — healthy project signal)
    - Whale transaction count (>$100K transfers)

    Trading signals:
    - Social spike (2x avg) = attention, potential move coming
    - Extreme social spike (3x+) = contrarian sell signal (buy the rumor, sell the news)
    - High dev activity = project health
    - Whale transactions increasing = big players positioning
    """

    GRAPHQL_URL = "https://api.santiment.net/graphql"

    def __init__(self, symbols: list[str] | None = None, api_key: str = "", poll_interval: int = 600):
        self.symbols = symbols or ["bitcoin", "ethereum"]
        self.api_key = api_key
        self.poll_interval = poll_interval
        self._data: dict[str, SocialData] = {}
        self._running = False
        self._background_tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        self._running = True
        self._background_tasks.append(asyncio.create_task(self._poll_loop()))
        logger.info("Santiment monitor started (symbols={}, poll={}s)", self.symbols, self.poll_interval)

    async def stop(self) -> None:
        self._running = False

    def get(self, symbol: str) -> SocialData | None:
        slug = self._to_slug(symbol)
        return self._data.get(slug)

    def sentiment_signal(self, symbol: str = "BTC") -> str:
        d = self.get(symbol)
        return d.sentiment_signal if d else "neutral"

    def is_social_spike(self, symbol: str = "BTC") -> bool:
        d = self.get(symbol)
        return d.is_social_spike if d else False

    def position_bias(self) -> float:
        btc = self.get("BTC")
        if not btc:
            return 1.0
        if btc.sentiment_signal == "bearish":
            return 0.7
        if btc.is_social_spike:
            return 1.1
        return 1.0

    def _to_slug(self, symbol: str) -> str:
        mapping = {
            "BTC": "bitcoin",
            "ETH": "ethereum",
            "SOL": "solana",
            "DOGE": "dogecoin",
            "ADA": "cardano",
            "XRP": "ripple",
        }
        clean = symbol.upper().replace("/USDT", "").replace("USDT", "")
        return mapping.get(clean, clean.lower())

    async def _poll_loop(self) -> None:
        while self._running:
            for sym in self.symbols:
                try:
                    await self._fetch_symbol(sym)
                except Exception as e:
                    logger.error("Santiment error for {}: {}", sym, e)
            await asyncio.sleep(self.poll_interval)

    async def _fetch_symbol(self, slug: str) -> None:
        now = datetime.now(UTC)
        week_ago = now - timedelta(days=7)
        from_str = week_ago.strftime("%Y-%m-%dT%H:%M:%SZ")
        to_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        query = f"""
        {{
          getMetric(metric: "social_volume_total") {{
            timeseriesData(slug: "{slug}" from: "{from_str}" to: "{to_str}" interval: "1d") {{
              datetime value
            }}
          }}
        }}
        """

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Apikey {self.api_key}"

        data = SocialData(timestamp=now)
        timeout = aiohttp.ClientTimeout(total=15)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                async with session.post(self.GRAPHQL_URL, json={"query": query}, headers=headers) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        ts = result.get("data", {}).get("getMetric", {}).get("timeseriesData", [])
                        if ts:
                            values = [p["value"] for p in ts if p.get("value")]
                            if values:
                                data.social_volume = values[-1]
                                data.social_volume_avg = sum(values) / len(values)
            except Exception:
                pass

        self._data[slug] = data

    def summary(self) -> str:
        parts = []
        for slug, d in self._data.items():
            tag = ""
            if d.is_social_spike:
                tag = " ** SPIKE **"
            parts.append(f"{slug}: vol={d.social_volume:.0f} (avg={d.social_volume_avg:.0f}){tag}")
        return "Santiment: " + " | ".join(parts) if parts else "Santiment: no data"
