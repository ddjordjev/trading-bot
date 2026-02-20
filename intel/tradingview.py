from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from enum import Enum

import aiohttp
from loguru import logger
from pydantic import BaseModel


class TVRating(str, Enum):
    STRONG_BUY = "STRONG_BUY"
    BUY = "BUY"
    NEUTRAL = "NEUTRAL"
    SELL = "SELL"
    STRONG_SELL = "STRONG_SELL"


class TVAnalysis(BaseModel):
    """TradingView technical analysis summary for a symbol."""

    symbol: str
    exchange: str = "MEXC"
    interval: str = "1h"

    summary_rating: TVRating = TVRating.NEUTRAL
    oscillators_rating: TVRating = TVRating.NEUTRAL
    moving_averages_rating: TVRating = TVRating.NEUTRAL

    buy_count: int = 0
    sell_count: int = 0
    neutral_count: int = 0
    total_signals: int = 0

    rsi_14: float = 0.0
    macd_signal: float = 0.0
    adx_14: float = 0.0
    atr_14: float = 0.0
    ema_20: float = 0.0
    sma_50: float = 0.0
    sma_200: float = 0.0
    bb_upper: float = 0.0
    bb_lower: float = 0.0

    fetched_at: datetime = datetime.now(UTC)

    @property
    def is_strong_signal(self) -> bool:
        return self.summary_rating in (TVRating.STRONG_BUY, TVRating.STRONG_SELL)

    @property
    def signal_direction(self) -> str:
        if self.summary_rating in (TVRating.BUY, TVRating.STRONG_BUY):
            return "long"
        if self.summary_rating in (TVRating.SELL, TVRating.STRONG_SELL):
            return "short"
        return "neutral"

    @property
    def confidence(self) -> float:
        """0-1 how confident TV is in the signal direction."""
        if self.total_signals == 0:
            return 0.0
        dominant = max(self.buy_count, self.sell_count, self.neutral_count)
        return dominant / self.total_signals

    @property
    def trend_aligned(self) -> bool:
        """MA and oscillators agree on direction."""
        return self.oscillators_rating.value.replace("STRONG_", "") == self.moving_averages_rating.value.replace(
            "STRONG_", ""
        )


