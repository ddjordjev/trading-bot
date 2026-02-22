from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import aiohttp
from loguru import logger
from pydantic import BaseModel, Field


def _pct_24h(raw: Any) -> float:
    """Parse 24h change from API: may be a number or a dict with 'usd' key."""
    if raw is None:
        return 0.0
    if isinstance(raw, dict):
        return float(raw.get("usd", 0) or 0)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


class GeckoCoin(BaseModel):
    """Coin data from CoinGecko."""

    id: str = ""
    symbol: str
    name: str = ""
    price: float = 0.0
    market_cap: float = 0.0
    market_cap_rank: int = 0
    volume_24h: float = 0.0
    change_1h: float = 0.0
    change_24h: float = 0.0
    change_7d: float = 0.0
    ath: float = 0.0
    ath_change_pct: float = 0.0
    sparkline_7d: list[float] = []
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def trading_pair(self) -> str:
        return f"{self.symbol.upper()}/USDT"

    @property
    def is_near_ath(self) -> bool:
        return self.ath_change_pct > -5.0

    @property
    def is_heavily_discounted(self) -> bool:
        return self.ath_change_pct < -80.0

    @property
    def recent_trend(self) -> str:
        """Analyze sparkline for trend direction."""
        if len(self.sparkline_7d) < 10:
            return "unknown"
        recent = self.sparkline_7d[-24:]  # ~last day
        older = self.sparkline_7d[-48:-24] if len(self.sparkline_7d) >= 48 else self.sparkline_7d[:24]
        avg_recent = sum(recent) / len(recent) if recent else 0
        avg_older = sum(older) / len(older) if older else 0
        if avg_older == 0:
            return "unknown"
        change = (avg_recent - avg_older) / avg_older * 100
        if change > 3:
            return "up"
        if change < -3:
            return "down"
        return "flat"


