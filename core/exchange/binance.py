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
from web.metrics import timed


class BinanceExchange(BaseExchange):
    """Binance implementation via ccxt. Supports spot and USDT-M futures."""

    SUPPORTED_MARKET_TYPES = ("spot", "futures")
    HAS_TESTNET = True

    def __init__(self, api_key: str = "", api_secret: str = "", sandbox: bool = True):
        super().__init__(api_key, api_secret, sandbox)
        self._spot = ccxt.binance(
            {
                "apiKey": api_key,
                "secret": api_secret,
                "options": {"defaultType": "spot"},
                "enableRateLimit": True,
            }
        )
        self._futures = ccxt.binance(
            {
                "apiKey": api_key,
                "secret": api_secret,
                "options": {"defaultType": "future"},
                "enableRateLimit": True,
            }
        )
        if sandbox:
            self._spot.set_sandbox_mode(True)
            self._futures.set_sandbox_mode(True)

        self._watchers: list[asyncio.Task[None]] = []

    @property
    def name(self) -> str:
        return "binance"

    def _client(self, market_type: MarketType = MarketType.SPOT) -> ccxt.binance:
        return self._futures if market_type == MarketType.FUTURES else self._spot

    async def connect(self) -> None:
        logger.info("Connecting to Binance (sandbox={})", self.sandbox)
        await self._spot.load_markets()
        await self._futures.load_markets()
        logger.info("Binance markets loaded: {} spot, {} futures", len(self._spot.markets), len(self._futures.markets))

    async def disconnect(self) -> None:
        for task in self._watchers:
            task.cancel()
        await self._spot.close()
        await self._futures.close()
        logger.info("Binance disconnected")

    @timed("exchange.fetch_ticker")
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
        return [
            Ticker(
                symbol=sym,
                bid=d.get("bid", 0) or 0,
                ask=d.get("ask", 0) or 0,
                last=d.get("last", 0) or 0,
                volume_24h=d.get("quoteVolume", 0) or 0,
                change_pct_24h=d.get("percentage", 0) or 0,
                timestamp=ts_to_dt(d.get("timestamp")),
            )
            for sym, d in raw.items()
        ]

    @timed("exchange.fetch_candles")
    async def fetch_candles(self, symbol: str, timeframe: str = "1m", limit: int = 100) -> list[Candle]:
        data = await self._spot.fetch_ohlcv(symbol, timeframe, limit=limit)
        return [Candle(timestamp=ts_to_dt(c[0]), open=c[1], high=c[2], low=c[3], close=c[4], volume=c[5]) for c in data]

    async def fetch_order_book(self, symbol: str, limit: int = 20) -> OrderBook:
        data = await self._spot.fetch_order_book(symbol, limit)
        return OrderBook(
            symbol=symbol,
            bids=[(b[0], b[1]) for b in data.get("bids", [])],
            asks=[(a[0], a[1]) for a in data.get("asks", [])],
            timestamp=ts_to_dt(data.get("timestamp")),
        )

    @timed("exchange.fetch_balance")
    async def fetch_balance(self) -> dict[str, float]:
        data = await self._spot.fetch_balance()
        result: dict[str, float] = {}
        for asset, info in data.items():
            if isinstance(info, dict) and info.get("free", 0) > 0:
                result[asset] = float(info["free"])
        try:
            futures_data = await self._futures.fetch_balance()
            for asset, info in futures_data.items():
                if isinstance(info, dict) and info.get("free", 0) > 0:
                    result[asset] = result.get(asset, 0) + float(info["free"])
        except Exception:
            pass
        return {k: v for k, v in result.items() if v > 0}

    @timed("exchange.fetch_positions")
    async def fetch_positions(self, symbol: str | None = None) -> list[Position]:
        try:
            raw = await self._futures.fetch_positions(symbols=[symbol] if symbol else None)
        except Exception:
            return []

        positions = []
        for p in raw:
            amt = abs(float(p.get("contracts", 0) or 0))
            if amt == 0:
                continue
            side_str = p.get("side", "long")
            positions.append(
                Position(
                    symbol=p.get("symbol", symbol or ""),
                    side=OrderSide.BUY if side_str == "long" else OrderSide.SELL,
                    amount=amt,
                    entry_price=float(p.get("entryPrice", 0) or 0),
                    current_price=float(p.get("markPrice", 0) or 0),
                    leverage=int(p.get("leverage", 1) or 1),
                    market_type="futures",
                    unrealized_pnl=float(p.get("unrealizedPnl", 0) or 0),
                )
            )
        return positions

    @timed("exchange.place_order")
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
        client = self._client(market_type)
        ccxt_type = "market" if order_type == OrderType.MARKET else "limit"
        params: dict[str, Any] = {}
        if stop_price is not None:
            params["stopPrice"] = stop_price
        if market_type == MarketType.FUTURES:
            await self.set_leverage(symbol, leverage)

        logger.info(
            "Placing {} {} {} {} @ {} (leverage={})",
            market_type.value,
            side.value,
            ccxt_type,
            symbol,
            price or "market",
            leverage,
        )

        data = await client.create_order(
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
            leverage=leverage,
            market_type=market_type.value,
        )

    async def cancel_order(self, order_id: str, symbol: str, market_type: MarketType = MarketType.SPOT) -> Order:
        client = self._client(market_type)
        data = await client.cancel_order(order_id, symbol)
        return Order(
            id=order_id,
            symbol=symbol,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            amount=float(data.get("amount", 0) or 0),
            status=OrderStatus.CANCELLED,
        )

    async def fetch_order(self, order_id: str, symbol: str, market_type: MarketType = MarketType.SPOT) -> Order:
        client = self._client(market_type)
        data = await client.fetch_order(order_id, symbol)
        return Order(
            id=order_id,
            symbol=symbol,
            side=OrderSide.BUY if data.get("side") == "buy" else OrderSide.SELL,
            order_type=OrderType.MARKET if data.get("type") == "market" else OrderType.LIMIT,
            amount=float(data.get("amount", 0) or 0),
            status=parse_order_status(data.get("status", "")),
            filled=float(data.get("filled", 0) or 0),
            average_price=float(data.get("average", 0) or 0),
        )

    async def fetch_open_orders(
        self, symbol: str | None = None, market_type: MarketType = MarketType.SPOT
    ) -> list[Order]:
        client = self._client(market_type)
        raw = await client.fetch_open_orders(symbol)
        return [
            Order(
                id=str(d.get("id", "")),
                symbol=d.get("symbol", ""),
                side=OrderSide.BUY if d.get("side") == "buy" else OrderSide.SELL,
                order_type=OrderType.MARKET if d.get("type") == "market" else OrderType.LIMIT,
                amount=float(d.get("amount", 0) or 0),
                status=parse_order_status(d.get("status", "")),
                filled=float(d.get("filled", 0) or 0),
            )
            for d in raw
        ]

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        try:
            await self._futures.set_leverage(leverage, symbol)
            logger.debug("Leverage set to {}x for {}", leverage, symbol)
        except Exception as e:
            logger.warning("Could not set leverage for {}: {}", symbol, e)

    async def get_available_symbols(self, market_type: MarketType = MarketType.SPOT) -> list[str]:
        client = self._client(market_type)
        return list(client.markets.keys())

    async def watch_ticker(self, symbol: str, callback: Callable[..., Any]) -> None:
        async def _loop() -> None:
            while True:
                try:
                    data = await self._spot.watch_ticker(symbol)
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
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    msg = str(e)
                    if "not supported" in msg.lower():
                        logger.warning("watchTicker not supported for {} — unsubscribing", symbol)
                        break
                    logger.error("Ticker watch error for {}: {}", symbol, e)
                    await asyncio.sleep(5)

        task = asyncio.create_task(_loop())
        self._watchers.append(task)

    async def watch_candles(self, symbol: str, timeframe: str, callback: Callable[..., Any]) -> None:
        async def _loop() -> None:
            while True:
                try:
                    data = await self._spot.watch_ohlcv(symbol, timeframe)
                    for c in data:
                        candle = Candle(
                            timestamp=ts_to_dt(c[0]),
                            open=c[1],
                            high=c[2],
                            low=c[3],
                            close=c[4],
                            volume=c[5],
                        )
                        await callback(candle)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error("Candle watch error for {}: {}", symbol, e)
                    await asyncio.sleep(5)

        task = asyncio.create_task(_loop())
        self._watchers.append(task)
