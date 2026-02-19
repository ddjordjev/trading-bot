from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import aiohttp
from loguru import logger
from pydantic import BaseModel


class OISnapshot(BaseModel):
    """Historical open interest data point."""

    total_oi_usd: float = 0.0
    oi_change_1h_pct: float = 0.0
    oi_change_24h_pct: float = 0.0
    oi_weighted_funding: float = 0.0  # OI-weighted avg funding across exchanges
    top_trader_long_ratio: float = 0.5  # Binance top trader L/S
    timestamp: datetime = datetime.now(UTC)

    @property
    def oi_surging(self) -> bool:
        return self.oi_change_1h_pct > 3.0

    @property
    def oi_collapsing(self) -> bool:
        return self.oi_change_1h_pct < -5.0


class WhaleSentimentData(BaseModel):
    long_short_ratio: float = 1.0  # >1 = more longs, <1 = more shorts
    open_interest_24h_change_pct: float = 0.0
    funding_rate: float = 0.0  # positive = longs pay shorts
    oi_snapshot: OISnapshot | None = None
    timestamp: datetime = datetime.now(UTC)

    @property
    def is_overleveraged_longs(self) -> bool:
        """Extreme positive funding + high L/S ratio = too many longs. Crash incoming?"""
        return self.funding_rate > 0.05 and self.long_short_ratio > 1.5

    @property
    def is_overleveraged_shorts(self) -> bool:
        return self.funding_rate < -0.05 and self.long_short_ratio < 0.7

    @property
    def oi_building(self) -> bool:
        """OI increasing significantly = new money entering, directional move coming."""
        return self.open_interest_24h_change_pct > 5.0

    @property
    def oi_declining(self) -> bool:
        """OI declining = positions closing, momentum fading."""
        return self.open_interest_24h_change_pct < -5.0


