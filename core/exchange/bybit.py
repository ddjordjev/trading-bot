from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import ccxt.async_support as ccxt
from loguru import logger

from core.exchange.base import BaseExchange, parse_order_status, parse_order_type, parse_stop_price, ts_to_dt
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


class BybitExchange(BaseExchange):
    """Bybit implementation via ccxt. Supports spot and USDT perpetual futures."""

    SUPPORTED_MARKET_TYPES = ("spot", "futures")
    HAS_TESTNET = True

    def __init__(self, api_key: str = "", api_secret: str = "", sandbox: bool = True):
        super().__init__(api_key, api_secret, sandbox)
        self._spot = ccxt.bybit(
            {
                "apiKey": api_key,
                "secret": api_secret,
                "options": {"defaultType": "spot"},
                "enableRateLimit": True,
            }
        )
        self._futures = ccxt.bybit(
            {
                "apiKey": api_key,
                "secret": api_secret,
                "options": {"defaultType": "linear"},
                "enableRateLimit": True,
            }
        )
        if sandbox:
            self._spot.set_sandbox_mode(True)
            self._futures.set_sandbox_mode(True)

        self._watchers: list[asyncio.Task[None]] = []

    @property
    def name(self) -> str:
        return "bybit"

    def _client(self, market_type: MarketType = MarketType.SPOT) -> ccxt.bybit:
        return self._futures if market_type == MarketType.FUTURES else self._spot

    def _resolve_symbol(self, symbol: str, market_type: MarketType) -> str:
        """Resolve canonical ccxt market symbol for the selected market type.

        Hub proposals use normalized pair symbols like ``ICP/USDT``.
        Bybit linear futures often require ``ICP/USDT:USDT`` for API calls.
        """
        client = self._client(market_type)
        markets = getattr(client, "markets", {}) or {}
        if symbol in markets:
            return symbol
        if market_type == MarketType.FUTURES and ":" not in symbol:
            futures_variant = f"{symbol}:USDT"
            if futures_variant in markets:
                return futures_variant
        return symbol

    @staticmethod
    def _infer_position_leverage(raw_position: dict[str, Any]) -> int:
        """Infer leverage when exchange payload omits explicit value."""
        raw_leverage = raw_position.get("leverage")
        try:
            if raw_leverage is not None:
                parsed = round(float(raw_leverage))
                if parsed > 0:
                    return parsed
        except (TypeError, ValueError):
            pass

        try:
            margin_pct = float(raw_position.get("initialMarginPercentage", 0) or 0)
            if margin_pct > 0:
                inferred = round(1.0 / margin_pct)
                if inferred > 0:
                    return inferred
        except (TypeError, ValueError, ZeroDivisionError):
            pass

        info = raw_position.get("info") or {}
        try:
            initial_margin = float(raw_position.get("initialMargin") or info.get("positionInitialMargin") or 0)
            notional = abs(float(raw_position.get("notional") or info.get("notional") or 0))
            if initial_margin > 0 and notional > 0:
                inferred = round(notional / initial_margin)
                if inferred > 0:
                    return inferred
        except (TypeError, ValueError):
            pass

        return 1

    @staticmethod
    def _extract_position_level(raw_position: dict[str, Any], keys: tuple[str, ...]) -> float:
        info = raw_position.get("info") or {}
        for key in keys:
            val = raw_position.get(key)
            if val is None:
                val = info.get(key)
            try:
                f = float(val or 0)
                if f > 0:
                    return f
            except (TypeError, ValueError):
                continue
        return 0.0

    async def connect(self) -> None:
        logger.info("Connecting to Bybit (sandbox={})", self.sandbox)
        await self._spot.load_markets()
        await self._futures.load_markets()
        logger.info("Bybit markets loaded: {} spot, {} futures", len(self._spot.markets), len(self._futures.markets))

    async def disconnect(self) -> None:
        for task in self._watchers:
            task.cancel()
        await self._spot.close()
        await self._futures.close()
        logger.info("Bybit disconnected")

    async def fetch_ticker(self, symbol: str, market_type: MarketType = MarketType.SPOT) -> Ticker:
        resolved_symbol = self._resolve_symbol(symbol, market_type)
        data = await self._client(market_type).fetch_ticker(resolved_symbol)
        return Ticker(
            symbol=symbol,
            bid=data.get("bid", 0) or 0,
            ask=data.get("ask", 0) or 0,
            last=data.get("last", 0) or 0,
            volume_24h=data.get("quoteVolume", 0) or 0,
            change_pct_24h=data.get("percentage", 0) or 0,
            timestamp=ts_to_dt(data.get("timestamp")),
        )

    async def fetch_tickers(
        self, symbols: list[str] | None = None, market_type: MarketType = MarketType.SPOT
    ) -> list[Ticker]:
        resolved_symbols = None if symbols is None else [self._resolve_symbol(s, market_type) for s in symbols]
        raw = await self._client(market_type).fetch_tickers(resolved_symbols)
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

    async def fetch_candles(
        self,
        symbol: str,
        timeframe: str = "1m",
        limit: int = 100,
        market_type: MarketType = MarketType.SPOT,
    ) -> list[Candle]:
        resolved_symbol = self._resolve_symbol(symbol, market_type)
        data = await self._client(market_type).fetch_ohlcv(resolved_symbol, timeframe, limit=limit)
        return [Candle(timestamp=ts_to_dt(c[0]), open=c[1], high=c[2], low=c[3], close=c[4], volume=c[5]) for c in data]

    async def fetch_order_book(
        self, symbol: str, limit: int = 20, market_type: MarketType = MarketType.SPOT
    ) -> OrderBook:
        resolved_symbol = self._resolve_symbol(symbol, market_type)
        data = await self._client(market_type).fetch_order_book(resolved_symbol, limit)
        return OrderBook(
            symbol=symbol,
            bids=[(b[0], b[1]) for b in data.get("bids", [])],
            asks=[(a[0], a[1]) for a in data.get("asks", [])],
            timestamp=ts_to_dt(data.get("timestamp")),
        )

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
                    # Spot and derivatives are distinct wallets. Summing both free
                    # amounts for the same asset overstates available equity.
                    result[asset] = max(result.get(asset, 0.0), float(info["free"]))
        except Exception:
            pass
        return {k: v for k, v in result.items() if v > 0}

    async def fetch_positions(self, symbol: str | None = None) -> list[Position]:
        try:
            resolved_symbol = self._resolve_symbol(symbol, MarketType.FUTURES) if symbol else None
            raw = await self._futures.fetch_positions(symbols=[resolved_symbol] if resolved_symbol else None)
        except Exception as e:
            logger.warning("Bybit fetch_positions failed: {}", e)
            return []

        positions = []
        for p in raw:
            amt = abs(float(p.get("contracts", 0) or 0))
            if amt == 0:
                continue
            side_str = p.get("side", "long")
            raw_sym = p.get("symbol", symbol or "")
            norm_sym = raw_sym.split(":")[0] if ":" in raw_sym else raw_sym
            positions.append(
                Position(
                    symbol=norm_sym or (symbol or ""),
                    side=OrderSide.BUY if side_str == "long" else OrderSide.SELL,
                    amount=amt,
                    entry_price=float(p.get("entryPrice", 0) or 0),
                    current_price=float(p.get("markPrice", 0) or 0),
                    leverage=self._infer_position_leverage(p),
                    market_type="futures",
                    stop_loss=self._extract_position_level(
                        p,
                        ("stopLossPrice", "stopPrice", "sl", "slPrice"),
                    )
                    or None,
                    take_profit=self._extract_position_level(
                        p,
                        ("takeProfitPrice", "tp", "tpPrice"),
                    )
                    or None,
                    unrealized_pnl=float(p.get("unrealizedPnl", 0) or 0),
                )
            )
        return positions

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
        resolved_symbol = self._resolve_symbol(symbol, market_type)
        if order_type == OrderType.MARKET:
            ccxt_type = "market"
        elif order_type == OrderType.LIMIT:
            ccxt_type = "limit"
        elif order_type == OrderType.STOP_LOSS:
            ccxt_type = "STOP_MARKET"
        elif order_type == OrderType.TAKE_PROFIT:
            ccxt_type = "TAKE_PROFIT_MARKET"
        else:
            ccxt_type = "limit"
        params: dict[str, Any] = {}
        if stop_price is not None:
            params["stopPrice"] = stop_price
        is_protection_order = order_type in (OrderType.STOP_LOSS, OrderType.TAKE_PROFIT)
        if market_type == MarketType.FUTURES and is_protection_order:
            params["reduceOnly"] = True
        if market_type == MarketType.FUTURES and not is_protection_order:
            # Enforce isolated margin for all futures entries.
            if not await self.set_margin_mode(symbol, "isolated"):
                logger.warning(
                    "Proceeding with {} on {} despite isolated margin-mode set failure", order_type.value, symbol
                )
            if not await self.set_leverage(symbol, leverage):
                logger.warning(
                    "Proceeding with {} on {} despite leverage set failure ({}x)",
                    order_type.value,
                    symbol,
                    leverage,
                )

        logger.info(
            "Placing {} {} {} {} @ {} (leverage={}, stop_price={}, params={})",
            market_type.value,
            side.value,
            ccxt_type,
            symbol,
            price or "market",
            leverage,
            stop_price if stop_price is not None else "none",
            params,
        )

        data = await client.create_order(
            symbol=resolved_symbol,
            type=ccxt_type,
            side=side.value,
            amount=amount,
            price=price,
            params=params,
        )
        logger.info(
            "Order accepted {} {} {} id={} status={} filled={}",
            market_type.value,
            ccxt_type,
            symbol,
            str(data.get("id", "")),
            str(data.get("status", "")),
            float(data.get("filled", 0) or 0),
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
        resolved_symbol = self._resolve_symbol(symbol, market_type)
        data = await client.cancel_order(order_id, resolved_symbol)
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
        client = self._client(market_type)
        resolved_symbol = self._resolve_symbol(symbol, market_type)
        data = await client.fetch_order(order_id, resolved_symbol)
        return Order(
            id=order_id,
            symbol=symbol,
            side=OrderSide.BUY if data.get("side") == "buy" else OrderSide.SELL,
            order_type=parse_order_type(str(data.get("type", ""))),
            amount=float(data.get("amount", 0) or 0),
            stop_price=parse_stop_price(data),
            status=parse_order_status(data.get("status", "")),
            filled=float(data.get("filled", 0) or 0),
            average_price=float(data.get("average", 0) or 0),
            market_type=market_type.value,
        )

    async def fetch_open_orders(
        self, symbol: str | None = None, market_type: MarketType = MarketType.SPOT
    ) -> list[Order]:
        client = self._client(market_type)
        resolved_symbol = self._resolve_symbol(symbol, market_type) if symbol else None
        raw = await client.fetch_open_orders(resolved_symbol)
        return [
            Order(
                id=str(d.get("id", "")),
                symbol=d.get("symbol", ""),
                side=OrderSide.BUY if d.get("side") == "buy" else OrderSide.SELL,
                order_type=parse_order_type(str(d.get("type", ""))),
                amount=float(d.get("amount", 0) or 0),
                stop_price=parse_stop_price(d),
                status=parse_order_status(d.get("status", "")),
                filled=float(d.get("filled", 0) or 0),
                market_type=market_type.value,
            )
            for d in raw
        ]

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        try:
            resolved_symbol = self._resolve_symbol(symbol, MarketType.FUTURES)
            await self._futures.set_leverage(leverage, resolved_symbol)
            logger.debug("Leverage set to {}x for {}", leverage, symbol)
            return True
        except Exception as e:
            logger.warning("Could not set leverage for {}: {}", symbol, e)
            return False

    async def set_margin_mode(self, symbol: str, margin_mode: str) -> bool:
        try:
            resolved_symbol = self._resolve_symbol(symbol, MarketType.FUTURES)
            await self._futures.set_margin_mode(margin_mode, resolved_symbol)
            logger.debug("Margin mode set to {} for {}", margin_mode, symbol)
            return True
        except Exception as e:
            msg = str(e).lower()
            if "no need to change margin type" in msg:
                return True
            logger.warning("Could not set margin mode={} for {}: {}", margin_mode, symbol, e)
            return False

    async def get_available_symbols(self, market_type: MarketType = MarketType.SPOT) -> list[str]:
        client = self._client(market_type)
        return list(client.markets.keys())

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
