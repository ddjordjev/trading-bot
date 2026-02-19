from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import aiohttp
from loguru import logger
from pydantic import BaseModel, Field


class CMCCoin(BaseModel):
    """Coin data from CoinMarketCap."""

    id: int = 0
    symbol: str
    name: str = ""
    slug: str = ""
    price: float = 0.0
    market_cap: float = 0.0
    volume_24h: float = 0.0
    change_1h: float = 0.0
    change_24h: float = 0.0
    change_7d: float = 0.0
    cmc_rank: int = 0
    circulating_supply: float = 0.0
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def trading_pair(self) -> str:
        return f"{self.symbol.upper()}/USDT"

    @property
    def is_tradable_size(self) -> bool:
        return self.volume_24h >= 1_000_000 and self.market_cap >= 10_000_000


class CoinMarketCapClient:
    """Fetches trending, top gainers/losers, and new listings from CoinMarketCap.

    Two modes:
    - With API key: uses the official CMC API (more reliable, higher rate limits)
    - Without API key: uses the public web endpoints (limited but free)

    This feeds the scanner with coins worth investigating for trades.
    """

    API_BASE = "https://pro-api.coinmarketcap.com/v1"
    WEB_API = "https://api.coinmarketcap.com/data-api/v3"

    def __init__(self, api_key: str = "", poll_interval: int = 300):
        self.api_key = api_key
        self.poll_interval = poll_interval
        self._trending: list[CMCCoin] = []
        self._gainers: list[CMCCoin] = []
        self._losers: list[CMCCoin] = []
        self._recently_added: list[CMCCoin] = []
        self._running = False
        self._background_tasks: list = []

    async def start(self) -> None:
        self._running = True
        self._background_tasks.append(asyncio.create_task(self._poll_loop()))
        mode = "API key" if self.api_key else "public endpoints"
        logger.info("CoinMarketCap client started (mode={}, poll={}s)", mode, self.poll_interval)

    async def stop(self) -> None:
        self._running = False

    @property
    def trending(self) -> list[CMCCoin]:
        return list(self._trending)

    @property
    def gainers(self) -> list[CMCCoin]:
        return list(self._gainers)

    @property
    def losers(self) -> list[CMCCoin]:
        return list(self._losers)

    @property
    def recently_added(self) -> list[CMCCoin]:
        return list(self._recently_added)

    @property
    def all_interesting(self) -> list[CMCCoin]:
        """Union of trending, gainers, and recently added -- deduplicated."""
        seen: set[str] = set()
        result: list[CMCCoin] = []
        for coin in self._trending + self._gainers + self._recently_added:
            if coin.symbol not in seen:
                seen.add(coin.symbol)
                result.append(coin)
        return result

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._fetch_all()
            except Exception as e:
                logger.error("CMC fetch error: {}", e)
            await asyncio.sleep(self.poll_interval)

    async def _fetch_all(self) -> None:
        await asyncio.gather(
            self._fetch_trending(),
            self._fetch_gainers_losers(),
            self._fetch_recently_added(),
            return_exceptions=True,
        )

    def _headers(self) -> dict[str, str]:
        if self.api_key:
            return {"X-CMC_PRO_API_KEY": self.api_key, "Accept": "application/json"}
        return {"Accept": "application/json"}

    async def _fetch_trending(self) -> None:
        """Fetch trending coins from CMC."""
        url = f"{self.WEB_API}/topsearch/rank"
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(
                    url,
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp,
            ):
                if resp.status != 200:
                    logger.debug("CMC trending returned {}", resp.status)
                    return
                data = await resp.json()
        except Exception as e:
            logger.debug("CMC trending error: {}", e)
            return

        coins: list[CMCCoin] = []
        for item in data.get("data", {}).get("cryptoTopSearchRanks", [])[:30]:
            try:
                coins.append(
                    CMCCoin(
                        id=item.get("id", 0),
                        symbol=item.get("symbol", ""),
                        name=item.get("name", ""),
                        slug=item.get("slug", ""),
                        price=float(item.get("priceChange", {}).get("price", 0) or 0),
                        change_24h=float(item.get("priceChange", {}).get("priceChange24h", 0) or 0),
                        volume_24h=float(item.get("priceChange", {}).get("volume24h", 0) or 0),
                        market_cap=float(item.get("priceChange", {}).get("marketCap", 0) or 0),
                    )
                )
            except (ValueError, TypeError, KeyError):
                continue

        self._trending = coins
        if coins:
            logger.debug("CMC trending: {} coins (top: {})", len(coins), ", ".join(c.symbol for c in coins[:5]))

    async def _fetch_gainers_losers(self) -> None:
        """Fetch top gainers and losers from CMC."""
        if self.api_key:
            await self._fetch_gainers_api()
            return

        url = f"{self.WEB_API}/cryptocurrency/spotlight"
        params = {"dataType": "2", "limit": "30"}
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(
                    url,
                    params=params,
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp,
            ):
                if resp.status != 200:
                    return
                data = await resp.json()
        except Exception:
            return

        gainers: list[CMCCoin] = []
        losers: list[CMCCoin] = []

        for item in data.get("data", {}).get("gainerList", []):
            coin = self._parse_spotlight_coin(item)
            if coin:
                gainers.append(coin)
        for item in data.get("data", {}).get("loserList", []):
            coin = self._parse_spotlight_coin(item)
            if coin:
                losers.append(coin)

        self._gainers = gainers
        self._losers = losers

    async def _fetch_gainers_api(self) -> None:
        """Fetch gainers using the official API with key."""
        url = f"{self.API_BASE}/cryptocurrency/trending/gainers-losers"
        params = {"limit": "20", "time_period": "24h", "convert": "USD"}
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(
                    url,
                    params=params,
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp,
            ):
                if resp.status != 200:
                    return
                data = await resp.json()
        except Exception:
            return

        gainers: list[CMCCoin] = []
        losers: list[CMCCoin] = []
        for item in data.get("data", []):
            coin = self._parse_api_coin(item)
            if coin:
                if coin.change_24h >= 0:
                    gainers.append(coin)
                else:
                    losers.append(coin)

        self._gainers = gainers
        self._losers = losers

    async def _fetch_recently_added(self) -> None:
        """Fetch recently listed coins."""
        url = f"{self.WEB_API}/cryptocurrency/listing"
        params = {"start": "1", "limit": "20", "sortBy": "date_added", "sortType": "desc"}
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(
                    url,
                    params=params,
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp,
            ):
                if resp.status != 200:
                    return
                data = await resp.json()
        except Exception:
            return

        coins: list[CMCCoin] = []
        for item in data.get("data", {}).get("cryptoCurrencyList", []):
            try:
                quotes = item.get("quotes", [{}])
                q = quotes[0] if quotes else {}
                coins.append(
                    CMCCoin(
                        id=item.get("id", 0),
                        symbol=item.get("symbol", ""),
                        name=item.get("name", ""),
                        slug=item.get("slug", ""),
                        price=float(q.get("price", 0) or 0),
                        volume_24h=float(q.get("volume24h", 0) or 0),
                        market_cap=float(q.get("marketCap", 0) or 0),
                        change_24h=float(q.get("percentChange24h", 0) or 0),
                        change_1h=float(q.get("percentChange1h", 0) or 0),
                        change_7d=float(q.get("percentChange7d", 0) or 0),
                        cmc_rank=item.get("cmcRank", 0),
                    )
                )
            except (ValueError, TypeError, KeyError):
                continue

        self._recently_added = coins

    @staticmethod
    def _parse_spotlight_coin(item: dict) -> CMCCoin | None:
        try:
            return CMCCoin(
                id=item.get("id", 0),
                symbol=item.get("symbol", ""),
                name=item.get("name", ""),
                slug=item.get("slug", ""),
                price=float(item.get("priceChange", {}).get("price", 0) or 0),
                change_24h=float(item.get("priceChange", {}).get("priceChange24h", 0) or 0),
                volume_24h=float(item.get("priceChange", {}).get("volume24h", 0) or 0),
                market_cap=float(item.get("priceChange", {}).get("marketCap", 0) or 0),
                cmc_rank=item.get("cmcRank", 0),
            )
        except (ValueError, TypeError, KeyError):
            return None

    @staticmethod
    def _parse_api_coin(item: dict) -> CMCCoin | None:
        try:
            quote = item.get("quote", {}).get("USD", {})
            return CMCCoin(
                id=item.get("id", 0),
                symbol=item.get("symbol", ""),
                name=item.get("name", ""),
                slug=item.get("slug", ""),
                price=float(quote.get("price", 0) or 0),
                volume_24h=float(quote.get("volume_24h", 0) or 0),
                market_cap=float(quote.get("market_cap", 0) or 0),
                change_1h=float(quote.get("percent_change_1h", 0) or 0),
                change_24h=float(quote.get("percent_change_24h", 0) or 0),
                change_7d=float(quote.get("percent_change_7d", 0) or 0),
                cmc_rank=item.get("cmc_rank", 0),
            )
        except (ValueError, TypeError, KeyError):
            return None

    def summary(self) -> str:
        parts = []
        if self._trending:
            top = ", ".join(c.symbol for c in self._trending[:5])
            parts.append(f"trending: {top}")
        if self._gainers:
            top = ", ".join(f"{c.symbol}({c.change_24h:+.1f}%)" for c in self._gainers[:3])
            parts.append(f"gainers: {top}")
        if self._losers:
            top = ", ".join(f"{c.symbol}({c.change_24h:+.1f}%)" for c in self._losers[:3])
            parts.append(f"losers: {top}")
        if self._recently_added:
            parts.append(f"new: {len(self._recently_added)} coins")
        if not parts:
            return "CMC: no data"
        return "CMC: " + " | ".join(parts)
