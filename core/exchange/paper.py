from __future__ import annotations

import math
import time
import uuid
from collections.abc import Callable
from typing import Any

from loguru import logger

from core.exchange.base import BaseExchange
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

_TICKER_CACHE_TTL = 0.8


class PaperExchange(BaseExchange):
    """Simulated exchange for paper trading. Wraps a real exchange for market data
    but executes orders locally."""

    def __init__(self, real_exchange: BaseExchange, starting_balance: float = 10000.0):
        super().__init__()
        self._real = real_exchange
        self._balances: dict[str, float] = {"USDT": starting_balance}
        self._orders: dict[str, Order] = {}
        self._positions: list[Position] = []
        self._leverage_map: dict[str, int] = {}
        self._ticker_cache: dict[str, tuple[float, Ticker]] = {}

    @property
    def name(self) -> str:
        return f"paper_{self._real.name}"

    async def connect(self) -> None:
        await self._real.connect()
        logger.info("Paper trading active with ${:.2f} USDT", self._balances["USDT"])

    async def disconnect(self) -> None:
        await self._real.disconnect()

    # -- Market Data (pass-through to real exchange) --

    @timed("exchange.fetch_ticker")
    async def fetch_ticker(self, symbol: str, market_type: MarketType = MarketType.SPOT) -> Ticker:
        cached = self._ticker_cache.get(symbol)
        if cached and (time.monotonic() - cached[0]) < _TICKER_CACHE_TTL:
            return cached[1]
        ticker = await self._real.fetch_ticker(symbol, market_type=market_type)
        self._ticker_cache[symbol] = (time.monotonic(), ticker)
        return ticker

    async def fetch_tickers(
        self, symbols: list[str] | None = None, market_type: MarketType = MarketType.SPOT
    ) -> list[Ticker]:
        return await self._real.fetch_tickers(symbols, market_type=market_type)

    @timed("exchange.fetch_candles")
    async def fetch_candles(
        self,
        symbol: str,
        timeframe: str = "1m",
        limit: int = 100,
        market_type: MarketType = MarketType.SPOT,
    ) -> list[Candle]:
        return await self._real.fetch_candles(symbol, timeframe, limit, market_type=market_type)

    async def fetch_order_book(
        self, symbol: str, limit: int = 20, market_type: MarketType = MarketType.SPOT
    ) -> OrderBook:
        return await self._real.fetch_order_book(symbol, limit, market_type=market_type)

    # -- Account --

    @timed("exchange.fetch_balance")
    async def fetch_balance(self) -> dict[str, float]:
        total = dict(self._balances)
        for pos in self._positions:
            margin = pos.entry_price * pos.amount / max(pos.leverage, 1)
            try:
                mt = MarketType.FUTURES if pos.market_type == "futures" else MarketType.SPOT
                ticker = await self.fetch_ticker(pos.symbol, market_type=mt)
                if pos.side == OrderSide.BUY:
                    pnl = (ticker.last - pos.entry_price) * pos.amount
                else:
                    pnl = (pos.entry_price - ticker.last) * pos.amount
            except Exception:
                pnl = pos.unrealized_pnl or 0
            total["USDT"] = total.get("USDT", 0) + margin + pnl
        # Always expose USDT (even if negative after a losing close) so the bot sees real balance
        result = {k: v for k, v in total.items() if k != "USDT" and v > 0}
        result["USDT"] = total.get("USDT", 0.0)
        return result

    @timed("exchange.fetch_positions")
    async def fetch_positions(self, symbol: str | None = None) -> list[Position]:
        targets = [p for p in self._positions if p.symbol == symbol] if symbol else list(self._positions)
        for pos in targets:
            try:
                mt = MarketType.FUTURES if pos.market_type == "futures" else MarketType.SPOT
                ticker = await self.fetch_ticker(pos.symbol, market_type=mt)
                pos.current_price = ticker.last
                if pos.entry_price > 0:
                    if pos.side == OrderSide.BUY:
                        pos.unrealized_pnl = (ticker.last - pos.entry_price) * pos.amount
                    else:
                        pos.unrealized_pnl = (pos.entry_price - ticker.last) * pos.amount
            except Exception:
                pass
        return targets

    # -- Trading (simulated) --

    @staticmethod
    def _parse_base_asset(symbol: str) -> str:
        """Extract the base asset from a trading pair (e.g. 'BTC/USDT' -> 'BTC')."""
        return symbol.split("/")[0] if "/" in symbol else symbol

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
        order_id = str(uuid.uuid4())[:8]

        if amount <= 0:
            logger.warning("[PAPER] Invalid amount {} for {}", amount, symbol)
            return Order(
                id=order_id,
                symbol=symbol,
                side=side,
                order_type=order_type,
                amount=amount,
                price=0,
                status=OrderStatus.FAILED,
                market_type=market_type.value,
            )

        try:
            ticker = await self._real.fetch_ticker(symbol)
            self._ticker_cache[symbol] = (time.monotonic(), ticker)
            fill_price = price if price and order_type != OrderType.MARKET else ticker.last
            if not fill_price or fill_price <= 0 or not math.isfinite(fill_price):
                logger.error("[PAPER] Invalid fill price {} for {}", fill_price, symbol)
                return Order(
                    id=order_id,
                    symbol=symbol,
                    side=side,
                    order_type=order_type,
                    amount=amount,
                    price=0,
                    status=OrderStatus.FAILED,
                    market_type=market_type.value,
                )
        except Exception as e:
            logger.error("[PAPER] Failed to fetch price for {}: {}", symbol, e)
            return Order(
                id=order_id,
                symbol=symbol,
                side=side,
                order_type=order_type,
                amount=amount,
                price=0,
                status=OrderStatus.FAILED,
                market_type=market_type.value,
            )

        if market_type == MarketType.SPOT:
            if side == OrderSide.BUY:
                cost = fill_price * amount
                if self._balances.get("USDT", 0) < cost:
                    logger.warning("[PAPER] Insufficient USDT for BUY {} {}", amount, symbol)
                    return Order(
                        id=order_id,
                        symbol=symbol,
                        side=side,
                        order_type=order_type,
                        amount=amount,
                        price=fill_price,
                        status=OrderStatus.FAILED,
                        market_type=market_type.value,
                    )
                self._balances["USDT"] -= cost
                base = self._parse_base_asset(symbol)
                self._balances[base] = self._balances.get(base, 0) + amount
            else:
                base = self._parse_base_asset(symbol)
                held = self._balances.get(base, 0)
                if held < amount:
                    logger.warning("[PAPER] Insufficient {} for SELL ({:.6f} held, {:.6f} needed)", base, held, amount)
                    return Order(
                        id=order_id,
                        symbol=symbol,
                        side=side,
                        order_type=order_type,
                        amount=amount,
                        price=fill_price,
                        status=OrderStatus.FAILED,
                        market_type=market_type.value,
                    )
                self._balances[base] -= amount
                self._balances["USDT"] += fill_price * amount

        order = Order(
            id=order_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            amount=amount,
            price=fill_price,
            status=OrderStatus.FILLED,
            filled=amount,
            average_price=fill_price,
            leverage=leverage,
            market_type=market_type.value,
        )
        self._orders[order_id] = order

        if market_type == MarketType.FUTURES and not self._update_position(order, fill_price):
            order.status = OrderStatus.FAILED
            return order

        logger.info(
            "[PAPER] {} {} {} {} @ {:.6f} (leverage={}x)",
            order.status.value.upper(),
            side.value.upper(),
            order.amount,
            symbol,
            fill_price,
            leverage,
        )
        return order

    def _update_position(self, order: Order, fill_price: float) -> bool:
        """Update futures positions and adjust margin.

        All futures balance changes (margin deduction/credit) happen here,
        not in place_order, so close orders don't incorrectly consume margin.

        Returns False if insufficient margin to open/add to a position.
        """
        if order.leverage <= 0:
            logger.error("[PAPER] Invalid leverage {} for {} — clamping to 1", order.leverage, order.symbol)
            order.leverage = 1
        # Match by symbol AND side: close opposite-side position first (hedge/main same symbol).
        to_close = next((p for p in self._positions if p.symbol == order.symbol and p.side != order.side), None)
        existing_same = next((p for p in self._positions if p.symbol == order.symbol and p.side == order.side), None)
        if to_close and to_close.leverage <= 0:
            to_close.leverage = 1
        if existing_same and existing_same.leverage <= 0:
            existing_same.leverage = 1

        if to_close:
            close_amount = min(order.amount, to_close.amount)
            if to_close.side == OrderSide.BUY:
                pnl = (fill_price - to_close.entry_price) * close_amount
            else:
                pnl = (to_close.entry_price - fill_price) * close_amount
            margin_returned = to_close.entry_price * close_amount / to_close.leverage
            self._balances["USDT"] += margin_returned + pnl

            if close_amount >= to_close.amount:
                self._positions.remove(to_close)
            else:
                to_close.amount -= close_amount

            logger.info("[PAPER] Closed {:.6f} of {} PnL: {:.2f}", close_amount, order.symbol, pnl)

            remainder = order.amount - close_amount
            if remainder > 0:
                margin_needed = fill_price * remainder / order.leverage
                if self._balances.get("USDT", 0) < margin_needed:
                    logger.warning(
                        "[PAPER] Insufficient margin for remainder {} {} — partial fill only", remainder, order.symbol
                    )
                    order.filled = close_amount
                    order.status = OrderStatus.PARTIALLY_FILLED
                    return True
                self._balances["USDT"] -= margin_needed
                self._positions.append(
                    Position(
                        symbol=order.symbol,
                        side=order.side,
                        amount=remainder,
                        entry_price=fill_price,
                        current_price=fill_price,
                        leverage=order.leverage,
                        market_type=order.market_type,
                    )
                )
            return True

        if existing_same:
            total_amount = existing_same.amount + order.amount
            if total_amount <= 0:
                logger.warning("[PAPER] DCA total amount is 0 for {}", order.symbol)
                return False
            margin_needed = fill_price * order.amount / order.leverage
            if self._balances.get("USDT", 0) < margin_needed:
                logger.warning("[PAPER] Insufficient margin for DCA {} {}", order.amount, order.symbol)
                return False
            self._balances["USDT"] -= margin_needed
            avg_entry = (existing_same.entry_price * existing_same.amount + fill_price * order.amount) / total_amount
            existing_same.amount = total_amount
            existing_same.entry_price = avg_entry
            existing_same.current_price = fill_price
            existing_same.leverage = max(existing_same.leverage, order.leverage)
            logger.info(
                "[PAPER] DCA into {} — avg entry: {:.6f}, total: {:.6f}",
                order.symbol,
                avg_entry,
                total_amount,
            )
            return True

        margin_needed = fill_price * order.amount / order.leverage
        if self._balances.get("USDT", 0) < margin_needed:
            logger.warning("[PAPER] Insufficient margin for {} {}", order.amount, order.symbol)
            return False
        self._balances["USDT"] -= margin_needed
        self._positions.append(
            Position(
                symbol=order.symbol,
                side=order.side,
                amount=order.amount,
                entry_price=fill_price,
                current_price=fill_price,
                leverage=order.leverage,
                market_type=order.market_type,
            )
        )
        return True

    async def cancel_order(self, order_id: str, symbol: str, market_type: MarketType = MarketType.SPOT) -> Order:
        if order_id in self._orders:
            self._orders[order_id].status = OrderStatus.CANCELLED
            return self._orders[order_id]
        return Order(
            id=order_id,
            symbol=symbol,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            amount=0,
            status=OrderStatus.CANCELLED,
            market_type=market_type.value,
        )

    async def fetch_order(self, order_id: str, symbol: str, market_type: MarketType = MarketType.SPOT) -> Order:
        if order_id in self._orders:
            return self._orders[order_id]
        return Order(
            id=order_id,
            symbol=symbol,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            amount=0,
            status=OrderStatus.FAILED,
            market_type=market_type.value,
        )

    async def fetch_open_orders(
        self, symbol: str | None = None, market_type: MarketType = MarketType.SPOT
    ) -> list[Order]:
        return [o for o in self._orders.values() if o.status == OrderStatus.OPEN and (not symbol or o.symbol == symbol)]

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        self._leverage_map[symbol] = leverage
        logger.debug("[PAPER] Leverage set to {}x for {}", leverage, symbol)
        return True

    async def set_margin_mode(self, symbol: str, margin_mode: str) -> bool:
        _ = symbol
        _ = margin_mode
        # Paper-local does not model margin mode, but keep interface parity.
        return True

    async def get_available_symbols(self, market_type: MarketType = MarketType.SPOT) -> list[str]:
        return await self._real.get_available_symbols(market_type)

    async def watch_ticker(self, symbol: str, callback: Callable[..., Any]) -> None:
        await self._real.watch_ticker(symbol, callback)

    async def watch_candles(self, symbol: str, timeframe: str, callback: Callable[..., Any]) -> None:
        await self._real.watch_candles(symbol, timeframe, callback)
