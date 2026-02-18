from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from loguru import logger
from pydantic import BaseModel


class TVLSnapshot(BaseModel):
    total_tvl: float = 0.0
    tvl_24h_change_pct: float = 0.0
    top_gaining_chains: list[str] = []
    top_losing_chains: list[str] = []
    timestamp: datetime = datetime.now(timezone.utc)


class DeFiLlamaClient:
    """DeFiLlama TVL flows — tracks capital movement across DeFi.

    Free API, no key needed. Monitors:
    - Total DeFi TVL and 24h trend
    - Chain-level flows (which chains are gaining/losing capital)
    - Top protocol movers

    Trading signals:
    - TVL growing across chains = risk-on, capital entering crypto
    - TVL shrinking = risk-off, capital leaving
    - Capital flowing to specific chains = those tokens may pump
    """

    BASE_URL = "https://api.llama.fi"

    def __init__(self, poll_interval: int = 600):
        self.poll_interval = poll_interval
        self._data = TVLSnapshot()
        self._running = False
        self._prev_tvl: float = 0.0

    async def start(self) -> None:
        self._running = True
        asyncio.create_task(self._poll_loop())
        logger.info("DeFiLlama monitor started (poll={}s)", self.poll_interval)

    async def stop(self) -> None:
        self._running = False

    @property
    def snapshot(self) -> TVLSnapshot:
        return self._data

    @property
    def tvl_trend(self) -> str:
        pct = self._data.tvl_24h_change_pct
        if pct > 2.0:
            return "growing"
        if pct < -2.0:
            return "shrinking"
        return "stable"

    @property
    def capital_flowing_to(self) -> list[str]:
        return self._data.top_gaining_chains

    def position_bias(self) -> float:
        """Multiplier based on TVL trend. Growing TVL = more aggressive."""
        trend = self.tvl_trend
        if trend == "growing":
            return 1.1
        if trend == "shrinking":
            return 0.85
        return 1.0

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._fetch()
            except Exception as e:
                logger.error("DeFiLlama error: {}", e)
            await asyncio.sleep(self.poll_interval)

    async def _fetch(self) -> None:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            chains = await self._fetch_chains(session)
            if chains:
                total = sum(c.get("tvl", 0) for c in chains)
                sorted_chains = sorted(chains, key=lambda c: c.get("change_1d", 0) or 0, reverse=True)

                change_pct = 0.0
                if self._prev_tvl > 0:
                    change_pct = ((total - self._prev_tvl) / self._prev_tvl) * 100
                self._prev_tvl = total

                gainers = [c["name"] for c in sorted_chains[:5]
                           if (c.get("change_1d") or 0) > 0]
                losers = [c["name"] for c in sorted_chains[-5:]
                          if (c.get("change_1d") or 0) < 0]

                self._data = TVLSnapshot(
                    total_tvl=total,
                    tvl_24h_change_pct=change_pct,
                    top_gaining_chains=gainers,
                    top_losing_chains=losers,
                    timestamp=datetime.now(timezone.utc),
                )

    async def _fetch_chains(self, session: aiohttp.ClientSession) -> list[dict]:
        try:
            async with session.get(f"{self.BASE_URL}/v2/chains") as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception:
            pass
        return []

    def summary(self) -> str:
        d = self._data
        if d.total_tvl == 0:
            return "DeFiLlama: no data"
        parts = [
            f"TVL: ${d.total_tvl/1e9:.1f}B ({d.tvl_24h_change_pct:+.1f}%)",
            f"trend={self.tvl_trend}",
        ]
        if d.top_gaining_chains:
            parts.append(f"inflows: {','.join(d.top_gaining_chains[:3])}")
        if d.top_losing_chains:
            parts.append(f"outflows: {','.join(d.top_losing_chains[:3])}")
        return "DeFiLlama: " + " | ".join(parts)
