from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from loguru import logger
from pydantic import BaseModel


class FearGreedReading(BaseModel):
    value: int                 # 0-100
    classification: str        # "Extreme Fear", "Fear", "Neutral", "Greed", "Extreme Greed"
    timestamp: datetime
    previous_value: int = 0
    previous_classification: str = ""


class FearGreedClient:
    """Fetches the Crypto Fear & Greed Index from alternative.me.

    Trading rules:
    - Extreme Fear (0-25): BUY bias -- everyone is panicking, blood in the streets
    - Fear (25-40): slight BUY bias -- cautious accumulation zone
    - Neutral (40-60): no bias -- trade normally
    - Greed (60-75): slight SELL bias -- start tightening stops
    - Extreme Greed (75-100): SELL bias -- market is overheated, protect profits
    """

    API_URL = "https://api.alternative.me/fng/?limit=2"

    def __init__(self, poll_interval: int = 3600):
        self.poll_interval = poll_interval
        self._latest: Optional[FearGreedReading] = None
        self._running = False

    async def start(self) -> None:
        self._running = True
        asyncio.create_task(self._poll_loop())
        logger.info("Fear & Greed monitor started (poll={}s)", self.poll_interval)

    async def stop(self) -> None:
        self._running = False

    @property
    def latest(self) -> Optional[FearGreedReading]:
        return self._latest

    @property
    def value(self) -> int:
        return self._latest.value if self._latest else 50

    @property
    def is_extreme_fear(self) -> bool:
        return self.value <= 25

    @property
    def is_fear(self) -> bool:
        return self.value <= 40

    @property
    def is_greed(self) -> bool:
        return self.value >= 60

    @property
    def is_extreme_greed(self) -> bool:
        return self.value >= 75

    def position_bias(self) -> float:
        """Returns a multiplier for position sizing.
        < 1.0 = reduce size (greed), > 1.0 = increase size (fear/opportunity)
        """
        v = self.value
        if v <= 10:
            return 1.4    # extreme fear = big opportunity
        if v <= 25:
            return 1.2    # fear = good buying
        if v <= 40:
            return 1.1
        if v <= 60:
            return 1.0    # neutral
        if v <= 75:
            return 0.8    # greed = tighten up
        return 0.6        # extreme greed = protect capital

    def trade_direction_bias(self) -> str:
        """Suggests preferred trade direction based on sentiment."""
        if self.value <= 25:
            return "long"
        if self.value >= 75:
            return "short"
        return "neutral"

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._fetch()
            except Exception as e:
                logger.error("Fear & Greed fetch error: {}", e)
            await asyncio.sleep(self.poll_interval)

    async def _fetch(self) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.get(self.API_URL, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    logger.warning("Fear & Greed API returned {}", resp.status)
                    return
                data = await resp.json()

        entries = data.get("data", [])
        if not entries:
            return

        current = entries[0]
        previous = entries[1] if len(entries) > 1 else {}

        self._latest = FearGreedReading(
            value=int(current.get("value", 50)),
            classification=current.get("value_classification", "Neutral"),
            timestamp=datetime.fromtimestamp(
                int(current.get("timestamp", 0)), tz=timezone.utc
            ),
            previous_value=int(previous.get("value", 0)) if previous else 0,
            previous_classification=previous.get("value_classification", "") if previous else "",
        )

        logger.info("Fear & Greed: {} ({}) | prev: {} ({})",
                     self._latest.value, self._latest.classification,
                     self._latest.previous_value, self._latest.previous_classification)

    def summary(self) -> str:
        if not self._latest:
            return "Fear & Greed: no data"
        r = self._latest
        direction = self.trade_direction_bias()
        return (f"Fear & Greed: {r.value} ({r.classification}) | "
                f"bias: {direction} | size_mult: {self.position_bias():.1f}x")
