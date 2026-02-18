from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Callable, Optional

import aiohttp
from loguru import logger
from pydantic import BaseModel, Field


class TrendingCoin(BaseModel):
    symbol: str
    name: str = ""
    price: float = 0.0
    market_cap: float = 0.0
    volume_24h: float = 0.0
    change_1h: float = 0.0
    change_24h: float = 0.0
    change_7d: float = 0.0
    change_30d: float = 0.0
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def trading_pair(self) -> str:
        clean = self.symbol.upper().replace(" ", "")
        if clean.endswith("USDT") or clean.endswith("USD"):
            return clean
        return f"{clean}/USDT"

    @property
    def momentum_score(self) -> float:
        return self.change_1h * 3 + self.change_24h * 2 + self.change_7d * 0.5

    @property
    def is_low_liquidity(self) -> bool:
        """Flag coins that are likely too thin for reliable stop execution."""
        return self.volume_24h < 5_000_000 or self.market_cap < 50_000_000

    @property
    def volatility_to_liquidity_ratio(self) -> float:
        """High volatility + low volume = wick-through-your-SL territory.
        Higher ratio = more dangerous."""
        vol = max(self.volume_24h, 1)
        move = max(abs(self.change_1h), abs(self.change_24h) / 4)
        return move / (vol / 1e6)