class WhaleSentiment:
    """Aggregates whale/smart money signals from CoinGlass and WhaleTrades.

    Trading rules:
    - Extreme positive funding rate (>0.05%): longs are overleveraged.
      Contrarian SHORT bias -- or at minimum, don't open new longs.
    - Extreme negative funding (<-0.05%): shorts overleveraged.
      Contrarian LONG bias.
    - Long/Short ratio extreme (>1.5): too many longs. Careful.
    - Long/Short ratio extreme (<0.7): too many shorts. Buy opportunity.
    - OI building + price flat: breakout incoming (unknown direction).
    - OI declining: momentum exhausting, tighten stops.

    Sources:
    - https://whaletrades.io/ (dashboard summary)
    - https://www.coinglass.com/ (funding rates, OI, L/S ratios)
    """

    COINGLASS_LS_URL = "https://fapi.coinglass.com/api/futures/longShortRate"
    COINGLASS_FUNDING_URL = "https://fapi.coinglass.com/api/futures/funding/v2"
    COINGLASS_OI_URL = "https://fapi.coinglass.com/api/futures/openInterest/chart"
    COINGLASS_OI_HISTORY_URL = "https://fapi.coinglass.com/api/futures/openInterest/ohlc-history"
    COINGLASS_TOP_TRADERS_URL = "https://fapi.coinglass.com/api/futures/topLongShortAccountRatio"

    def __init__(self, symbols: list[str] | None = None, poll_interval: int = 300, coinglass_key: str = ""):
        self.symbols = symbols or ["BTC", "ETH"]
        self.poll_interval = poll_interval
        self.coinglass_key = coinglass_key
        self._data: dict[str, WhaleSentimentData] = {}
        self._running = False
        self._background_tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        self._running = True
        self._background_tasks.append(asyncio.create_task(self._poll_loop()))
        logger.info("Whale sentiment monitor started (symbols={}, poll={}s)", self.symbols, self.poll_interval)

    async def stop(self) -> None:
        self._running = False

    def get(self, symbol: str) -> WhaleSentimentData | None:
        clean = symbol.upper().replace("/USDT", "").replace("USDT", "")
        return self._data.get(clean)

    def contrarian_bias(self, symbol: str = "BTC") -> str:
        """Returns contrarian direction based on crowd positioning."""
        d = self.get(symbol)
        if not d:
            return "neutral"
        if d.is_overleveraged_longs:
            return "short"
        if d.is_overleveraged_shorts:
            return "long"
        if d.long_short_ratio > 1.3:
            return "short"
        if d.long_short_ratio < 0.8:
            return "long"
        return "neutral"

    def should_avoid_longs(self, symbol: str = "BTC") -> bool:
        d = self.get(symbol)
        if not d:
            return False
        return d.is_overleveraged_longs

    def should_avoid_shorts(self, symbol: str = "BTC") -> bool:
        d = self.get(symbol)
        if not d:
            return False
        return d.is_overleveraged_shorts

    def breakout_expected(self, symbol: str = "BTC") -> bool:
        d = self.get(symbol)
        if not d:
            return False
        return d.oi_building

    async def _poll_loop(self) -> None:
        while self._running:
            for sym in self.symbols:
                try:
                    await self._fetch_symbol(sym)
                except Exception as e:
                    logger.error("Whale sentiment error for {}: {}", sym, e)
            await asyncio.sleep(self.poll_interval)

    async def _fetch_symbol(self, symbol: str) -> None:
        headers = {}
        if self.coinglass_key:
            headers["coinglassSecret"] = self.coinglass_key

        data = WhaleSentimentData(timestamp=datetime.now(UTC))

        async with aiohttp.ClientSession() as session:
            # Long/Short ratio
            try:
                params: dict[str, str | int] = {"symbol": symbol, "timeType": 2}
                async with session.get(
                    self.COINGLASS_LS_URL, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        json_data = await resp.json()
                        ls_data = json_data.get("data", [])
                        if ls_data and isinstance(ls_data, list):
                            latest = ls_data[-1] if ls_data else {}
                            if isinstance(latest, dict):
                                data.long_short_ratio = float(latest.get("longRate", 50)) / max(
                                    float(latest.get("shortRate", 50)), 0.01
                                )
            except Exception:
                pass

            # Funding rate
            try:
                params: dict[str, str | int] = {"symbol": symbol}
                async with session.get(
                    self.COINGLASS_FUNDING_URL, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        json_data = await resp.json()
                        funding_data = json_data.get("data", [])
                        if funding_data and isinstance(funding_data, list):
                            for item in funding_data:
                                rate = item.get("rate") or item.get("uMarginList", [{}])[0].get("rate", 0)
                                if rate:
                                    data.funding_rate = float(rate)
                                    break
            except Exception:
                pass

            # Open interest details
            oi_snap = OISnapshot(timestamp=datetime.now(UTC))
            try:
                params: dict[str, str | int] = {"symbol": symbol, "timeType": 2}
                async with session.get(
                    self.COINGLASS_OI_URL, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        json_data = await resp.json()
                        oi_data = json_data.get("data", [])
                        if oi_data and isinstance(oi_data, list) and len(oi_data) >= 2:
                            latest = oi_data[-1] if isinstance(oi_data[-1], dict) else {}
                            prev = oi_data[-2] if isinstance(oi_data[-2], dict) else {}
                            cur_oi = float(latest.get("y", 0) or 0)
                            prev_oi = float(prev.get("y", 0) or 0)
                            oi_snap.total_oi_usd = cur_oi
                            if prev_oi > 0:
                                oi_snap.oi_change_1h_pct = ((cur_oi - prev_oi) / prev_oi) * 100
                            data.open_interest_24h_change_pct = oi_snap.oi_change_1h_pct
            except Exception:
                pass

            # Top trader positions (Binance top trader L/S)
            try:
                params: dict[str, str | int] = {"symbol": symbol, "timeType": 2}
                async with session.get(
                    self.COINGLASS_TOP_TRADERS_URL,
                    params=params,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        json_data = await resp.json()
                        tt_data = json_data.get("data", [])
                        if tt_data and isinstance(tt_data, list):
                            latest = tt_data[-1] if isinstance(tt_data[-1], dict) else {}
                            oi_snap.top_trader_long_ratio = float(latest.get("longRate", 50)) / 100.0
            except Exception:
                pass

            data.oi_snapshot = oi_snap

        self._data[symbol] = data

        if data.is_overleveraged_longs:
            logger.warning(
                "WHALE ALERT {}: overleveraged longs (funding={:.4f}%, L/S={:.2f})",
                symbol,
                data.funding_rate * 100,
                data.long_short_ratio,
            )
        elif data.is_overleveraged_shorts:
            logger.warning(
                "WHALE ALERT {}: overleveraged shorts (funding={:.4f}%, L/S={:.2f})",
                symbol,
                data.funding_rate * 100,
                data.long_short_ratio,
            )

    def summary(self) -> str:
        parts = []
        for sym, d in self._data.items():
            tag = ""
            if d.is_overleveraged_longs:
                tag = " ** OVER-LONG **"
            elif d.is_overleveraged_shorts:
                tag = " ** OVER-SHORT **"
            parts.append(f"{sym}: L/S={d.long_short_ratio:.2f} fund={d.funding_rate * 100:.4f}%{tag}")
        return "Whale: " + " | ".join(parts) if parts else "Whale: no data"
