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
from core.orders.scaler import PositionScaler, ScalePhase


class OrderManager:
    """Translates signals into orders, manages open positions, and enforces stops.

    Key MEXC-specific behaviors:
    - Stop-losses on low-liq coins are unreliable (wick-through liquidation).
      For those, we manage stops ourselves via polling + market close, never
      trusting exchange SL orders.
    - ALWAYS start small and add to winners (scaled entries).
    - Once at +5% profit, lock stop to break-even.
    """

    def __init__(self, exchange: BaseExchange, risk: RiskManager, settings: Settings):
        self.exchange = exchange
        self.risk = risk
        self.settings = settings
        self.trailing = TrailingStopManager(
            default_initial_pct=settings.stop_loss_pct,
            default_trail_pct=max(0.5, settings.stop_loss_pct * 0.4),
            breakeven_pct=settings.breakeven_lock_pct,
        )
        self.scaler = PositionScaler(gambling_budget_pct=settings.gambling_budget_pct)
        self._active_orders: list[Order] = []
        self._trade_log: list[dict] = []

    async def execute_signal(self, signal: Signal, low_liquidity: bool = False) -> Order | None:
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

        return await self._open_position(signal, balance, low_liquidity)

    async def _open_position(self, signal: Signal, balance: float,
                             low_liquidity: bool = False) -> Order | None:
        price = signal.suggested_price or 0
        leverage = signal.leverage or self.settings.default_leverage
        market_type = MarketType(signal.market_type) if signal.market_type else MarketType.SPOT

        # Calculate full intended size
        full_amount = self.risk.calculate_position_size(
            balance, price, leverage if market_type == MarketType.FUTURES else 1,
        )
        if full_amount <= 0:
            logger.warning("Calculated position size is 0, skipping")
            return None

        side = OrderSide.BUY if signal.action == SignalAction.BUY else OrderSide.SELL
        side_str = "long" if side == OrderSide.BUY else "short"

        # Create scaled position tracker
        if low_liquidity:
            # Gambling bet: tiny initial, 1 add max
            amount = self.scaler.gambling_size(balance, price, leverage)
            sp = self.scaler.create(
                symbol=signal.symbol, side=side_str,
                intended_size=amount * 2,  # allow one add
                strategy=signal.strategy, market_type=signal.market_type or "futures",
                leverage=leverage, low_liquidity=True,
            )
            logger.info("LOW-LIQ gambling bet on {} | size: {:.6f} (pocket money)", signal.symbol, amount)
        else:
            # Normal: start with initial_pct of intended size
            sp = self.scaler.create(
                symbol=signal.symbol, side=side_str,
                intended_size=full_amount, strategy=signal.strategy,
                market_type=signal.market_type or "futures", leverage=leverage,
            )
            amount = sp.get_initial_amount()
            logger.info("Scaled entry on {} | initial: {:.6f} / {:.6f} ({:.0f}%)",
                        signal.symbol, amount, full_amount, sp.initial_pct * 100)

        if amount <= 0:
            return None

        order = await self.exchange.place_order(
            symbol=signal.symbol,
            side=side,
            order_type=OrderType.MARKET,
            amount=amount,
            leverage=leverage,
            market_type=market_type,
        )

        if order.status == OrderStatus.FILLED:
            sp.record_add(order.filled, order.average_price)
            self._log_trade(signal, order, "open")
            logger.info("Opened {} {} {} @ {:.6f} (phase: {})",
                        signal.market_type, side.value, signal.symbol,
                        order.average_price, sp.phase.value)

            pos = Position(
                symbol=signal.symbol, side=side, amount=amount,
                entry_price=order.average_price, current_price=order.average_price,
                leverage=leverage, market_type=market_type.value,
            )
            self.trailing.register(pos, low_liquidity=low_liquidity)

        self._active_orders.append(order)
        return order

    async def try_scale_in(self) -> list[Order]:
        """Check all scaled positions and add to winners.

        Called every tick. Only adds if:
        - Price is in profit beyond min_profit_to_add_pct
        - Not pulling back too hard
        - Position isn't already at max size
        """
        added: list[Order] = []
        positions = await self.exchange.fetch_positions()

        prices = {p.symbol: p.current_price for p in positions}
        to_add = self.scaler.get_symbols_to_add(prices)

        for symbol, amount in to_add:
            sp = self.scaler.get(symbol)
            if not sp:
                continue

            pos = next((p for p in positions if p.symbol == symbol), None)
            if not pos:
                continue

            side = OrderSide.BUY if sp.side == "long" else OrderSide.SELL
            market_type = MarketType(sp.market_type) if sp.market_type else MarketType.FUTURES

            logger.info("SCALING IN to {} | add #{} | amount: {:.6f} | profit: {:.2f}%",
                        symbol, sp.adds + 1, amount, pos.pnl_pct)

            order = await self.exchange.place_order(
                symbol=symbol,
                side=side,
                order_type=OrderType.MARKET,
                amount=amount,
                leverage=sp.leverage,
                market_type=market_type,
            )

            if order.status == OrderStatus.FILLED:
                sp.record_add(order.filled, order.average_price)
                self._log_trade(
                    Signal(symbol=symbol, action=SignalAction.BUY if side == OrderSide.BUY else SignalAction.SELL,
                           strategy=sp.strategy, reason=f"scale_in_#{sp.adds}",
                           market_type=sp.market_type),
                    order, "scale_in",
                )

                # Update trailing stop with new average entry
                ts = self.trailing.get(symbol)
                if ts:
                    ts.entry_price = sp.avg_entry_price
                    logger.info("Updated trail entry for {} to avg: {:.6f}", symbol, sp.avg_entry_price)

                added.append(order)

        return added

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
            self.scaler.remove(signal.symbol)

        return order

    async def check_stops(self) -> list[Order]:
        """Check trailing stops and liquidation risk for all positions.

        For low-liquidity coins, we NEVER trust exchange stop orders.
        We poll prices and close via market order ourselves.
        """
        closed: list[Order] = []
        positions = await self.exchange.fetch_positions()
        balance_map = await self.exchange.fetch_balance()
        balance = balance_map.get("USDT", 0.0)

        stopped_symbols = self.trailing.update_all(positions)
        for symbol in stopped_symbols:
            ts = self.trailing.get(symbol)
            pnl_info = f" (locked PnL ~{ts.pnl_from_stop:+.1f}%)" if ts else ""
            liq_tag = " [LOW-LIQ self-managed]" if ts and ts.low_liquidity else ""

            if ts and ts.activated:
                reason = "trailing_stop"
            elif ts and ts.breakeven_locked:
                reason = "breakeven_stop"
            else:
                reason = "initial_stop"

            logger.info("Stop triggered for {}{}{} (reason: {})",
                        symbol, pnl_info, liq_tag, reason)

            signal = Signal(
                symbol=symbol, action=SignalAction.CLOSE,
                strategy="trailing_stop", reason=reason,
                market_type="futures",
            )
            order = await self.execute_signal(signal)
            if order:
                closed.append(order)
                self.trailing.remove(symbol)
                self.scaler.remove(symbol)

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
                    self.scaler.remove(pos.symbol)

        return closed

    async def close_expired_quick_trades(self, active_signals: list[Signal]) -> list[Order]:
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
                    self.scaler.remove(signal.symbol)
                    logger.info("Auto-closed quick trade {} after {:.0f}m", signal.symbol, elapsed)

        return closed

    def _log_trade(self, signal: Signal, order: Order, action: str, pnl: float = 0.0) -> None:
        sp = self.scaler.get(signal.symbol)
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
            "scale_phase": sp.phase.value if sp else "n/a",
            "scale_fill_pct": sp.fill_pct if sp else 0,
        })

    @property
    def trade_history(self) -> list[dict]:
        return list(self._trade_log)
