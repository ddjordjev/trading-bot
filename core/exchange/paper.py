from __future__ import annotations

import math
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
    async def fetch_ticker(self, symbol: str) -> Ticker:
        return await self._real.fetch_ticker(symbol)

    async def fetch_tickers(self, symbols: list[str] | None = None) -> list[Ticker]:
        return await self._real.fetch_tickers(symbols)

    @timed("exchange.fetch_candles")
    async def fetch_candles(self, symbol: str, timeframe: str = "1m", limit: int = 100) -> list[Candle]:
        return await self._real.fetch_candles(symbol, timeframe, limit)

    async def fetch_order_book(self, symbol: str, limit: int = 20) -> OrderBook:
        return await self._real.fetch_order_book(symbol, limit)

    # -- Account --

    @timed("exchange.fetch_balance")
    async def fetch_balance(self) -> dict[str, float]:
        return {k: v for k, v in self._balances.items() if v > 0}

    @timed("exchange.fetch_positions")
    async def fetch_positions(self, symbol: str | None = None) -> list[Position]:
        targets = [p for p in self._positions if p.symbol == symbol] if symbol else list(self._positions)
        for pos in targets:
            try:
                ticker = await self._real.fetch_ticker(pos.symbol)
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
            ticker = await self.fetch_ticker(symbol)
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
        existing = next((p for p in self._positions if p.symbol == order.symbol), None)
        if existing and existing.leverage <= 0:
            existing.leverage = 1

        if existing and existing.side != order.side:
            close_amount = min(order.amount, existing.amount)
            if existing.side == OrderSide.BUY:
                pnl = (fill_price - existing.entry_price) * close_amount
            else:
                pnl = (existing.entry_price - fill_price) * close_amount
            margin_returned = existing.entry_price * close_amount / existing.leverage
            self._balances["USDT"] += margin_returned + pnl

            if close_amount >= existing.amount:
                self._positions.remove(existing)
            else:
                existing.amount -= close_amount

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

        if existing and existing.side == order.side:
            total_amount = existing.amount + order.amount
            if total_amount <= 0:
                logger.warning("[PAPER] DCA total amount is 0 for {}", order.symbol)
                return False
            margin_needed = fill_price * order.amount / order.leverage
            if self._balances.get("USDT", 0) < margin_needed:
                logger.warning("[PAPER] Insufficient margin for DCA {} {}", order.amount, order.symbol)
                return False
            self._balances["USDT"] -= margin_needed
            avg_entry = (existing.entry_price * existing.amount + fill_price * order.amount) / total_amount
            existing.amount = total_amount
            existing.entry_price = avg_entry
            existing.current_price = fill_price
            existing.leverage = max(existing.leverage, order.leverage)
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

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        self._leverage_map[symbol] = leverage
        logger.debug("[PAPER] Leverage set to {}x for {}", leverage, symbol)

    async def get_available_symbols(self, market_type: MarketType = MarketType.SPOT) -> list[str]:
        return await self._real.get_available_symbols(market_type)

    async def watch_ticker(self, symbol: str, callback: Callable[..., Any]) -> None:
        await self._real.watch_ticker(symbol, callback)

    async def watch_candles(self, symbol: str, timeframe: str, callback: Callable[..., Any]) -> None:
        await self._real.watch_candles(symbol, timeframe, callback)
