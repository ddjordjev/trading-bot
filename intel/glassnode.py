from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from loguru import logger
from pydantic import BaseModel


class OnChainData(BaseModel):
    exchange_netflow_btc: float = 0.0  # positive = inflows (selling pressure)
    nupl: float = 0.5                   # Net Unrealized Profit/Loss
    sopr: float = 1.0                   # Spent Output Profit Ratio
    active_addresses_24h: int = 0
    timestamp: datetime = datetime.now(timezone.utc)

    @property
    def is_distribution(self) -> bool:
        """Smart money selling: coins flowing TO exchanges + high NUPL."""
        return self.exchange_netflow_btc > 100 and self.nupl > 0.6

    @property
    def is_accumulation(self) -> bool:
        """Smart money buying: coins flowing FROM exchanges + low NUPL."""
        return self.exchange_netflow_btc < -100 and self.nupl < 0.3

    @property
    def on_chain_bias(self) -> str:
        if self.is_accumulation:
            return "bullish"
        if self.is_distribution:
            return "bearish"
        if self.nupl > 0.75:
            return "bearish"  # euphoria zone
        if self.nupl < 0:
            return "bullish"  # capitulation = buy
        return "neutral"


class GlassnodeClient:
    """Glassnode on-chain metrics — tracks smart money behavior.

    Requires API key (free tier available at glassnode.com).

    Monitors:
    - Exchange net flows: inflow - outflow (positive = selling pressure)
    - NUPL: >0.75 = euphoria/sell, <0 = capitulation/buy
    - SOPR: >1 = profit taking, <1 = selling at loss
    - Active addresses: network health

    Trading signals:
    - Distribution phase: exchange inflows + high NUPL = smart money exiting
    - Accumulation phase: exchange outflows + low NUPL = smart money buying
    """

    BASE_URL = "https://api.glassnode.com/v1/metrics"

    def __init__(self, api_key: str = "", poll_interval: int = 900):
        self.api_key = api_key
        self.poll_interval = poll_interval
        self._data: dict[str, OnChainData] = {}
        self._running = False

    async def start(self) -> None:
        if not self.api_key:
            logger.warning("Glassnode: no API key set, on-chain metrics disabled. "
                           "Get a free key at glassnode.com")
            return
        self._running = True
        asyncio.create_task(self._poll_loop())
        logger.info("Glassnode monitor started (poll={}s)", self.poll_interval)

    async def stop(self) -> None:
        self._running = False

    def get(self, symbol: str = "BTC") -> Optional[OnChainData]:
        clean = symbol.upper().replace("/USDT", "").replace("USDT", "")
        return self._data.get(clean)

    def on_chain_bias(self, symbol: str = "BTC") -> str:
        d = self.get(symbol)
        return d.on_chain_bias if d else "neutral"

    def is_distribution_phase(self) -> bool:
        d = self.get("BTC")
        return d.is_distribution if d else False

    def is_accumulation_phase(self) -> bool:
        d = self.get("BTC")
        return d.is_accumulation if d else False

    def position_bias(self) -> float:
        bias = self.on_chain_bias("BTC")
        if bias == "bullish":
            return 1.15
        if bias == "bearish":
            return 0.75
        return 1.0

    async def _poll_loop(self) -> None:
        while self._running:
            for asset in ["BTC", "ETH"]:
                try:
                    await self._fetch_asset(asset)
                except Exception as e:
                    logger.error("Glassnode error for {}: {}", asset, e)
            await asyncio.sleep(self.poll_interval)

    async def _fetch_asset(self, asset: str) -> None:
        params_base = {"a": asset, "api_key": self.api_key}
        data = OnChainData(timestamp=datetime.now(timezone.utc))
        timeout = aiohttp.ClientTimeout(total=15)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            # Exchange net flow
            try:
                async with session.get(
                    f"{self.BASE_URL}/transactions/transfers_volume_exchanges_net",
                    params=params_base,
                ) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        if result and isinstance(result, list):
                            data.exchange_netflow_btc = float(result[-1].get("v", 0))
            except Exception:
                pass

            # NUPL
            try:
                async with session.get(
                    f"{self.BASE_URL}/market/net_unrealized_profit_loss",
                    params=params_base,
                ) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        if result and isinstance(result, list):
                            data.nupl = float(result[-1].get("v", 0.5))
            except Exception:
                pass

            # SOPR
            try:
                async with session.get(
                    f"{self.BASE_URL}/indicators/sopr",
                    params=params_base,
                ) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        if result and isinstance(result, list):
                            data.sopr = float(result[-1].get("v", 1.0))
            except Exception:
                pass

            # Active addresses
            try:
                async with session.get(
                    f"{self.BASE_URL}/addresses/active_count",
                    params=params_base,
                ) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        if result and isinstance(result, list):
                            data.active_addresses_24h = int(result[-1].get("v", 0))
            except Exception:
                pass

        self._data[asset] = data

        if data.is_distribution:
            logger.warning("GLASSNODE {}: distribution phase (netflow={:.0f}, NUPL={:.2f})",
                           asset, data.exchange_netflow_btc, data.nupl)
        elif data.is_accumulation:
            logger.info("GLASSNODE {}: accumulation phase (netflow={:.0f}, NUPL={:.2f})",
                        asset, data.exchange_netflow_btc, data.nupl)

    def summary(self) -> str:
        parts = []
        for sym, d in self._data.items():
            tag = ""
            if d.is_distribution:
                tag = " ** DISTRIBUTION **"
            elif d.is_accumulation:
                tag = " ** ACCUMULATION **"
            parts.append(f"{sym}: NUPL={d.nupl:.2f} SOPR={d.sopr:.3f} "
                         f"netflow={d.exchange_netflow_btc:+.0f}{tag}")
        return "Glassnode: " + " | ".join(parts) if parts else "Glassnode: no data (set GLASSNODE_API_KEY)"
