from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger

from config.settings import Settings
from core.exchange.base import BaseExchange
from core.models import (
    Signal, SignalAction, Order, OrderSide, OrderType, OrderStatus,
    Position, MarketType,
)
from core.risk.manager import RiskManager
from core.orders.trailing import TrailingStopManager


class OrderManager:
    """Translates signals into orders, manages open positions, and enforces stops."""

    def __init__(self, exchange: BaseExchange, risk: RiskManager, settings: Settings):
        self.exchange = exchange
        self.risk = risk
        self.settings = settings
        self.trailing = TrailingStopManager(
            default_initial_pct=settings.stop_loss_pct,
            default_trail_pct=max(0.5, settings.stop_loss_pct * 0.4),
        )
        self._active_orders: list[Order] = []
        self._trade_log: list[dict] = []

    async def execute_signal(self, signal: Signal) -> Order | None:
        """Execute a trading signal if it passes risk checks."""
        balance_map = await self.exchange.fetch_balance()
        balance = balance_map.get("USDT", 0.0)
        positions = await self.exchange.fetch_positions()

        if not self.risk.check_signal(signal, balance, positions):
            return None

        signal = self.risk.apply_stops(signal)

        if signal.action == SignalAction.CLOSE:
            return await self._close_position(signal)

        if signal.action == SignalAction.HOLD:
            return None

        return await self._open_position(signal, balance)

    async def _open_position(self, signal: Signal, balance: float) -> Order | None:
        price = signal.suggested_price or 0
        leverage = signal.leverage or self.settings.default_leverage
        market_type = MarketType(signal.market_type) if signal.market_type else MarketType.SPOT

        amount = self.risk.calculate_position_size(
            balance, price, leverage if market_type == MarketType.FUTURES else 1,
        )
        if amount <= 0:
            logger.warning("Calculated position size is 0, skipping")
            return None

        side = OrderSide.BUY if signal.action == SignalAction.BUY else OrderSide.SELL

        order = await self.exchange.place_order(
            symbol=signal.symbol,
            side=side,
            order_type=OrderType.MARKET,
            amount=amount,
            leverage=leverage,
            market_type=market_type,
        )

        if order.status == OrderStatus.FILLED:
            self._log_trade(signal, order, "open")
            logger.info("Opened {} {} {} @ {:.6f}",
                        signal.market_type, side.value, signal.symbol, order.average_price)

            pos = Position(
                symbol=signal.symbol, side=side, amount=amount,
                entry_price=order.average_price, current_price=order.average_price,
                leverage=leverage, market_type=market_type.value,
            )
            self.trailing.register(pos)

        self._active_orders.append(order)
        return order

    async def _close_position(self, signal: Signal) -> Order | None:
        positions = await self.exchange.fetch_positions(signal.symbol)
        if not positions:
            logger.info("No position to close for {}", signal.symbol)
            return None

        pos = positions[0]
        close_side = OrderSide.SELL if pos.side == OrderSide.BUY else OrderSide.BUY
        market_type = MarketType(pos.market_type) if pos.market_type else MarketType.SPOT

        order = await self.exchange.place_order(
            symbol=signal.symbol,
            side=close_side,
            order_type=OrderType.MARKET,
            amount=pos.amount,
            leverage=pos.leverage,
            market_type=market_type,
        )

        if order.status == OrderStatus.FILLED:
            pnl = pos.unrealized_pnl
            self.risk.record_pnl(pnl)
            self._log_trade(signal, order, "close", pnl)
            logger.info("Closed {} {} PnL: {:.2f}", signal.symbol, pos.side.value, pnl)

        return order

    async def check_stops(self) -> list[Order]:
        """Check trailing stops and liquidation risk for all positions."""
        closed: list[Order] = []
        positions = await self.exchange.fetch_positions()
        balance_map = await self.exchange.fetch_balance()
        balance = balance_map.get("USDT", 0.0)

        # Trailing stops
        stopped_symbols = self.trailing.update_all(positions)
        for symbol in stopped_symbols:
            ts = self.trailing.get(symbol)
            pnl_info = f" (locked PnL ~{ts.pnl_from_stop:+.1f}%)" if ts else ""
            reason = "trailing_stop" if ts and ts.activated else "initial_stop"
            logger.info("Trailing stop triggered for {}{}", symbol, pnl_info)

            signal = Signal(
                symbol=symbol, action=SignalAction.CLOSE,
                strategy="trailing_stop", reason=reason,
                market_type="futures",
            )
            order = await self.execute_signal(signal)
            if order:
                closed.append(order)
                self.trailing.remove(symbol)

        # Liquidation risk
        for pos in positions:
            if pos.symbol in stopped_symbols:
                continue
            if self.risk.check_liquidation(pos, balance):
                logger.critical("LIQUIDATION RISK for {} - closing immediately!", pos.symbol)
                signal = Signal(
                    symbol=pos.symbol, action=SignalAction.CLOSE,
                    strategy="risk_manager", reason="liquidation_risk",
                    market_type=pos.market_type,
                )
                order = await self.execute_signal(signal)
                if order:
                    closed.append(order)
                    self.trailing.remove(pos.symbol)

        return closed

    async def close_expired_quick_trades(self, active_signals: list[Signal]) -> list[Order]:
        """Auto-close positions from quick trades that exceeded max hold time."""
        closed: list[Order] = []
        now = datetime.now(timezone.utc)

        for signal in active_signals:
            if not signal.quick_trade or not signal.max_hold_minutes:
                continue
            elapsed = (now - signal.timestamp).total_seconds() / 60
            if elapsed >= signal.max_hold_minutes:
                close_signal = Signal(
                    symbol=signal.symbol, action=SignalAction.CLOSE,
                    strategy=signal.strategy, reason="max_hold_time_exceeded",
                    market_type=signal.market_type,
                )
                order = await self.execute_signal(close_signal)
                if order:
                    closed.append(order)
                    logger.info("Auto-closed quick trade {} after {:.0f}m", signal.symbol, elapsed)

        return closed

    def _log_trade(self, signal: Signal, order: Order, action: str, pnl: float = 0.0) -> None:
        self._trade_log.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": signal.symbol,
            "action": action,
            "side": order.side.value,
            "amount": order.filled,
            "price": order.average_price,
            "strategy": signal.strategy,
            "reason": signal.reason,
            "pnl": pnl,
        })

    @property
    def trade_history(self) -> list[dict]:
        return list(self._trade_log)
