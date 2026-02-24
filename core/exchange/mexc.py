from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import ccxt.async_support as ccxt
from loguru import logger

from core.exchange.base import BaseExchange, parse_order_status, ts_to_dt
from core.models import (
    Candle,
    MarketType,
    Order,
    OrderBook,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Ticker,
)


class MexcExchange(BaseExchange):
    """MEXC implementation via ccxt.

    Spot only -- MEXC restricts futures/swap to institutional accounts.
    Retail users cannot trade futures on MEXC.
    """

    SUPPORTED_MARKET_TYPES = ("spot",)
    HAS_TESTNET = False

    def __init__(self, api_key: str = "", api_secret: str = "", sandbox: bool = True):
        super().__init__(api_key, api_secret, sandbox)
        self._spot = ccxt.mexc(
            {
                "apiKey": api_key,
                "secret": api_secret,
                "options": {"defaultType": "spot"},
                "enableRateLimit": True,
            }
        )
        if sandbox:
            self._spot.set_sandbox_mode(True)

        self._watchers: list[asyncio.Task[None]] = []

    @property
    def name(self) -> str:
        return "mexc"

    async def connect(self) -> None:
        logger.info("Connecting to MEXC (sandbox={}, spot only)", self.sandbox)
        await self._spot.load_markets()
        logger.info("MEXC markets loaded: {} spot symbols", len(self._spot.markets))

    async def disconnect(self) -> None:
        for task in self._watchers:
            task.cancel()
        await self._spot.close()
        logger.info("MEXC disconnected")

    # -- Market Data --

    async def fetch_ticker(self, symbol: str) -> Ticker:
        data = await self._spot.fetch_ticker(symbol)
        return Ticker(
            symbol=symbol,
            bid=data.get("bid", 0) or 0,
            ask=data.get("ask", 0) or 0,
            last=data.get("last", 0) or 0,
            volume_24h=data.get("quoteVolume", 0) or 0,
            change_pct_24h=data.get("percentage", 0) or 0,
            timestamp=ts_to_dt(data.get("timestamp")),
        )

    async def fetch_tickers(self, symbols: list[str] | None = None) -> list[Ticker]:
        raw = await self._spot.fetch_tickers(symbols)
        tickers = []
        for sym, data in raw.items():
            tickers.append(
                Ticker(
                    symbol=sym,
                    bid=data.get("bid", 0) or 0,
                    ask=data.get("ask", 0) or 0,
                    last=data.get("last", 0) or 0,
                    volume_24h=data.get("quoteVolume", 0) or 0,
                    change_pct_24h=data.get("percentage", 0) or 0,
                    timestamp=ts_to_dt(data.get("timestamp")),
                )
            )
        return tickers

    async def fetch_candles(self, symbol: str, timeframe: str = "1m", limit: int = 100) -> list[Candle]:
        data = await self._spot.fetch_ohlcv(symbol, timeframe, limit=limit)
        return [
            Candle(
                timestamp=ts_to_dt(c[0]),
                open=c[1],
                high=c[2],
                low=c[3],
                close=c[4],
                volume=c[5],
            )
            for c in data
        ]

    async def fetch_order_book(self, symbol: str, limit: int = 20) -> OrderBook:
        data = await self._spot.fetch_order_book(symbol, limit)
        return OrderBook(
            symbol=symbol,
            bids=[(b[0], b[1]) for b in data.get("bids", [])],
            asks=[(a[0], a[1]) for a in data.get("asks", [])],
            timestamp=ts_to_dt(data.get("timestamp")),
        )

    # -- Account --

    async def fetch_balance(self) -> dict[str, float]:
        data = await self._spot.fetch_balance()
        result: dict[str, float] = {}
        for asset, info in data.items():
            if isinstance(info, dict) and info.get("free", 0) > 0:
                result[asset] = float(info["free"])
        return result

    async def fetch_positions(self, symbol: str | None = None) -> list[Position]:
        return []

    # -- Trading --

    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        amount: float,
        price: float | None = None,
        stop_price: float | None = None,
        leverage: int = 1,
        market_type: MarketType = MarketType.SPOT,
    ) -> Order:
        if market_type == MarketType.FUTURES:
            logger.error("MEXC does not support futures for retail accounts")
            return Order(
                id="",
                symbol=symbol,
                side=side,
                order_type=order_type,
                amount=amount,
                price=price,
                status=OrderStatus.FAILED,
                market_type="spot",
            )

        ccxt_type = "market" if order_type == OrderType.MARKET else "limit"
        params: dict[str, Any] = {}
        if stop_price is not None:
            params["stopPrice"] = stop_price

        logger.info("Placing spot {} {} {} @ {}", side.value, ccxt_type, symbol, price or "market")

        data = await self._spot.create_order(
            symbol=symbol,
            type=ccxt_type,
            side=side.value,
            amount=amount,
            price=price,
            params=params,
        )

        return Order(
            id=str(data.get("id", "")),
            symbol=symbol,
            side=side,
            order_type=order_type,
            amount=amount,
            price=price,
            stop_price=stop_price,
            status=parse_order_status(data.get("status", "open")),
            filled=float(data.get("filled", 0) or 0),
            average_price=float(data.get("average", 0) or 0),
            leverage=1,
            market_type="spot",
        )

    async def cancel_order(self, order_id: str, symbol: str, market_type: MarketType = MarketType.SPOT) -> Order:
        data = await self._spot.cancel_order(order_id, symbol)
        return Order(
            id=order_id,
            symbol=symbol,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            amount=float(data.get("amount", 0) or 0),
            status=OrderStatus.CANCELLED,
            market_type=market_type.value,
        )

    async def fetch_order(self, order_id: str, symbol: str, market_type: MarketType = MarketType.SPOT) -> Order:
        data = await self._spot.fetch_order(order_id, symbol)
        return Order(
            id=order_id,
            symbol=symbol,
            side=OrderSide.BUY if data.get("side") == "buy" else OrderSide.SELL,
            order_type=OrderType.MARKET if data.get("type") == "market" else OrderType.LIMIT,
            amount=float(data.get("amount", 0) or 0),
            status=parse_order_status(data.get("status", "")),
            filled=float(data.get("filled", 0) or 0),
            average_price=float(data.get("average", 0) or 0),
            market_type=market_type.value,
        )

    async def fetch_open_orders(
        self, symbol: str | None = None, market_type: MarketType = MarketType.SPOT
    ) -> list[Order]:
        raw = await self._spot.fetch_open_orders(symbol)
        orders = []
        for data in raw:
            orders.append(
                Order(
                    id=str(data.get("id", "")),
                    symbol=data.get("symbol", ""),
                    side=OrderSide.BUY if data.get("side") == "buy" else OrderSide.SELL,
                    order_type=OrderType.MARKET if data.get("type") == "market" else OrderType.LIMIT,
                    amount=float(data.get("amount", 0) or 0),
                    status=parse_order_status(data.get("status", "")),
                    filled=float(data.get("filled", 0) or 0),
                    market_type=market_type.value,
                )
            )
        return orders

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        pass

    # -- Symbols --

    async def get_available_symbols(self, market_type: MarketType = MarketType.SPOT) -> list[str]:
        if market_type == MarketType.FUTURES:
            return []
        return list(self._spot.markets.keys())

    # -- Streaming --

    async def watch_ticker(self, symbol: str, callback: Callable[..., Any]) -> None:
        async def _loop() -> None:
            supports_ws = hasattr(self._spot, "watch_ticker")
            while True:
                try:
                    if supports_ws:
                        data = await self._spot.watch_ticker(symbol)
                    else:
                        data = await self._spot.fetch_ticker(symbol)
                    ticker = Ticker(
                        symbol=symbol,
                        bid=data.get("bid", 0) or 0,
                        ask=data.get("ask", 0) or 0,
                        last=data.get("last", 0) or 0,
                        volume_24h=data.get("quoteVolume", 0) or 0,
                        change_pct_24h=data.get("percentage", 0) or 0,
                        timestamp=ts_to_dt(data.get("timestamp")),
                    )
                    await callback(ticker)
                    if not supports_ws:
                        await asyncio.sleep(1)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error("Ticker watch error for {}: {}", symbol, e)
                    await asyncio.sleep(5)

        task = asyncio.create_task(_loop())
        self._watchers.append(task)

    async def watch_candles(self, symbol: str, timeframe: str, callback: Callable[..., Any]) -> None:
        async def _loop() -> None:
            supports_ws = hasattr(self._spot, "watch_ohlcv")
            while True:
                try:
                    if supports_ws:
                        data = await self._spot.watch_ohlcv(symbol, timeframe)
                    else:
                        data = await self._spot.fetch_ohlcv(symbol, timeframe, limit=2)
                    candles = data if supports_ws else data[-1:]
                    for c in candles:
                        candle = Candle(
                            timestamp=ts_to_dt(c[0]),
                            open=c[1],
                            high=c[2],
                            low=c[3],
                            close=c[4],
                            volume=c[5],
                        )
                        await callback(candle)
                    if not supports_ws:
                        await asyncio.sleep(2)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error("Candle watch error for {}: {}", symbol, e)
                    await asyncio.sleep(5)

        task = asyncio.create_task(_loop())
        self._watchers.append(task)
