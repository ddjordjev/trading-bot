from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import ccxt.async_support as ccxt
from loguru import logger

from core.exchange.base import (
    BaseExchange,
    extract_position_level,
    infer_position_leverage,
    parse_order_status,
    parse_order_type,
    parse_stop_price,
    ts_to_dt,
)
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
        self._positions_cache: tuple[float, list[Position]] | None = None
        self._positions_cache_ttl_secs = 1.5
        self._positions_cache_lock = asyncio.Lock()
        self._leverage_by_symbol: dict[str, int] = {}

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
    def _extract_order_id(payload: dict[str, Any]) -> str:
        direct = str(payload.get("id", "") or "").strip()
        if direct:
            return direct
        info = payload.get("info")
        if isinstance(info, dict):
            for key in ("orderId", "order_id", "stopOrderId", "triggerOrderId"):
                value = str(info.get(key, "") or "").strip()
                if value:
                    return value
        return ""

    @staticmethod
    def _infer_bybit_order_type(payload: dict[str, Any]) -> OrderType:
        """Infer Bybit conditional order type when ccxt reports generic market type."""
        parsed = parse_order_type(str(payload.get("type", "")))
        if parsed in {OrderType.STOP_LOSS, OrderType.TAKE_PROFIT, OrderType.STOP_LIMIT}:
            return parsed

        info = payload.get("info")
        if isinstance(info, dict):
            stop_order_type = str(info.get("stopOrderType", "") or info.get("stop_order_type", "")).strip().lower()
            if "stop" in stop_order_type and "loss" in stop_order_type:
                return OrderType.STOP_LOSS
            if "take" in stop_order_type and "profit" in stop_order_type:
                return OrderType.TAKE_PROFIT

            stop_price = parse_stop_price(payload)
            trigger_direction_raw = info.get("triggerDirection", info.get("trigger_direction"))
            reduce_only_raw = info.get("reduceOnly", info.get("reduce_only"))
            side_txt = str(payload.get("side", "") or "").strip().lower()
            is_buy = side_txt == "buy"
            is_sell = side_txt == "sell"
            is_reduce_only = str(reduce_only_raw).strip().lower() in {"true", "1"} or reduce_only_raw is True
            try:
                trigger_direction = int(float(trigger_direction_raw)) if trigger_direction_raw is not None else None
            except (TypeError, ValueError):
                trigger_direction = None

            if stop_price and is_reduce_only and trigger_direction in {1, 2} and (is_buy or is_sell):
                if (is_sell and trigger_direction == 2) or (is_buy and trigger_direction == 1):
                    return OrderType.STOP_LOSS
                if (is_sell and trigger_direction == 1) or (is_buy and trigger_direction == 2):
                    return OrderType.TAKE_PROFIT

        return parsed

    @staticmethod
    def _infer_position_leverage(raw_position: dict[str, Any]) -> int:
        return infer_position_leverage(raw_position)

    @staticmethod
    def _extract_position_level(raw_position: dict[str, Any], keys: tuple[str, ...]) -> float:
        return extract_position_level(raw_position, keys)

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
        now = time.monotonic()
        cached = self._positions_cache
        if cached and (now - cached[0]) < self._positions_cache_ttl_secs:
            positions = cached[1]
            if symbol:
                resolved_symbol = self._resolve_symbol(symbol, MarketType.FUTURES)
                normalized = symbol.split(":")[0] if ":" in symbol else symbol
                return [p for p in positions if p.symbol in {symbol, normalized, resolved_symbol}]
            return positions

        try:
            async with self._positions_cache_lock:
                now = time.monotonic()
                cached = self._positions_cache
                if cached and (now - cached[0]) < self._positions_cache_ttl_secs:
                    positions = cached[1]
                else:
                    resolved_symbol_opt = self._resolve_symbol(symbol, MarketType.FUTURES) if symbol else None
                    raw = await self._futures.fetch_positions(
                        symbols=[resolved_symbol_opt] if resolved_symbol_opt else None
                    )
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
                    self._positions_cache = (time.monotonic(), positions)
        except Exception as e:
            logger.warning("Bybit fetch_positions failed: {}", e)
            if cached:
                positions = cached[1]
            else:
                return []
        if symbol:
            resolved_symbol = self._resolve_symbol(symbol, MarketType.FUTURES)
            normalized = symbol.split(":")[0] if ":" in symbol else symbol
            return [p for p in positions if p.symbol in {symbol, normalized, resolved_symbol}]
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
        elif order_type in (OrderType.STOP_LOSS, OrderType.TAKE_PROFIT):
            ccxt_type = "market"
        else:
            ccxt_type = "limit"
        params: dict[str, Any] = {}
        if stop_price is not None:
            params["stopPrice"] = stop_price
        is_protection_order = order_type in (OrderType.STOP_LOSS, OrderType.TAKE_PROFIT)
        if market_type == MarketType.FUTURES and is_protection_order:
            params["reduceOnly"] = True
            if stop_price is not None:
                params["triggerPrice"] = stop_price
            if order_type == OrderType.STOP_LOSS:
                params["triggerDirection"] = 1 if side == OrderSide.BUY else 2
            elif order_type == OrderType.TAKE_PROFIT:
                params["triggerDirection"] = 2 if side == OrderSide.BUY else 1
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

        create_order_kwargs: dict[str, Any] = {
            "symbol": resolved_symbol,
            "type": ccxt_type,
            "side": side.value,
            "amount": amount,
            "params": params,
        }
        # Avoid sending price=None/0 on market orders; Bybit may reject with
        # retCode 30209 ("price lower than minimum selling price").
        if ccxt_type == "limit":
            if price is None or float(price) <= 0:
                raise ValueError(f"Invalid limit price for {symbol}: {price!r}")
            create_order_kwargs["price"] = float(price)
        elif price is not None and float(price) > 0:
            # Some adapters pass a price hint on market orders; keep it only if valid.
            create_order_kwargs["price"] = float(price)

        data = await client.create_order(**create_order_kwargs)
        order_id = self._extract_order_id(data)
        logger.info(
            "Order accepted {} {} {} id={} status={} filled={}",
            market_type.value,
            ccxt_type,
            symbol,
            order_id,
            str(data.get("status", "")),
            float(data.get("filled", 0) or 0),
        )
        return Order(
            id=order_id,
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
        side_txt = str(data.get("side", "") or "").strip().lower()
        return Order(
            id=order_id,
            symbol=symbol,
            side=OrderSide.BUY if side_txt == "buy" else OrderSide.SELL,
            order_type=self._infer_bybit_order_type(data),
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
        orders: list[Order] = []
        for d in raw:
            side_txt = str(d.get("side", "") or "").strip().lower()
            raw_symbol = str(d.get("symbol", "") or "")
            norm_symbol = raw_symbol.split(":")[0] if ":" in raw_symbol else raw_symbol
            orders.append(
                Order(
                    id=self._extract_order_id(d),
                    symbol=norm_symbol,
                    side=OrderSide.BUY if side_txt == "buy" else OrderSide.SELL,
                    order_type=self._infer_bybit_order_type(d),
                    amount=float(d.get("amount", 0) or 0),
                    stop_price=parse_stop_price(d),
                    status=parse_order_status(d.get("status", "")),
                    filled=float(d.get("filled", 0) or 0),
                    market_type=market_type.value,
                )
            )
        return orders

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        cached = self._leverage_by_symbol.get(symbol)
        if cached == leverage:
            return True
        try:
            resolved_symbol = self._resolve_symbol(symbol, MarketType.FUTURES)
            await self._futures.set_leverage(leverage, resolved_symbol)
            self._leverage_by_symbol[symbol] = leverage
            logger.debug("Leverage set to {}x for {}", leverage, symbol)
            return True
        except Exception as e:
            msg = str(e).lower()
            if 'retcode":110043' in msg or "retcode 110043" in msg or "leverage not modified" in msg:
                # Bybit returns this when requested leverage already matches current.
                self._leverage_by_symbol[symbol] = leverage
                return True
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

    def supports_ticker_ws(self, symbol: str) -> bool:
        futures_symbol = self._resolve_symbol(symbol, MarketType.FUTURES)
        futures_markets = getattr(self._futures, "markets", {}) or {}
        spot_markets = getattr(self._spot, "markets", {}) or {}
        use_futures = futures_symbol in futures_markets and symbol not in spot_markets
        client = self._futures if use_futures else self._spot
        return bool(hasattr(client, "watch_ticker"))

    async def watch_ticker(self, symbol: str, callback: Callable[..., Any]) -> None:
        async def _loop() -> None:
            futures_symbol = self._resolve_symbol(symbol, MarketType.FUTURES)
            futures_markets = getattr(self._futures, "markets", {}) or {}
            spot_markets = getattr(self._spot, "markets", {}) or {}
            use_futures = futures_symbol in futures_markets and symbol not in spot_markets
            client = self._futures if use_futures else self._spot
            watch_symbol = futures_symbol if use_futures else symbol
            supports_ws = hasattr(client, "watch_ticker")
            if not supports_ws:
                logger.warning("Bybit WS ticker unavailable for {}, unsubscribing watcher", symbol)
                return
            while True:
                try:
                    data = await client.watch_ticker(watch_symbol)
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
                    msg = str(e).lower()
                    if "not supported yet" in msg:
                        logger.warning("Bybit WS ticker unsupported for {}, unsubscribing watcher", symbol)
                        break
                    logger.error("Ticker watch error for {}: {}", symbol, e)
                    await asyncio.sleep(5)

        task = asyncio.create_task(_loop())
        self._watchers.append(task)

    async def watch_candles(self, symbol: str, timeframe: str, callback: Callable[..., Any]) -> None:
        async def _loop() -> None:
            futures_symbol = self._resolve_symbol(symbol, MarketType.FUTURES)
            futures_markets = getattr(self._futures, "markets", {}) or {}
            spot_markets = getattr(self._spot, "markets", {}) or {}
            use_futures = futures_symbol in futures_markets and symbol not in spot_markets
            client = self._futures if use_futures else self._spot
            watch_symbol = futures_symbol if use_futures else symbol
            supports_ws = hasattr(client, "watch_ohlcv")
            while True:
                try:
                    if supports_ws:
                        data = await client.watch_ohlcv(watch_symbol, timeframe)
                    else:
                        data = await client.fetch_ohlcv(watch_symbol, timeframe, limit=2)
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
                    msg = str(e).lower()
                    if supports_ws and "not supported yet" in msg:
                        supports_ws = False
                        logger.warning("Bybit WS candles unsupported for {}, falling back to polling", symbol)
                        await asyncio.sleep(1)
                        continue
                    logger.error("Candle watch error for {}: {}", symbol, e)
                    await asyncio.sleep(5)

        task = asyncio.create_task(_loop())
        self._watchers.append(task)
