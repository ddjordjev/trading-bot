from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import aiohttp
from loguru import logger
from pydantic import BaseModel, Field


class TrendingCoin(BaseModel):
    symbol: str
    name: str = ""
    price: float = 0.0
    market_cap: float = 0.0
    volume_24h: float = 0.0
    change_5m: float = 0.0  # Populated when 5m candle/price source available
    change_1h: float = 0.0
    change_24h: float = 0.0
    change_7d: float = 0.0
    change_30d: float = 0.0
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    exchange_pair: str = ""

    @property
    def trading_pair(self) -> str:
        if self.exchange_pair:
            return self.exchange_pair
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
        self._exchange_symbols: set[str] = set()
        self._symbol_alias_map: dict[str, tuple[str, int]] = {}

        self._callbacks: list[Callable[..., Any]] = []
        self._running = False
        self._latest_scan: list[TrendingCoin] = []
        self._hot_movers: list[TrendingCoin] = []
        self._background_tasks: list[asyncio.Task[None]] = []

    def set_exchange_symbols(self, symbols: list[str]) -> None:
        """Set the list of symbols available on the active exchange for filtering.

        Builds a normalized set for exact matching and a fuzzy alias map for
        multiplier-prefix tokens (e.g. 1000LUNC/USDT → base LUNC, mult 1000).
        """
        self._exchange_symbols = set()
        self._symbol_alias_map.clear()

        _prefix_re = re.compile(r"^(\d+)([A-Z]+)(/USDT.*)$")
        for raw in symbols:
            normed = raw.upper().split(":")[0]  # strip :USDT settle suffix
            self._exchange_symbols.add(normed)

            m = _prefix_re.match(normed)
            if m:
                mult, base, suffix = int(m.group(1)), m.group(2), m.group(3)
                plain = f"{base}{suffix}"  # e.g. LUNC/USDT
                self._symbol_alias_map[plain] = (normed, mult)

        logger.info(
            "Scanner: loaded {} exchange symbols, {} multiplier aliases (e.g. {})",
            len(self._exchange_symbols),
            len(self._symbol_alias_map),
            ", ".join(f"{k}->{v[0]}" for k, v in list(self._symbol_alias_map.items())[:3]) or "none",
        )

    def on_trending(self, callback: Callable[..., Any]) -> None:
        """Register callback for when interesting movers are found."""
        self._callbacks.append(callback)

    async def start(self) -> None:
        self._running = True
        self._background_tasks.append(asyncio.create_task(self._scan_loop()))
        logger.info(
            "Trending scanner started (interval={}s, min_vol={:.0f}, min_move={}%/1h, {}%/24h)",
            self.poll_interval,
            self.min_volume_24h,
            self.min_hourly_move_pct,
            self.min_daily_move_pct,
        )

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
                            logger.info(
                                "  {} | 1h: {:+.1f}% | 24h: {:+.1f}% | vol: {:.0f}M | score: {:+.1f}",
                                coin.trading_pair,
                                coin.change_1h,
                                coin.change_24h,
                                coin.volume_24h / 1e6,
                                coin.momentum_score,
                            )

            except Exception as e:
                logger.error("Scanner error: {}", e)

            await asyncio.sleep(self.poll_interval)

    def _merge_external_sources(self) -> list[TrendingCoin]:
        """Pull trending coins from CoinMarketCap and CoinGecko via MarketIntel."""
        if not self._intel:
            return []

        coins: list[TrendingCoin] = []
        try:
            intel = self._intel
            if hasattr(intel, "coinmarketcap"):
                for cmc in intel.coinmarketcap.all_interesting:
                    coins.append(
                        TrendingCoin(
                            symbol=cmc.symbol,
                            name=cmc.name,
                            price=cmc.price,
                            market_cap=cmc.market_cap,
                            volume_24h=cmc.volume_24h,
                            change_1h=cmc.change_1h,
                            change_24h=cmc.change_24h,
                            change_7d=cmc.change_7d,
                        )
                    )
            if hasattr(intel, "coingecko"):
                for gc in intel.coingecko.all_interesting:
                    coins.append(
                        TrendingCoin(
                            symbol=gc.symbol,
                            name=gc.name,
                            price=gc.price,
                            market_cap=gc.market_cap,
                            volume_24h=gc.volume_24h,
                            change_1h=gc.change_1h,
                            change_24h=gc.change_24h,
                            change_7d=gc.change_7d,
                        )
                    )
        except Exception as e:
            logger.debug("External source merge error: {}", e)

        return coins

    async def _fetch_cryptobubbles(self) -> list[TrendingCoin]:
        """Fetch market data from cryptobubbles.net."""
        url = "https://cryptobubbles.net/backend/data/bubbles1000.usd.json"
        coins: list[TrendingCoin] = []

        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp,
            ):
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

                coins.append(
                    TrendingCoin(
                        symbol=symbol,
                        name=item.get("name", ""),
                        price=float(item.get("price", 0) or 0),
                        market_cap=float(item.get("marketcap", 0) or 0),
                        volume_24h=float(item.get("volume", 0) or 0),
                        change_1h=float(perf.get("hour", 0) or 0),
                        change_24h=float(perf.get("day", 0) or 0),
                        change_7d=float(perf.get("week", 0) or 0),
                        change_30d=float(perf.get("month", 0) or 0),
                    )
                )
            except (ValueError, TypeError):
                continue

        return coins

    async def _fallback_fetch(self) -> list[TrendingCoin]:
        """Fallback: scrape the HTML page for basic data."""
        url = "https://cryptobubbles.net"
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp,
            ):
                text = await resp.text()
        except Exception:
            return []

        coins: list[TrendingCoin] = []
        _rows = re.findall(
            r"\|\s*(\d+)\s*\|.*?\$[\d,.]+.*?\|.*?\|.*?\|"
            r"\s*([-+]?[\d.]+)%\s*\|"
            r"\s*([-+]?[\d.]+)%\s*\|"
            r"\s*([-+]?[\d.]+)%\s*\|",
            text,
        )
        for rank_str, h1, h24, d7 in _rows[:20]:
            try:
                coins.append(
                    TrendingCoin(
                        symbol=f"UNKNOWN_{rank_str}",
                        name=f"Rank {rank_str}",
                        change_1h=float(h1),
                        change_24h=float(h24),
                        change_7d=float(d7),
                    )
                )
            except (ValueError, TypeError):
                continue
        return coins

    def _resolve_exchange_symbol(self, coin: TrendingCoin) -> bool:
        """Try to match a coin to an exchange symbol.

        Checks exact match first, then looks for multiplier-prefix aliases
        (e.g. LUNC → 1000LUNC). Validates via price: the mapped price
        (coin.price * multiplier) must land in a reasonable futures range.
        Returns True if the coin is tradeable and sets coin.exchange_pair.
        """
        if not self._exchange_symbols:
            return True

        pair = coin.trading_pair.upper()
        if pair in self._exchange_symbols:
            return True

        alias = getattr(self, "_symbol_alias_map", {}).get(pair)
        if not alias:
            return False

        exchange_sym, multiplier = alias
        mapped_price = coin.price * multiplier
        if mapped_price < 0.0001 or mapped_price > 10_000_000:
            logger.debug(
                "Scanner: {} -> {} price sanity fail (${:.6f} * {} = ${:.4f})",
                pair,
                exchange_sym,
                coin.price,
                multiplier,
                mapped_price,
            )
            return False

        coin.exchange_pair = exchange_sym
        logger.debug("Scanner: mapped {} -> {} (x{}, ~${:.4f})", pair, exchange_sym, multiplier, mapped_price)
        return True

    def _filter_movers(self, coins: list[TrendingCoin]) -> list[TrendingCoin]:
        """Filter for tradeable, liquid coins that are actually moving."""
        stablecoins = {"USDT", "USDC", "DAI", "TUSD", "BUSD", "FDUSD", "PYUSD", "USDP", "USDD", "EURC", "GHO", "RLUSD"}

        filtered = []
        for coin in coins:
            sym = coin.symbol.upper()
            if sym in stablecoins:
                continue
            if coin.volume_24h < self.min_volume_24h:
                continue
            if coin.market_cap < self.min_market_cap:
                continue
            if not self._resolve_exchange_symbol(coin):
                continue

            hourly_hot = abs(coin.change_1h) >= self.min_hourly_move_pct
            daily_hot = abs(coin.change_24h) >= self.min_daily_move_pct

            if hourly_hot or daily_hot:
                filtered.append(coin)

        filtered.sort(key=lambda c: abs(c.momentum_score), reverse=True)
        return filtered[: self.top_movers_count]

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
                f"vol:{coin.volume_24h / 1e6:6.0f}M"
            )
        return "\n".join(lines)