class CoinGeckoClient:
    """Fetches trending coins, market data, and price changes from CoinGecko.

    Free API -- no key needed, but rate-limited (10-50 calls/min depending on IP).
    We keep polls infrequent and cache aggressively.
    """

    BASE_URL = "https://api.coingecko.com/api/v3"
    PRO_URL = "https://pro-api.coingecko.com/api/v3"

    def __init__(self, api_key: str = "", poll_interval: int = 300):
        self.api_key = api_key
        self.poll_interval = poll_interval
        self._trending: list[GeckoCoin] = []
        self._top_by_volume: list[GeckoCoin] = []
        self._top_gainers: list[GeckoCoin] = []
        self._running = False
        self._background_tasks: list[asyncio.Task[None]] = []

    @property
    def _base_url(self) -> str:
        return self.PRO_URL if self.api_key else self.BASE_URL

    def _params(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        p = {}
        if self.api_key:
            p["x_cg_pro_api_key"] = self.api_key
        if extra:
            p.update(extra)
        return p

    async def start(self) -> None:
        self._running = True
        self._background_tasks.append(asyncio.create_task(self._poll_loop()))
        mode = "Pro API" if self.api_key else "free API"
        logger.info("CoinGecko client started (mode={}, poll={}s)", mode, self.poll_interval)

    async def stop(self) -> None:
        self._running = False

    @property
    def trending(self) -> list[GeckoCoin]:
        return list(self._trending)

    @property
    def top_volume(self) -> list[GeckoCoin]:
        return list(self._top_by_volume)

    @property
    def top_gainers(self) -> list[GeckoCoin]:
        return list(self._top_gainers)

    @property
    def all_interesting(self) -> list[GeckoCoin]:
        seen: set[str] = set()
        result: list[GeckoCoin] = []
        for coin in self._trending + self._top_gainers + self._top_by_volume:
            if coin.symbol not in seen:
                seen.add(coin.symbol)
                result.append(coin)
        return result

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._fetch_all()
            except Exception as e:
                logger.error("CoinGecko fetch error: {}", e)
            await asyncio.sleep(self.poll_interval)

    async def _fetch_all(self) -> None:
        await self._fetch_trending()
        await asyncio.sleep(2)  # rate limit buffer
        await self._fetch_market_data()

    async def _fetch_trending(self) -> None:
        """Fetch trending search coins from CoinGecko."""
        url = f"{self._base_url}/search/trending"
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(
                    url,
                    params=self._params(),
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp,
            ):
                if resp.status != 200:
                    logger.debug("CoinGecko trending returned {}", resp.status)
                    return
                data = await resp.json()
        except Exception as e:
            logger.warning("CoinGecko trending error: {}", e)
            return

        if not isinstance(data, dict):
            return
        coins_raw = data.get("coins", [])
        if not isinstance(coins_raw, list):
            logger.debug("CoinGecko trending returned non-list coins: {}", type(coins_raw).__name__)
            return

        coins: list[GeckoCoin] = []
        for item in coins_raw:
            if not isinstance(item, dict):
                continue
            coin_data = item.get("item", {})
            if not isinstance(coin_data, dict):
                coin_data = {}
            data_inner = coin_data.get("data")
            data_inner = data_inner if isinstance(data_inner, dict) else {}
            try:
                coins.append(
                    GeckoCoin(
                        id=coin_data.get("id", ""),
                        symbol=coin_data.get("symbol", ""),
                        name=coin_data.get("name", ""),
                        price=float(data_inner.get("price", 0) or 0),
                        market_cap=float(data_inner.get("market_cap", "0").replace(",", "").replace("$", "") or 0)
                        if isinstance(data_inner.get("market_cap"), str)
                        else float(data_inner.get("market_cap", 0) or 0),
                        change_24h=_pct_24h(data_inner.get("price_change_percentage_24h")),
                        volume_24h=float(data_inner.get("total_volume", "0").replace(",", "").replace("$", "") or 0)
                        if isinstance(data_inner.get("total_volume"), str)
                        else float(data_inner.get("total_volume", 0) or 0),
                        market_cap_rank=coin_data.get("market_cap_rank", 0) or 0,
                        sparkline_7d=data_inner.get("sparkline", [])
                        if isinstance(data_inner.get("sparkline"), list)
                        else [],
                    )
                )
            except (ValueError, TypeError, KeyError):
                continue

        self._trending = coins
        if coins:
            logger.debug("CoinGecko trending: {} coins (top: {})", len(coins), ", ".join(c.symbol for c in coins[:5]))

    async def _fetch_market_data(self) -> None:
        """Fetch top coins by market cap with full data."""
        url = f"{self._base_url}/coins/markets"
        params = self._params(
            {
                "vs_currency": "usd",
                "order": "volume_desc",
                "per_page": "100",
                "page": "1",
                "sparkline": "true",
                "price_change_percentage": "1h,24h,7d",
            }
        )

        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(
                    url,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp,
            ):
                if resp.status != 200:
                    logger.debug("CoinGecko markets returned {}", resp.status)
                    return
                data = await resp.json()
        except Exception as e:
            logger.warning("CoinGecko markets error: {}", e)
            return

        if not isinstance(data, list):
            logger.debug("CoinGecko markets returned non-list: {}", type(data).__name__)
            return

        stablecoins = {"usdt", "usdc", "dai", "tusd", "busd", "fdusd", "pyusd", "usdp", "usdd"}

        coins: list[GeckoCoin] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            try:
                sym = item.get("symbol", "").lower()
                if sym in stablecoins:
                    continue

                si7 = item.get("sparkline_in_7d")
                if isinstance(si7, list):
                    sparkline = si7
                elif isinstance(si7, dict):
                    sparkline = si7.get("price", []) or []
                else:
                    sparkline = []

                coins.append(
                    GeckoCoin(
                        id=item.get("id", ""),
                        symbol=item.get("symbol", ""),
                        name=item.get("name", ""),
                        price=float(item.get("current_price", 0) or 0),
                        market_cap=float(item.get("market_cap", 0) or 0),
                        market_cap_rank=item.get("market_cap_rank", 0) or 0,
                        volume_24h=float(item.get("total_volume", 0) or 0),
                        change_1h=float(item.get("price_change_percentage_1h_in_currency", 0) or 0),
                        change_24h=float(item.get("price_change_percentage_24h", 0) or 0),
                        change_7d=float(item.get("price_change_percentage_7d_in_currency", 0) or 0),
                        ath=float(item.get("ath", 0) or 0),
                        ath_change_pct=float(item.get("ath_change_percentage", 0) or 0),
                        sparkline_7d=sparkline[-168:] if sparkline else [],
                    )
                )
            except (ValueError, TypeError):
                continue

        self._top_by_volume = coins[:50]

        self._top_gainers = sorted(
            [c for c in coins if abs(c.change_24h) >= 5],
            key=lambda c: abs(c.change_24h),
            reverse=True,
        )[:20]

        if self._top_gainers:
            logger.debug(
                "CoinGecko: {} movers >5% (top: {})",
                len(self._top_gainers),
                ", ".join(f"{c.symbol}({c.change_24h:+.1f}%)" for c in self._top_gainers[:5]),
            )

    def find_by_symbol(self, symbol: str) -> GeckoCoin | None:
        sym = symbol.lower().replace("/usdt", "")
        for coin in self._top_by_volume + self._trending:
            if coin.symbol.lower() == sym:
                return coin
        return None

    def summary(self) -> str:
        parts = []
        if self._trending:
            top = ", ".join(c.symbol.upper() for c in self._trending[:5])
            parts.append(f"trending: {top}")
        if self._top_gainers:
            top = ", ".join(f"{c.symbol.upper()}({c.change_24h:+.1f}%)" for c in self._top_gainers[:3])
            parts.append(f"movers: {top}")
        if self._top_by_volume:
            parts.append(f"tracked: {len(self._top_by_volume)} coins by volume")
        if not parts:
            return "CoinGecko: no data"
        return "CoinGecko: " + " | ".join(parts)
