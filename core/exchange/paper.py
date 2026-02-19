from __future__ import annotations

import uuid
from collections.abc import Callable

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

    async def fetch_ticker(self, symbol: str) -> Ticker:
        return await self._real.fetch_ticker(symbol)

    async def fetch_tickers(self, symbols: list[str] | None = None) -> list[Ticker]:
        return await self._real.fetch_tickers(symbols)

    async def fetch_candles(self, symbol: str, timeframe: str = "1m", limit: int = 100) -> list[Candle]:
        return await self._real.fetch_candles(symbol, timeframe, limit)

    async def fetch_order_book(self, symbol: str, limit: int = 20) -> OrderBook:
        return await self._real.fetch_order_book(symbol, limit)

    # -- Account --

    async def fetch_balance(self) -> dict[str, float]:
        return {k: v for k, v in self._balances.items() if v > 0}

    async def fetch_positions(self, symbol: str | None = None) -> list[Position]:
        targets = [p for p in self._positions if p.symbol == symbol] if symbol else list(self._positions)
        for pos in targets:
            try:
                ticker = await self._real.fetch_ticker(pos.symbol)
                pos.current_price = ticker.last
                if pos.entry_price > 0:
                    if pos.side == OrderSide.BUY:
                        pos.unrealized_pnl = (ticker.last - pos.entry_price) * pos.amount * pos.leverage
                    else:
                        pos.unrealized_pnl = (pos.entry_price - ticker.last) * pos.amount * pos.leverage
            except Exception:
                pass
        return targets

    # -- Trading (simulated) --

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
        ticker = await self.fetch_ticker(symbol)
        fill_price = price if price and order_type != OrderType.MARKET else ticker.last

        order_id = str(uuid.uuid4())[:8]
        cost = fill_price * amount

        if market_type == MarketType.FUTURES:
            cost = cost / leverage

        if side == OrderSide.BUY:
            if self._balances.get("USDT", 0) < cost:
                logger.warning("[PAPER] Insufficient balance for {} {} {}", side.value, amount, symbol)
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

        if market_type == MarketType.FUTURES:
            self._update_position(order, fill_price)
        elif side == OrderSide.SELL:
            self._balances["USDT"] += fill_price * amount

        logger.info(
            "[PAPER] {} {} {} {} @ {:.6f} (leverage={}x)",
            "FILLED",
            side.value.upper(),
            amount,
            symbol,
            fill_price,
            leverage,
        )
        return order

    def _update_position(self, order: Order, fill_price: float) -> None:
        existing = next((p for p in self._positions if p.symbol == order.symbol), None)

        if existing and existing.side != order.side:
            pnl = (fill_price - existing.entry_price) * existing.amount
            if existing.side == OrderSide.SELL:
                pnl = -pnl
            pnl *= existing.leverage
            self._balances["USDT"] += (existing.entry_price * existing.amount / existing.leverage) + pnl
            self._positions.remove(existing)
            logger.info("[PAPER] Closed position {} PnL: {:.2f}", order.symbol, pnl)
            return

        if existing and existing.side == order.side:
            total_amount = existing.amount + order.amount
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
            return

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

    async def watch_ticker(self, symbol: str, callback: Callable) -> None:  # type: ignore[type-arg]
        await self._real.watch_ticker(symbol, callback)

    async def watch_candles(self, symbol: str, timeframe: str, callback: Callable) -> None:  # type: ignore[type-arg]
        await self._real.watch_candles(symbol, timeframe, callback)