class TrendingScanner:
    """Scans cryptobubbles.net, CoinMarketCap, and CoinGecko for trending/moving coins.

    Aggregates data from multiple sources, deduplicates, and identifies the
    biggest movers to present as trading opportunities.
    """

    def __init__(
        self,
        poll_interval: int = 60,
        min_volume_24h: float = 5_000_000,
        min_market_cap: float = 50_000_000,
        top_movers_count: int = 10,
        min_hourly_move_pct: float = 2.0,
        min_daily_move_pct: float = 5.0,
        intel: object = None,
    ):
        self.poll_interval = poll_interval
        self.min_volume_24h = min_volume_24h
        self.min_market_cap = min_market_cap
        self.top_movers_count = top_movers_count
        self.min_hourly_move_pct = min_hourly_move_pct
        self.min_daily_move_pct = min_daily_move_pct
        self._intel = intel  # MarketIntel reference for CMC/CoinGecko data

        self._callbacks: list[Callable] = []
        self._running = False
        self._latest_scan: list[TrendingCoin] = []
        self._hot_movers: list[TrendingCoin] = []

    def on_trending(self, callback: Callable) -> None:
        """Register callback for when interesting movers are found."""
        self._callbacks.append(callback)

    async def start(self) -> None:
        self._running = True
        asyncio.create_task(self._scan_loop())
        logger.info("Trending scanner started (interval={}s, min_vol={:.0f}, min_move={}%/1h, {}%/24h)",
                     self.poll_interval, self.min_volume_24h,
                     self.min_hourly_move_pct, self.min_daily_move_pct)

    async def stop(self) -> None:
        self._running = False

    @property
    def hot_movers(self) -> list[TrendingCoin]:
        return list(self._hot_movers)

    @property
    def latest_scan(self) -> list[TrendingCoin]:
        return list(self._latest_scan)

    async def _scan_loop(self) -> None:
        while self._running:
            try:
                coins = await self._fetch_cryptobubbles()
                extra = self._merge_external_sources()
                if extra:
                    seen = {c.symbol.upper() for c in coins}
                    for ec in extra:
                        if ec.symbol.upper() not in seen:
                            coins.append(ec)
                            seen.add(ec.symbol.upper())

                if coins:
                    self._latest_scan = coins
                    movers = self._filter_movers(coins)

                    if movers != self._hot_movers:
                        self._hot_movers = movers
                        for cb in self._callbacks:
                            try:
                                await cb(movers)
                            except Exception as e:
                                logger.error("Scanner callback error: {}", e)

                    if movers:
                        logger.info("Scanner found {} hot movers:", len(movers))
                        for coin in movers[:5]:
                            logger.info("  {} | 1h: {:+.1f}% | 24h: {:+.1f}% | vol: {:.0f}M | score: {:+.1f}",
                                        coin.trading_pair, coin.change_1h, coin.change_24h,
                                        coin.volume_24h / 1e6, coin.momentum_score)

            except Exception as e:
                logger.error("Scanner error: {}", e)

            await asyncio.sleep(self.poll_interval)

    def _merge_external_sources(self) -> list[TrendingCoin]:
        """Pull trending coins from CoinMarketCap and CoinGecko via MarketIntel."""
        if not self._intel:
            return []

        coins: list[TrendingCoin] = []
        try:
            from intel.coinmarketcap import CoinMarketCapClient
            from intel.coingecko import CoinGeckoClient

            intel = self._intel
            if hasattr(intel, "coinmarketcap"):
                for cmc in intel.coinmarketcap.all_interesting:
                    coins.append(TrendingCoin(
                        symbol=cmc.symbol,
                        name=cmc.name,
                        price=cmc.price,
                        market_cap=cmc.market_cap,
                        volume_24h=cmc.volume_24h,
                        change_1h=cmc.change_1h,
                        change_24h=cmc.change_24h,
                        change_7d=cmc.change_7d,
                    ))
            if hasattr(intel, "coingecko"):
                for gc in intel.coingecko.all_interesting:
                    coins.append(TrendingCoin(
                        symbol=gc.symbol,
                        name=gc.name,
                        price=gc.price,
                        market_cap=gc.market_cap,
                        volume_24h=gc.volume_24h,
                        change_1h=gc.change_1h,
                        change_24h=gc.change_24h,
                        change_7d=gc.change_7d,
                    ))
        except Exception as e:
            logger.debug("External source merge error: {}", e)

        return coins

    async def _fetch_cryptobubbles(self) -> list[TrendingCoin]:
        """Fetch market data from cryptobubbles.net."""
        url = "https://cryptobubbles.net/backend/data/bubbles1000.usd.json"
        coins: list[TrendingCoin] = []

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        logger.warning("Cryptobubbles returned status {}", resp.status)
                        return await self._fallback_fetch()
                    data = await resp.json()
        except Exception as e:
            logger.warning("Cryptobubbles fetch failed: {} -- using fallback", e)
            return await self._fallback_fetch()

        for item in data:
            try:
                symbol = item.get("symbol", "")
                if not symbol:
                    continue

                perf = item.get("performance", {})

                coins.append(TrendingCoin(
                    symbol=symbol,
                    name=item.get("name", ""),
                    price=float(item.get("price", 0) or 0),
                    market_cap=float(item.get("marketcap", 0) or 0),
                    volume_24h=float(item.get("volume", 0) or 0),
                    change_1h=float(perf.get("hour", 0) or 0),
                    change_24h=float(perf.get("day", 0) or 0),
                    change_7d=float(perf.get("week", 0) or 0),
                    change_30d=float(perf.get("month", 0) or 0),
                ))
            except (ValueError, TypeError):
                continue

        return coins

    async def _fallback_fetch(self) -> list[TrendingCoin]:
        """Fallback: scrape the HTML page for basic data."""
        url = "https://cryptobubbles.net"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    text = await resp.text()
        except Exception:
            return []

        coins: list[TrendingCoin] = []
        rows = re.findall(
            r'\|\s*(\d+)\s*\|.*?\$[\d,.]+.*?\|.*?\|.*?\|'
            r'\s*([-+]?[\d.]+)%\s*\|'
            r'\s*([-+]?[\d.]+)%\s*\|'
            r'\s*([-+]?[\d.]+)%\s*\|',
            text,
        )
        return coins

    def _filter_movers(self, coins: list[TrendingCoin]) -> list[TrendingCoin]:
        """Filter for tradeable, liquid coins that are actually moving."""
        stablecoins = {"USDT", "USDC", "DAI", "TUSD", "BUSD", "FDUSD", "PYUSD", "USDP",
                       "USDD", "EURC", "GHO", "RLUSD"}

        filtered = []
        for coin in coins:
            sym = coin.symbol.upper()
            if sym in stablecoins:
                continue
            if coin.volume_24h < self.min_volume_24h:
                continue
            if coin.market_cap < self.min_market_cap:
                continue

            hourly_hot = abs(coin.change_1h) >= self.min_hourly_move_pct
            daily_hot = abs(coin.change_24h) >= self.min_daily_move_pct

            if hourly_hot or daily_hot:
                filtered.append(coin)

        filtered.sort(key=lambda c: abs(c.momentum_score), reverse=True)
        return filtered[:self.top_movers_count]

    def get_strongest_bullish(self, n: int = 3) -> list[TrendingCoin]:
        """Top N strongest upward movers."""
        return sorted(
            [c for c in self._hot_movers if c.momentum_score > 0],
            key=lambda c: c.momentum_score,
            reverse=True,
        )[:n]

    def get_strongest_bearish(self, n: int = 3) -> list[TrendingCoin]:
        """Top N strongest downward movers (for shorting opportunities)."""
        return sorted(
            [c for c in self._hot_movers if c.momentum_score < 0],
            key=lambda c: c.momentum_score,
        )[:n]

    def scan_summary(self) -> str:
        if not self._hot_movers:
            return "Scanner: No hot movers found"

        sources = ["CryptoBubbles"]
        if self._intel:
            if hasattr(self._intel, "coinmarketcap") and self._intel.coinmarketcap.trending:
                sources.append("CMC")
            if hasattr(self._intel, "coingecko") and self._intel.coingecko.trending:
                sources.append("CoinGecko")

        lines = [f"Scanner: {len(self._hot_movers)} hot movers (sources: {', '.join(sources)})"]
        for coin in self._hot_movers[:5]:
            direction = "UP" if coin.momentum_score > 0 else "DN"
            lines.append(
                f"  {direction} {coin.trading_pair:>12} | "
                f"1h:{coin.change_1h:+5.1f}% | "
                f"24h:{coin.change_24h:+6.1f}% | "
                f"vol:{coin.volume_24h/1e6:6.0f}M"
            )
        return "\n".join(lines)