class TradingViewClient:
    """Fetches technical analysis ratings from TradingView's scanner API.

    Uses the public scanner endpoint to get oscillator, moving average,
    and summary ratings for any symbol. No API key needed.

    Timeframes:
    - 1m, 5m, 15m, 1h, 4h, 1D, 1W, 1M
    - We use 1h for scalps, 4h for swing, 1D for context
    """

    SCANNER_URL = "https://scanner.tradingview.com/crypto/scan"

    INDICATOR_COLUMNS = [
        "Recommend.All",
        "Recommend.Other",
        "Recommend.MA",
        "RSI",
        "RSI[1]",
        "MACD.macd",
        "MACD.signal",
        "ADX",
        "ATR",
        "EMA20",
        "SMA50",
        "SMA200",
        "BB.upper",
        "BB.lower",
        "Rec.Stoch.RSI",
        "Rec.WR",
        "Rec.BBPower",
        "Rec.UO",
        "Rec.Ichimoku",
        "Rec.VWMA",
        "Rec.HullMA9",
    ]

    INTERVAL_MAP = {
        "1m": "|1",
        "5m": "|5",
        "15m": "|15",
        "1h": "|60",
        "4h": "|240",
        "1D": "",
        "1W": "|1W",
        "1M": "|1M",
    }

    def __init__(self, exchange: str = "MEXC", intervals: list[str] | None = None, poll_interval: int = 120):
        self.exchange = exchange.upper()
        self.intervals = intervals or ["1h", "4h", "1D"]
        self.poll_interval = poll_interval
        self._cache: dict[str, dict[str, TVAnalysis]] = {}
        self._running = False
        self._poll_symbols: list[str] = []
        self._poll_task: asyncio.Task[None] | None = None

    def set_poll_symbols(self, symbols: list[str]) -> None:
        """Configure which symbols are periodically refreshed."""
        self._poll_symbols = list(symbols)

    async def start(self) -> None:
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info(
            "TradingView client started (exchange={}, intervals={}, poll={}s, symbols={})",
            self.exchange,
            self.intervals,
            self.poll_interval,
            self._poll_symbols or "none (call set_poll_symbols)",
        )

    async def stop(self) -> None:
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
            self._poll_task = None

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                for symbol in self._poll_symbols:
                    if not self._running:
                        break
                    await self.full_analysis(symbol)
            except Exception as e:
                logger.debug("TV poll error: {}", e)
            await asyncio.sleep(self.poll_interval)

    async def analyze(self, symbol: str, interval: str = "1h") -> TVAnalysis | None:
        """Get TV analysis for a single symbol at a given interval."""
        clean = symbol.upper().replace("/", "").replace("-", "")
        tv_symbol = f"{self.exchange}:{clean}"
        suffix = self.INTERVAL_MAP.get(interval, "|60")

        columns = [f"{col}{suffix}" if suffix and "|" not in col else col for col in self.INDICATOR_COLUMNS]

        payload = {
            "symbols": {"tickers": [tv_symbol]},
            "columns": columns,
        }

        try:
            async with (
                aiohttp.ClientSession() as session,
                session.post(
                    self.SCANNER_URL,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp,
            ):
                if resp.status != 200:
                    logger.debug("TV scanner {} returned {}", tv_symbol, resp.status)
                    return None
                data = await resp.json()
        except Exception as e:
            logger.debug("TV fetch error for {}: {}", symbol, e)
            return None

        if not isinstance(data, dict):
            return None
        rows = data.get("data", [])
        if not rows or not isinstance(rows, list):
            return None

        first = rows[0]
        if not isinstance(first, dict):
            return None
        vals = first.get("d", [])
        if len(vals) < 14:
            return None

        def _safe(idx: int) -> float:
            v = vals[idx] if idx < len(vals) else None
            return float(v) if v is not None else 0.0

        recommend_all = _safe(0)
        recommend_osc = _safe(1)
        recommend_ma = _safe(2)

        analysis = TVAnalysis(
            symbol=symbol,
            exchange=self.exchange,
            interval=interval,
            summary_rating=self._score_to_rating(recommend_all),
            oscillators_rating=self._score_to_rating(recommend_osc),
            moving_averages_rating=self._score_to_rating(recommend_ma),
            rsi_14=_safe(3),
            macd_signal=_safe(6),
            adx_14=_safe(7),
            atr_14=_safe(8),
            ema_20=_safe(9),
            sma_50=_safe(10),
            sma_200=_safe(11),
            bb_upper=_safe(12),
            bb_lower=_safe(13),
            fetched_at=datetime.now(UTC),
        )

        analysis.buy_count = sum(1 for i in range(14, len(vals)) if vals[i] is not None and vals[i] > 0)
        analysis.sell_count = sum(1 for i in range(14, len(vals)) if vals[i] is not None and vals[i] < 0)
        analysis.neutral_count = sum(1 for i in range(14, len(vals)) if vals[i] is not None and vals[i] == 0)
        analysis.total_signals = analysis.buy_count + analysis.sell_count + analysis.neutral_count

        self._cache.setdefault(symbol, {})[interval] = analysis
        return analysis

    async def analyze_multi(self, symbols: list[str], interval: str = "1h") -> dict[str, TVAnalysis]:
        """Batch analyze multiple symbols."""
        results: dict[str, TVAnalysis] = {}
        tasks = [self.analyze(sym, interval) for sym in symbols]
        analyses = await asyncio.gather(*tasks, return_exceptions=True)
        for sym, result in zip(symbols, analyses, strict=False):
            if isinstance(result, TVAnalysis):
                results[sym] = result
        return results

    async def full_analysis(self, symbol: str) -> dict[str, TVAnalysis]:
        """Analyze a symbol across all configured intervals."""
        results: dict[str, TVAnalysis] = {}
        for interval in self.intervals:
            a = await self.analyze(symbol, interval)
            if a:
                results[interval] = a
        return results

    def get_cached(self, symbol: str, interval: str = "1h") -> TVAnalysis | None:
        return self._cache.get(symbol, {}).get(interval)

    def get_all_cached(self) -> dict[str, dict[str, TVAnalysis]]:
        """Public accessor for all cached analyses."""
        return dict(self._cache)

    def consensus(self, symbol: str) -> str:
        """Cross-interval consensus for a symbol."""
        cached = self._cache.get(symbol, {})
        if not cached:
            return "no_data"

        long_votes = sum(1 for a in cached.values() if a.signal_direction == "long")
        short_votes = sum(1 for a in cached.values() if a.signal_direction == "short")
        total = len(cached)

        if long_votes > total * 0.6:
            return "long"
        if short_votes > total * 0.6:
            return "short"
        return "neutral"

    def signal_boost(self, symbol: str, proposed_side: str) -> float:
        """Multiplier for signal strength based on TV alignment.

        If TV agrees with the proposed trade direction, boost.
        If TV disagrees, penalize.
        """
        cached = self._cache.get(symbol, {})
        if not cached:
            return 1.0

        hourly = cached.get("1h")
        if not hourly:
            return 1.0

        if hourly.signal_direction == proposed_side:
            boost = 1.1 if hourly.confidence >= 0.6 else 1.0
            if hourly.is_strong_signal:
                boost = 1.2
            if hourly.trend_aligned:
                boost += 0.1
            return min(boost, 1.3)

        if hourly.signal_direction != "neutral" and hourly.signal_direction != proposed_side:
            return 0.7 if hourly.is_strong_signal else 0.85

        return 1.0

    @staticmethod
    def _score_to_rating(score: float) -> TVRating:
        if score >= 0.5:
            return TVRating.STRONG_BUY
        if score >= 0.1:
            return TVRating.BUY
        if score > -0.1:
            return TVRating.NEUTRAL
        if score > -0.5:
            return TVRating.SELL
        return TVRating.STRONG_SELL

    def summary(self, symbol: str = "") -> str:
        if symbol:
            cached = self._cache.get(symbol, {})
            if not cached:
                return f"TV ({symbol}): no data"
            parts = [f"{itv}: {a.summary_rating.value} ({a.confidence:.0%})" for itv, a in cached.items()]
            return f"TV ({symbol}): {' | '.join(parts)} => {self.consensus(symbol)}"

        if not self._cache:
            return "TradingView: no data cached"
        lines = [f"TradingView ({len(self._cache)} symbols cached):"]
        for sym in sorted(self._cache)[:10]:
            lines.append(f"  {sym}: {self.consensus(sym)}")
        return "\n".join(lines)
