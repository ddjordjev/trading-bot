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
        self._spot.options["warnOnFetchOpenOrdersWithoutSymbol"] = False
        self._futures.options["warnOnFetchOpenOrdersWithoutSymbol"] = False
        if sandbox:
            self._spot.set_sandbox_mode(True)
            self._futures.set_sandbox_mode(True)
            # Override futures URLs to Binance Demo Trading endpoint
            api_urls = self._futures.urls.get("api", {})
            if isinstance(api_urls, dict):
                for key, url in list(api_urls.items()):
                    if isinstance(url, str) and "testnet.binancefuture.com" in url:
                        api_urls[key] = url.replace("testnet.binancefuture.com", "demo-fapi.binance.com")

        self._watchers: list[asyncio.Task[None]] = []

    @property
    def name(self) -> str:
        return "binance"

    def _client(self, market_type: MarketType = MarketType.SPOT) -> ccxt.binance:
        return self._futures if market_type == MarketType.FUTURES else self._spot

    def _futures_market_id(self, symbol: str) -> str:
        try:
            market = self._futures.market(symbol)
            market_id = market.get("id")
            if isinstance(market_id, str) and market_id:
                return market_id
        except Exception:
            pass
        return symbol.replace("/", "").split(":")[0]

    def _symbol_from_futures_market_id(self, market_id: str) -> str:
        try:
            by_id = self._futures.markets_by_id.get(market_id)
            if isinstance(by_id, list) and by_id:
                maybe_symbol = by_id[0].get("symbol")
                if isinstance(maybe_symbol, str) and maybe_symbol:
                    return maybe_symbol.split(":")[0]
            if isinstance(by_id, dict):
                maybe_symbol = by_id.get("symbol")
                if isinstance(maybe_symbol, str) and maybe_symbol:
                    return maybe_symbol.split(":")[0]
        except Exception:
            pass
        if market_id.endswith("USDT") and len(market_id) > 4:
            return f"{market_id[:-4]}/USDT"
        return market_id

    @staticmethod
    def _parse_algo_status(raw_status: str) -> OrderStatus:
        status = str(raw_status or "").strip().upper()
        if status in {"NEW", "WORKING"}:
            return OrderStatus.OPEN
        if status in {"FILLED", "TRIGGERED", "FINISHED"}:
            return OrderStatus.FILLED
        if status in {"CANCELED", "CANCELLED", "EXPIRED"}:
            return OrderStatus.CANCELLED
        if status in {"REJECTED", "FAILED"}:
            return OrderStatus.FAILED
        return OrderStatus.PENDING

    def _to_algo_order(self, raw: dict[str, Any], symbol_hint: str, market_type: MarketType) -> Order:
        hinted_type = str(raw.get("orderType", "") or raw.get("type", ""))
        if not hinted_type:
            info = raw.get("info") or {}
            hinted_type = str(info.get("orderType", "") or info.get("origType", "") or info.get("type", ""))
        order_type = parse_order_type(hinted_type)
        symbol_raw = str(raw.get("symbol") or "")
        if symbol_raw:
            symbol = symbol_raw.split(":")[0] if "/" in symbol_raw else self._symbol_from_futures_market_id(symbol_raw)
        else:
            symbol = symbol_hint.split(":")[0]
        side_raw = str(raw.get("side", "")).lower()
        qty = raw.get("quantity", 0) or raw.get("origQty", 0) or 0
        return Order(
            id=str(raw.get("algoId", "") or raw.get("orderId", "") or ""),
            symbol=symbol,
            side=OrderSide.BUY if side_raw == "buy" else OrderSide.SELL,
            order_type=order_type,
            amount=float(qty or 0),
            stop_price=parse_stop_price(raw),
            status=self._parse_algo_status(str(raw.get("algoStatus", ""))),
            filled=0.0,
            average_price=float(raw.get("actualPrice", 0) or 0),
            market_type=market_type.value,
        )

    @staticmethod
    def _resolve_order_type(raw: dict[str, Any]) -> OrderType:
        parsed = parse_order_type(str(raw.get("type", "")))
        if parsed != OrderType.MARKET:
            return parsed
        info = raw.get("info") or {}
        hinted = str(info.get("origType", "") or info.get("orderType", "") or info.get("type", ""))
        if hinted:
            hinted_parsed = parse_order_type(hinted)
            if hinted_parsed != OrderType.MARKET:
                return hinted_parsed
        if parse_stop_price(raw) is not None:
            return OrderType.STOP_LOSS
        return parsed

    @staticmethod
    def _infer_position_leverage(raw_position: dict[str, Any]) -> int:
        """Infer leverage when the exchange payload omits explicit value."""
        raw_leverage = raw_position.get("leverage")
        try:
            if raw_leverage is not None:
                parsed = round(float(raw_leverage))
                if parsed > 0:
                    return parsed
        except (TypeError, ValueError):
            pass

        # Binance demo payload can include initialMarginPercentage (e.g. 0.3333 for 3x).
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

    def _normalize_amount(self, client: Any, symbol: str, amount: float) -> float:
        try:
            amount_to_precision = getattr(client, "amount_to_precision", None)
            if callable(amount_to_precision):
                normalized = amount_to_precision(symbol, amount)
                if isinstance(normalized, str | int | float):
                    return float(normalized)
                logger.debug(
                    "Ignoring non-numeric normalized amount for {} (amount={}, type={})",
                    symbol,
                    amount,
                    type(normalized).__name__,
                )
        except Exception as exc:
            logger.debug(
                "Failed to normalize amount for {} (amount={}): {}",
                symbol,
                amount,
                exc,
            )
        return amount

    def _cap_amount_to_market_limits(self, client: Any, symbol: str, amount: float) -> float:
        try:
            market_fn = getattr(client, "market", None)
            if not callable(market_fn):
                return amount
            market = market_fn(symbol) or {}
            limits = market.get("limits") or {}
            amount_limits = limits.get("amount") or {}
            max_amount = amount_limits.get("max")
            if max_amount is None:
                return amount
            max_amount_f = float(max_amount)
            if max_amount_f <= 0:
                return amount
            if amount > max_amount_f:
                logger.warning(
                    "Capping {} order amount for {} from {:.6f} to exchange max {:.6f}",
                    self.name,
                    symbol,
                    amount,
                    max_amount_f,
                )
                return max_amount_f
        except Exception as exc:
            logger.debug("Failed to apply market amount cap for {}: {}", symbol, exc)
        return amount

    @staticmethod
    def _is_max_quantity_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        return 'code":-4005' in msg or ("max quantity" in msg and "quantity" in msg)

    def _reduced_amount_attempts(self, client: Any, symbol: str, initial_amount: float) -> list[float]:
        attempts: list[float] = []
        amount = float(initial_amount)
        for _ in range(6):
            amount *= 0.5
            if amount <= 0:
                break
            reduced = self._normalize_amount(client, symbol, amount)
            reduced = self._cap_amount_to_market_limits(client, symbol, reduced)
            if reduced <= 0:
                continue
            if attempts and abs(attempts[-1] - reduced) < 1e-12:
                continue
            attempts.append(reduced)
        return attempts

    def _normalize_price(self, client: Any, symbol: str, value: float | None) -> float | None:
        if value is None:
            return None
        try:
            price_to_precision = getattr(client, "price_to_precision", None)
            if callable(price_to_precision):
                normalized = price_to_precision(symbol, value)
                if isinstance(normalized, str | int | float):
                    return float(normalized)
                logger.debug(
                    "Ignoring non-numeric normalized price for {} (price={}, type={})",
                    symbol,
                    value,
                    type(normalized).__name__,
                )
        except Exception as exc:
            logger.debug(
                "Failed to normalize price for {} (price={}): {}",
                symbol,
                value,
                exc,
            )
        return value

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
    async def fetch_ticker(self, symbol: str, market_type: MarketType = MarketType.SPOT) -> Ticker:
        data = await self._client(market_type).fetch_ticker(symbol)
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
        raw = await self._client(market_type).fetch_tickers(symbols)
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
    async def fetch_candles(
        self,
        symbol: str,
        timeframe: str = "1m",
        limit: int = 100,
        market_type: MarketType = MarketType.SPOT,
    ) -> list[Candle]:
        data = await self._client(market_type).fetch_ohlcv(symbol, timeframe, limit=limit)
        return [Candle(timestamp=ts_to_dt(c[0]), open=c[1], high=c[2], low=c[3], close=c[4], volume=c[5]) for c in data]

    async def fetch_order_book(
        self, symbol: str, limit: int = 20, market_type: MarketType = MarketType.SPOT
    ) -> OrderBook:
        data = await self._client(market_type).fetch_order_book(symbol, limit)
        return OrderBook(
            symbol=symbol,
            bids=[(b[0], b[1]) for b in data.get("bids", [])],
            asks=[(a[0], a[1]) for a in data.get("asks", [])],
            timestamp=ts_to_dt(data.get("timestamp")),
        )

    @timed("exchange.fetch_balance")
    async def fetch_balance(self) -> dict[str, float]:
        result: dict[str, float] = {}
        try:
            data = await self._spot.fetch_balance()
            for asset, info in data.items():
                if isinstance(info, dict) and info.get("free", 0) > 0:
                    result[asset] = float(info["free"])
        except Exception:
            if self.sandbox:
                logger.debug("Spot balance fetch failed in sandbox mode — skipping")
            else:
                raise
        try:
            futures_data = await self._futures.fetch_balance()
            for asset, info in futures_data.items():
                if isinstance(info, dict) and info.get("free", 0) > 0:
                    result[asset] = result.get(asset, 0) + float(info["free"])
        except Exception:
            if self.sandbox:
                logger.debug("Futures balance fetch failed in sandbox mode — skipping")
            else:
                raise
        return {k: v for k, v in result.items() if v > 0}

    @timed("exchange.fetch_positions")
    async def fetch_positions(self, symbol: str | None = None) -> list[Position]:
        try:
            raw = await self._futures.fetch_positions(symbols=[symbol] if symbol else None)
        except Exception as e:
            logger.warning("Binance fetch_positions failed: {}", e)
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
        normalized_amount = self._normalize_amount(client, symbol, amount)
        normalized_amount = self._cap_amount_to_market_limits(client, symbol, normalized_amount)
        normalized_price = self._normalize_price(client, symbol, price)
        normalized_stop_price = self._normalize_price(client, symbol, stop_price)
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
        if normalized_stop_price is not None:
            params["stopPrice"] = normalized_stop_price
        is_protection_order = order_type in (OrderType.STOP_LOSS, OrderType.TAKE_PROFIT)
        if market_type == MarketType.FUTURES and is_protection_order:
            market_id = self._futures_market_id(symbol)
            algo_type = "STOP_MARKET" if order_type == OrderType.STOP_LOSS else "TAKE_PROFIT_MARKET"
            payload: dict[str, Any] = {
                "symbol": market_id,
                "side": side.value.upper(),
                "algoType": "CONDITIONAL",
                "type": algo_type,
                "quantity": normalized_amount,
                "triggerPrice": normalized_stop_price,
                "timeInForce": "GTC",
                "reduceOnly": "true",
                "closePosition": "false",
                "workingType": "CONTRACT_PRICE",
            }
            logger.info(
                "Placing {} {} {} {} @ {} (leverage={}, stop_price={}, params={})",
                market_type.value,
                side.value,
                ccxt_type,
                symbol,
                normalized_price or "market",
                leverage,
                normalized_stop_price if normalized_stop_price is not None else "none",
                payload,
            )
            try:
                data = await self._futures.fapiPrivatePostAlgoOrder(payload)
            except Exception as exc:
                if not self._is_max_quantity_error(exc):
                    raise
                last_exc = exc
                for retry_amount in self._reduced_amount_attempts(self._futures, symbol, normalized_amount):
                    logger.warning(
                        "Retrying {} protection order on {} with reduced quantity {:.6f} after max-quantity rejection",
                        order_type.value,
                        symbol,
                        retry_amount,
                    )
                    retry_payload = dict(payload)
                    retry_payload["quantity"] = retry_amount
                    try:
                        data = await self._futures.fapiPrivatePostAlgoOrder(retry_payload)
                        normalized_amount = retry_amount
                        break
                    except Exception as retry_exc:
                        last_exc = retry_exc
                else:
                    raise last_exc
            order = self._to_algo_order(data, symbol, market_type)
            logger.info(
                "Order accepted {} {} {} id={} status={} trigger={}",
                market_type.value,
                ccxt_type,
                symbol,
                order.id,
                order.status.value,
                order.stop_price if order.stop_price is not None else "none",
            )
            return order
        if market_type == MarketType.FUTURES and is_protection_order:
            params["reduceOnly"] = True
            # Avoid over-closing when account mode supports one-way futures.
            params["closePosition"] = False
        # Protection orders should never be blocked by leverage endpoint issues.
        if (
            market_type == MarketType.FUTURES
            and not is_protection_order
            and not await self.set_leverage(symbol, leverage)
        ):
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
            normalized_price or "market",
            leverage,
            normalized_stop_price if normalized_stop_price is not None else "none",
            params,
        )

        try:
            data = await client.create_order(
                symbol=symbol,
                type=ccxt_type,
                side=side.value,
                amount=normalized_amount,
                price=normalized_price,
                params=params,
            )
        except Exception as exc:
            if not (market_type == MarketType.FUTURES and self._is_max_quantity_error(exc)):
                raise
            last_exc = exc
            for retry_amount in self._reduced_amount_attempts(client, symbol, normalized_amount):
                logger.warning(
                    "Retrying {} order on {} with reduced quantity {:.6f} after max-quantity rejection",
                    ccxt_type,
                    symbol,
                    retry_amount,
                )
                try:
                    data = await client.create_order(
                        symbol=symbol,
                        type=ccxt_type,
                        side=side.value,
                        amount=retry_amount,
                        price=normalized_price,
                        params=params,
                    )
                    normalized_amount = retry_amount
                    break
                except Exception as retry_exc:
                    last_exc = retry_exc
            else:
                raise last_exc
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
            amount=normalized_amount,
            price=normalized_price,
            stop_price=normalized_stop_price,
            status=parse_order_status(data.get("status", "open")),
            filled=float(data.get("filled", 0) or 0),
            average_price=float(data.get("average", 0) or 0),
            leverage=leverage,
            market_type=market_type.value,
        )

    async def cancel_order(self, order_id: str, symbol: str, market_type: MarketType = MarketType.SPOT) -> Order:
        client = self._client(market_type)
        try:
            data = await client.cancel_order(order_id, symbol)
        except Exception as err:
            if market_type != MarketType.FUTURES:
                raise
            market_id = self._futures_market_id(symbol)
            try:
                data = await self._futures.fapiPrivateDeleteAlgoOrder({"symbol": market_id, "algoId": order_id})
            except Exception as algo_err:
                raise err from algo_err
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
        if market_type == MarketType.FUTURES:
            try:
                data = await client.fetch_order(order_id, symbol, params={"stop": True})
                return Order(
                    id=order_id,
                    symbol=symbol,
                    side=OrderSide.BUY if data.get("side") == "buy" else OrderSide.SELL,
                    order_type=self._resolve_order_type(data),
                    amount=float(data.get("amount", 0) or 0),
                    stop_price=parse_stop_price(data),
                    status=parse_order_status(str(data.get("status", "")).lower()),
                    filled=float(data.get("filled", 0) or 0),
                    average_price=float(data.get("average", 0) or 0),
                    market_type=market_type.value,
                )
            except Exception as stop_err:
                market_id = self._futures_market_id(symbol)
                try:
                    data = await self._futures.fapiPrivateGetAlgoOrder({"symbol": market_id, "algoId": order_id})
                    return self._to_algo_order(data, symbol, market_type)
                except Exception as algo_err:
                    raise stop_err from algo_err

        data = await client.fetch_order(order_id, symbol)
        return Order(
            id=order_id,
            symbol=symbol,
            side=OrderSide.BUY if data.get("side") == "buy" else OrderSide.SELL,
            order_type=parse_order_type(str(data.get("type", ""))),
            amount=float(data.get("amount", 0) or 0),
            stop_price=parse_stop_price(data),
            status=parse_order_status(str(data.get("status", "")).lower()),
            filled=float(data.get("filled", 0) or 0),
            average_price=float(data.get("average", 0) or 0),
            market_type=market_type.value,
        )

    async def fetch_open_orders(
        self, symbol: str | None = None, market_type: MarketType = MarketType.SPOT
    ) -> list[Order]:
        client = self._client(market_type)
        raw = await client.fetch_open_orders(symbol)
        orders = [
            Order(
                id=str(d.get("id", "")),
                symbol=str(d.get("symbol", "")).split(":")[0],
                side=OrderSide.BUY if d.get("side") == "buy" else OrderSide.SELL,
                order_type=self._resolve_order_type(d),
                amount=float(d.get("amount", 0) or 0),
                stop_price=parse_stop_price(d),
                status=parse_order_status(str(d.get("status", "")).lower()),
                filled=float(d.get("filled", 0) or 0),
                market_type=market_type.value,
            )
            for d in raw
        ]
        if market_type != MarketType.FUTURES:
            return orders
        try:
            stop_raw = await client.fetch_open_orders(symbol, params={"stop": True})
        except Exception:
            stop_raw = []
        for entry in stop_raw:
            orders.append(
                Order(
                    id=str(entry.get("id", "")),
                    symbol=str(entry.get("symbol", "")).split(":")[0],
                    side=OrderSide.BUY if entry.get("side") == "buy" else OrderSide.SELL,
                    order_type=self._resolve_order_type(entry),
                    amount=float(entry.get("amount", 0) or 0),
                    stop_price=parse_stop_price(entry),
                    status=parse_order_status(str(entry.get("status", "")).lower()),
                    filled=float(entry.get("filled", 0) or 0),
                    market_type=market_type.value,
                )
            )
        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = self._futures_market_id(symbol)
        try:
            algo_raw = await self._futures.fapiPrivateGetOpenAlgoOrders(params)
        except Exception:
            algo_raw = []
        for entry in algo_raw:
            orders.append(self._to_algo_order(entry, symbol or "", market_type))
        deduped_by_id: dict[str, Order] = {}
        for order in orders:
            order_id = str(order.id or "")
            if not order_id:
                # Keep id-less rows as-is; they are rare and cannot be deduped safely.
                deduped_by_id[f"__idx__{len(deduped_by_id)}"] = order
                continue
            existing = deduped_by_id.get(order_id)
            if existing is None:
                deduped_by_id[order_id] = order
                continue
            existing_is_market = existing.order_type == OrderType.MARKET
            new_is_market = order.order_type == OrderType.MARKET
            existing_has_stop = float(existing.stop_price or 0.0) > 0
            new_has_stop = float(order.stop_price or 0.0) > 0
            # Prefer the richer protection representation when IDs collide.
            if (existing_is_market and not new_is_market) or (not existing_has_stop and new_has_stop):
                deduped_by_id[order_id] = order
        return list(deduped_by_id.values())

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        try:
            await self._futures.set_leverage(leverage, symbol)
            logger.debug("Leverage set to {}x for {}", leverage, symbol)
            return True
        except Exception as e:
            logger.warning("Could not set leverage for {}: {}", symbol, e)
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
