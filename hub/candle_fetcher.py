"""Lightweight read-only market data fetcher for the hub.

Uses ccxt to fetch candles and tickers without any trading capability.
The hub only needs price data to run technical analysis centrally.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import ccxt.async_support as ccxt
from loguru import logger

from core.models import Candle, Ticker


def _safe_float(x: object) -> float | None:
    """Return float(x) if valid and finite, else None."""
    if x is None:
        return None
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


class CandleFetcher:
    """Async candle/ticker fetcher backed by a ccxt exchange instance."""

    def __init__(self, exchange_id: str = "binance", sandbox: bool = False, market_type: str = "futures") -> None:
        cls = getattr(ccxt, exchange_id.lower(), None)
        if cls is None:
            cls = ccxt.binance
        default_type = "future" if market_type == "futures" else "spot"
        self._market_type = market_type
        self._exchange: ccxt.Exchange = cls(
            {
                "enableRateLimit": True,
                "options": {"defaultType": default_type},
            }
        )
        if sandbox:
            self._exchange.set_sandbox_mode(True)
        self._loaded = False
        self._unavailable_symbols: set[str] = set()

    @staticmethod
    def _is_unavailable_symbol_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        return (
            "invalid symbol status" in msg
            or "does not have market symbol" in msg
            or "bad symbol" in msg
            or "symbol not found" in msg
        )

    async def _ensure_loaded(self) -> None:
        if not self._loaded:
            await self._exchange.load_markets()
            self._loaded = True

    async def fetch_candles(self, symbol: str, timeframe: str = "1m", limit: int = 200) -> list[Candle]:
        try:
            await self._ensure_loaded()
        except Exception as e:
            logger.warning("CandleFetcher: load_markets failed: {}", e)
            return []
        try:
            raw = await self._exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        except Exception as e:
            if self._is_unavailable_symbol_error(e):
                self._unavailable_symbols.add(symbol)
                logger.debug("CandleFetcher: skipping unavailable symbol {}: {}", symbol, e)
            else:
                logger.warning("CandleFetcher: failed to fetch {} {}: {}", symbol, timeframe, e)
            return []
        candles = []
        for row in raw:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                continue
            ts = row[0]
            try:
                ts_sec = int(ts) / 1000 if ts is not None else None
            except (TypeError, ValueError):
                ts_sec = None
            if ts_sec is None:
                continue
            o = _safe_float(row[1])
            h = _safe_float(row[2])
            low = _safe_float(row[3])
            c = _safe_float(row[4])
            vol = _safe_float(row[5])
            if o is None or h is None or low is None or c is None or vol is None:
                continue
            candles.append(
                Candle(
                    timestamp=datetime.fromtimestamp(ts_sec, tz=UTC),
                    open=o,
                    high=h,
                    low=low,
                    close=c,
                    volume=vol,
                )
            )
        return candles

    async def fetch_ticker(self, symbol: str) -> Ticker | None:
        try:
            await self._ensure_loaded()
        except Exception as e:
            logger.warning("CandleFetcher: load_markets failed: {}", e)
            return None
        try:
            raw = await self._exchange.fetch_ticker(symbol)
        except Exception as e:
            if self._is_unavailable_symbol_error(e):
                self._unavailable_symbols.add(symbol)
                logger.debug("CandleFetcher: skipping unavailable ticker symbol {}: {}", symbol, e)
            else:
                logger.warning("CandleFetcher: failed to fetch ticker {}: {}", symbol, e)
            return None
        bid = _safe_float(raw.get("bid")) or 0.0
        ask = _safe_float(raw.get("ask")) or 0.0
        last = _safe_float(raw.get("last")) or 0.0
        return Ticker(
            symbol=symbol,
            bid=bid,
            ask=ask,
            last=last,
            volume_24h=_safe_float(raw.get("quoteVolume")) or 0.0,
            change_pct_24h=_safe_float(raw.get("percentage")) or 0.0,
            timestamp=datetime.now(UTC),
        )

    def has_symbol(self, symbol: str) -> bool:
        if not self._loaded:
            return True
        if symbol in self._unavailable_symbols:
            return False
        market = self._exchange.markets.get(symbol)
        if not isinstance(market, dict):
            return False
        if not bool(market.get("active", True)):
            return False
        if self._market_type == "futures":
            return bool(market.get("future") or market.get("swap"))
        return bool(market.get("spot"))

    async def close(self) -> None:
        await self._exchange.close()
