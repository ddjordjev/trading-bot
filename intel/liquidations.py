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

    API_URL_V4 = "https://open-api-v4.coinglass.com/api/futures/liquidation/exchange-list"
    API_URL_V3 = "https://open-api-v3.coinglass.com/api/futures/liquidation/exchange-list"

    def __init__(self, poll_interval: int = 300, api_key: str = ""):
        self.poll_interval = poll_interval
        self.api_key = api_key
        self._latest: LiquidationSnapshot | None = None
        self._running = False
        self._history: list[LiquidationSnapshot] = []
        self._background_tasks: list[asyncio.Task[None]] = []
        self._warned_no_key = False

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
        if not self.api_key:
            if not self._warned_no_key:
                logger.warning(
                    "No CoinGlass API key set (COINGLASS_API_KEY). "
                    "Liquidation data unavailable. Get a free key at https://www.coinglass.com/pricing"
                )
                self._warned_no_key = True
            return

        snap = LiquidationSnapshot(timestamp=datetime.now(UTC))
        headers = {"CG-API-KEY": self.api_key}

        try:
            async with aiohttp.ClientSession() as session:
                for url in (self.API_URL_V4, self.API_URL_V3):
                    try:
                        async with session.get(
                            url,
                            headers=headers,
                            params={"symbol": "", "range": "24h"},
                            timeout=aiohttp.ClientTimeout(total=15),
                        ) as resp:
                            if resp.status != 200:
                                continue
                            data = await resp.json()
                            if isinstance(data, dict) and data.get("data"):
                                break
                    except Exception:
                        continue
                else:
                    logger.warning("CoinGlass: all endpoints returned empty or errored")
                    return
        except Exception as e:
            logger.warning("CoinGlass fetch failed: {}", e)
            return

        def _num(v: object) -> float:
            if v is None:
                return 0.0
            if isinstance(v, (int, float)):
                return float(v)
            if isinstance(v, str):
                cleaned = v.replace(",", "").replace("$", "").strip()
                if not cleaned:
                    return 0.0
                return float(cleaned)
            return 0.0

        def _pick(item: dict, keys: tuple[str, ...]) -> float:
            for k in keys:
                if k in item:
                    return _num(item.get(k))
            return 0.0

        try:
            if not isinstance(data, dict):
                return
            info = data.get("data", [])
            # Some API variants wrap rows in {"list": [...]}.
            if isinstance(info, dict) and isinstance(info.get("list"), list):
                info = info.get("list", [])

            if isinstance(info, list):
                for item in info:
                    if not isinstance(item, dict):
                        continue
                    ex = str(item.get("exchange", item.get("exchangeName", ""))).strip()
                    if ex.lower() == "all":
                        snap.total_24h = _pick(item, ("liquidation_usd", "liquidationUsd", "totalUsd"))
                        snap.long_24h = _pick(item, ("long_liquidation_usd", "longLiquidationUsd", "longUsd"))
                        snap.short_24h = _pick(item, ("short_liquidation_usd", "shortLiquidationUsd", "shortUsd"))
                        break
                else:
                    for item in info:
                        if not isinstance(item, dict):
                            continue
                        snap.total_24h += _pick(item, ("liquidation_usd", "liquidationUsd", "totalUsd"))
                        snap.long_24h += _pick(item, ("long_liquidation_usd", "longLiquidationUsd", "longUsd"))
                        snap.short_24h += _pick(item, ("short_liquidation_usd", "shortLiquidationUsd", "shortUsd"))
            elif isinstance(info, dict):
                snap.total_24h = _pick(info, ("liquidation_usd", "liquidationUsd", "totalUsd"))
                snap.long_24h = _pick(info, ("long_liquidation_usd", "longLiquidationUsd", "longUsd"))
                snap.short_24h = _pick(info, ("short_liquidation_usd", "shortLiquidationUsd", "shortUsd"))
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
