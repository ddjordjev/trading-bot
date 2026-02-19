from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import aiohttp
from loguru import logger
from pydantic import BaseModel


class LiquidationSnapshot(BaseModel):
    total_24h: float = 0.0  # total liquidations in USD (24h)
    long_24h: float = 0.0
    short_24h: float = 0.0
    total_1h: float = 0.0
    long_1h: float = 0.0
    short_1h: float = 0.0
    timestamp: datetime = datetime.now(UTC)

    @property
    def long_ratio_24h(self) -> float:
        if self.total_24h == 0:
            return 0.5
        return self.long_24h / self.total_24h

    @property
    def is_mass_liquidation(self) -> bool:
        """$1B+ in 24h = mass liquidation event (potential reversal zone)."""
        return self.total_24h >= 1_000_000_000

    @property
    def is_heavy_liquidation(self) -> bool:
        return self.total_24h >= 500_000_000

    @property
    def dominant_side(self) -> str:
        """Which side is getting liquidated more -- that's the exhaustion side."""
        if self.long_ratio_24h > 0.6:
            return "longs"  # longs getting rekt = bottom might be near
        if self.long_ratio_24h < 0.4:
            return "shorts"  # shorts getting rekt = top might be near
        return "balanced"


class LiquidationMonitor:
    """Monitors crypto-wide liquidation data from CoinGlass.

    Trading rules:
    - $1B+ liquidations in 24h: mass capitulation / squeeze. Look for reversal.
    - Longs dominant: potential bottom (everyone who could sell has been liquidated)
    - Shorts dominant: potential top (short squeeze exhausted)
    - 1h spike > $100M: immediate volatility, spike scalp territory

    Source: https://www.coinglass.com/liquidations
    """

    API_URL = "https://open-api.coinglass.com/public/v2/liquidation_info"
    FALLBACK_URL = "https://fapi.coinglass.com/api/futures/liquidation/info"

    def __init__(self, poll_interval: int = 300, api_key: str = ""):
        self.poll_interval = poll_interval
        self.api_key = api_key
        self._latest: LiquidationSnapshot | None = None
        self._running = False
        self._history: list[LiquidationSnapshot] = []
        self._background_tasks: list = []

    async def start(self) -> None:
        self._running = True
        self._background_tasks.append(asyncio.create_task(self._poll_loop()))
        logger.info("Liquidation monitor started (poll={}s)", self.poll_interval)

    async def stop(self) -> None:
        self._running = False

    @property
    def latest(self) -> LiquidationSnapshot | None:
        return self._latest

    def is_reversal_zone(self) -> bool:
        if not self._latest:
            return False
        return self._latest.is_mass_liquidation

    def reversal_bias(self) -> str:
        """If mass liq of longs -> buy bias (bottom). If shorts -> sell bias (top)."""
        if not self._latest or not self._latest.is_heavy_liquidation:
            return "neutral"
        dom = self._latest.dominant_side
        if dom == "longs":
            return "long"  # longs got liquidated = potential bottom
        if dom == "shorts":
            return "short"  # shorts got liquidated = potential top
        return "neutral"

    def aggression_boost(self) -> float:
        """Boost position sizing during mass liquidation events (reversal opportunity)."""
        if not self._latest:
            return 1.0
        if self._latest.is_mass_liquidation:
            return 1.3
        if self._latest.is_heavy_liquidation:
            return 1.1
        return 1.0

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._fetch()
            except Exception as e:
                logger.error("Liquidation fetch error: {}", e)
            await asyncio.sleep(self.poll_interval)

    async def _fetch(self) -> None:
        headers = {}
        if self.api_key:
            headers["coinglassSecret"] = self.api_key

        snap = LiquidationSnapshot(timestamp=datetime.now(UTC))

        try:
            async with aiohttp.ClientSession() as session:
                url = self.API_URL if self.api_key else self.FALLBACK_URL
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        logger.warning("CoinGlass returned {}", resp.status)
                        return
                    data = await resp.json()
        except Exception as e:
            logger.warning("CoinGlass fetch failed: {}", e)
            return

        try:
            info = data.get("data", {})

            if isinstance(info, list):
                for item in info:
                    snap.total_24h += float(item.get("volUsd", 0) or 0)
                    snap.long_24h += float(item.get("longVolUsd", 0) or 0)
                    snap.short_24h += float(item.get("shortVolUsd", 0) or 0)
            elif isinstance(info, dict):
                snap.total_24h = float(info.get("totalVolUsd", 0) or info.get("vol24hUsd", 0) or 0)
                snap.long_24h = float(info.get("longVolUsd", 0) or info.get("longVol24hUsd", 0) or 0)
                snap.short_24h = float(info.get("shortVolUsd", 0) or info.get("shortVol24hUsd", 0) or 0)
        except (ValueError, TypeError, KeyError) as e:
            logger.warning("CoinGlass parse error: {}", e)
            return

        self._latest = snap
        self._history.append(snap)
        if len(self._history) > 288:  # ~24h at 5min intervals
            self._history = self._history[-288:]

        if snap.is_mass_liquidation:
            logger.warning(
                "MASS LIQUIDATION: ${:.0f}B in 24h | longs: {:.0f}% | shorts: {:.0f}%",
                snap.total_24h / 1e9,
                snap.long_ratio_24h * 100,
                (1 - snap.long_ratio_24h) * 100,
            )
        else:
            logger.info(
                "Liquidations 24h: ${:.0f}M | L:{:.0f}% S:{:.0f}% | dom: {}",
                snap.total_24h / 1e6,
                snap.long_ratio_24h * 100,
                (1 - snap.long_ratio_24h) * 100,
                snap.dominant_side,
            )

    def summary(self) -> str:
        if not self._latest:
            return "Liquidations: no data"
        s = self._latest
        tag = " ** MASS LIQ **" if s.is_mass_liquidation else ""
        return (
            f"Liq 24h: ${s.total_24h / 1e6:.0f}M | "
            f"L:{s.long_ratio_24h:.0%} S:{1 - s.long_ratio_24h:.0%} | "
            f"dom: {s.dominant_side}{tag}"
        )
