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
from core.orders.scaler import PositionScaler, ScalePhase, ScaleMode
from core.orders.hedge import HedgeManager, HedgeState


class OrderManager:
    """Translates signals into orders, manages open positions, and enforces stops.

    Scaling modes: WINNERS (add to winners) / PYRAMID (DCA down, lever up)
    Hedging: when a profitable position shows reversal signs, open a small
             counter-position to exploit the pullback while protecting the main.
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
        self.scaler = PositionScaler(
            initial_risk_amount=settings.initial_risk_amount,
            max_notional=settings.max_notional_position,
            gambling_budget_pct=settings.gambling_budget_pct,
        )
        self.hedger = HedgeManager(
            hedge_ratio=settings.hedge_ratio,
            min_main_profit_pct=settings.hedge_min_profit_pct,
            hedge_stop_pct=settings.hedge_stop_pct,
            max_hedges=settings.max_hedges,
        )
        self._active_orders: list[Order] = []
        self._trade_log: list[dict] = []

    # ------------------------------------------------------------------ #
    #  Signal execution
    # ------------------------------------------------------------------ #

    async def execute_signal(self, signal: Signal, low_liquidity: bool = False,
                             pyramid: bool = False) -> Order | None:
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

        return await self._open_position(signal, balance, low_liquidity, pyramid)

    async def _open_position(self, signal: Signal, balance: float,
                             low_liquidity: bool = False,
                             pyramid: bool = False) -> Order | None:
        price = signal.suggested_price or 0
        if price <= 0:
            logger.warning("No price for {}, skipping", signal.symbol)
            return None

        target_leverage = signal.leverage or self.settings.default_leverage
        market_type = MarketType(signal.market_type) if signal.market_type else MarketType.SPOT

        side = OrderSide.BUY if signal.action == SignalAction.BUY else OrderSide.SELL
        side_str = "long" if side == OrderSide.BUY else "short"

        if low_liquidity:
            amount = self.scaler.gambling_size(balance, price, target_leverage)
            sp = self.scaler.create(
                symbol=signal.symbol, side=side_str,
                strategy=signal.strategy,
                market_type=signal.market_type or "futures",
                leverage=target_leverage, low_liquidity=True,
            )
            actual_leverage = target_leverage
            logger.info("LOW-LIQ gambling bet on {} | ${:.0f} | size: {:.6f}",
                        signal.symbol, amount * price, amount)

        elif pyramid:
            sp = self.scaler.create(
                symbol=signal.symbol, side=side_str,
                strategy=signal.strategy,
                market_type=signal.market_type or "futures",
                leverage=target_leverage, mode=ScaleMode.PYRAMID,
            )
            amount = sp.get_initial_amount(price)
            actual_leverage = sp.initial_leverage
            logger.info("PYRAMID entry on {} | ${:.0f} initial (cap: ${:.0f}K) | lev: {}x (target: {}x)",
                        signal.symbol, amount * price, sp.max_notional / 1000,
                        actual_leverage, target_leverage)
        else:
            sp = self.scaler.create(
                symbol=signal.symbol, side=side_str,
                strategy=signal.strategy,
                market_type=signal.market_type or "futures",
                leverage=target_leverage,
            )
            amount = sp.get_initial_amount(price)
            actual_leverage = target_leverage
            logger.info("Scaled entry on {} | ${:.0f} initial (cap: ${:.0f}K notional)",
                        signal.symbol, amount * price, sp.max_notional / 1000)

        if amount <= 0:
            return None

        order = await self.exchange.place_order(
            symbol=signal.symbol, side=side,
            order_type=OrderType.MARKET, amount=amount,
            leverage=actual_leverage, market_type=market_type,
        )

        if order.status == OrderStatus.FILLED:
            sp.record_add(order.filled, order.average_price)
            self._log_trade(signal, order, "open")
            logger.info("Opened {} {} {} @ {:.6f} (phase: {}, mode: {})",
                        signal.market_type, side.value, signal.symbol,
                        order.average_price, sp.phase.value, sp.mode.value)

            pos = Position(
                symbol=signal.symbol, side=side, amount=amount,
                entry_price=order.average_price, current_price=order.average_price,
                leverage=actual_leverage, market_type=market_type.value,
            )
            # PYRAMID: ultra-wide initial stop. Market makers wick through
            # expected support to grab stop-loss liquidity, then reverse.
            # We WANT to survive the wick and DCA into it. The initial $50
            # risk is small — even if we get wicked 15-20%, the dollar loss
            # is tiny. The real stop is "thesis invalidated" level, not a
            # tight technical level that MMs will hunt.
            if pyramid:
                pyramid_stop = max(sp.dca_interval_pct * 8, 15.0)
                self.trailing.register(
                    pos, initial_stop_pct=pyramid_stop, low_liquidity=low_liquidity,
                )
                logger.info("PYRAMID wide stop for {}: {:.1f}% (survive wicks, DCA zone)",
                            signal.symbol, pyramid_stop)
            else:
                self.trailing.register(pos, low_liquidity=low_liquidity)

        self._active_orders.append(order)
        return order

    # ------------------------------------------------------------------ #
    #  Scaling: add to positions (both WINNERS and PYRAMID)
    # ------------------------------------------------------------------ #

    async def try_scale_in(self) -> list[Order]:
        """Add to existing positions (winners or DCA-down)."""
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

            tag = "DCA DOWN" if sp.mode == ScaleMode.PYRAMID else "SCALE UP"
            logger.info("{} into {} | add #{} | amount: {:.6f} | profit: {:.2f}% | avg: {:.6f}",
                        tag, symbol, sp.adds + 1, amount, pos.pnl_pct, sp.avg_entry_price)

            order = await self.exchange.place_order(
                symbol=symbol, side=side, order_type=OrderType.MARKET,
                amount=amount, leverage=sp.current_leverage,
                market_type=market_type,
            )

            if order.status == OrderStatus.FILLED:
                sp.record_add(order.filled, order.average_price)
                self._log_trade(
                    Signal(symbol=symbol,
                           action=SignalAction.BUY if side == OrderSide.BUY else SignalAction.SELL,
                           strategy=sp.strategy, reason=f"{sp.mode.value}_add_#{sp.adds}",
                           market_type=sp.market_type),
                    order, "scale_in",
                )

                ts = self.trailing.get(symbol)
                if ts:
                    ts.entry_price = sp.avg_entry_price
                    logger.info("Updated trail entry for {} to avg: {:.6f}", symbol, sp.avg_entry_price)

                added.append(order)

        return added

    # ------------------------------------------------------------------ #
    #  PYRAMID: leverage raise when in profit
    # ------------------------------------------------------------------ #

    async def try_lever_up(self) -> list[str]:
        """For PYRAMID positions: raise leverage once avg entry is in profit.

        Flow: start at low leverage -> DCA down -> price recovers above avg ->
        raise leverage to amplify gains -> lock break-even stop.
        """
        levered: list[str] = []
        positions = await self.exchange.fetch_positions()
        prices = {p.symbol: p.current_price for p in positions}

        symbols = self.scaler.get_symbols_to_lever_up(prices)
        for symbol in symbols:
            sp = self.scaler.get(symbol)
            if not sp:
                continue

            new_lev = sp.target_leverage
            logger.info("LEVER UP {} | {}x -> {}x | avg entry: {:.6f} | current: {:.6f}",
                        symbol, sp.current_leverage, new_lev,
                        sp.avg_entry_price, prices.get(symbol, 0))

            try:
                await self.exchange.set_leverage(symbol, new_lev)
                sp.record_lever_up(new_lev)

                # Lock break-even on the trailing stop
                ts = self.trailing.get(symbol)
                if ts and sp.breakeven_after_lever:
                    ts.breakeven_locked = True
                    ts.current_stop = sp.avg_entry_price
                    logger.info("BREAK-EVEN locked for {} after leverage raise (stop -> {:.6f})",
                                symbol, sp.avg_entry_price)

                levered.append(symbol)
            except Exception as e:
                logger.error("Failed to raise leverage on {}: {}", symbol, e)

        return levered

    # ------------------------------------------------------------------ #
    #  PYRAMID: partial profit take (pull money out)
    # ------------------------------------------------------------------ #

    async def try_partial_take(self) -> list[Order]:
        """For PYRAMID positions: close a portion to pull capital off the table.

        After leverage is raised and position is in deeper profit,
        sell e.g. 30% to reduce risk. The remaining 70% rides with trail.
        """
        taken: list[Order] = []
        positions = await self.exchange.fetch_positions()
        prices = {p.symbol: p.current_price for p in positions}

        to_take = self.scaler.get_symbols_for_partial_take(prices)
        for symbol, amount in to_take:
            sp = self.scaler.get(symbol)
            if not sp:
                continue

            pos = next((p for p in positions if p.symbol == symbol), None)
            if not pos:
                continue

            close_side = OrderSide.SELL if sp.side == "long" else OrderSide.BUY
            market_type = MarketType(sp.market_type) if sp.market_type else MarketType.FUTURES

            logger.info("PARTIAL TAKE on {} | closing {:.4f} ({:.0f}%) | profit: {:.2f}%",
                        symbol, amount, sp.partial_take_pct, pos.pnl_pct)

            order = await self.exchange.place_order(
                symbol=symbol, side=close_side,
                order_type=OrderType.MARKET, amount=amount,
                leverage=sp.current_leverage, market_type=market_type,
            )

            if order.status == OrderStatus.FILLED:
                pnl_portion = pos.unrealized_pnl * (amount / pos.amount) if pos.amount > 0 else 0
                self.risk.record_pnl(pnl_portion)
                sp.record_partial_close(order.filled)
                self._log_trade(
                    Signal(symbol=symbol, action=SignalAction.CLOSE,
                           strategy=sp.strategy, reason="partial_take_profit",
                           market_type=sp.market_type),
                    order, "partial_close", pnl_portion,
                )
                taken.append(order)

        return taken

    # ------------------------------------------------------------------ #
    #  Hedging: counter-positions on reversal signals
    # ------------------------------------------------------------------ #

    async def try_hedge(self, candles_map: dict[str, list["Candle"]]) -> list[Order]:
        """Check profitable positions for reversal signals and open hedges.

        Example: Long $2500 BTC at +5%. RSI overextended, volume fading.
        -> Tighten stop on the long
        -> Open $500 short (20% of main) with tight 1% stop
        -> If reversal: short prints, long stopped at profit
        -> If no reversal: short stopped for tiny loss, long keeps running
        """
        from core.models import Candle  # avoid circular import at module level

        opened: list[Order] = []
        positions = await self.exchange.fetch_positions()

        # Ensure all profitable positions are tracked
        for pos in positions:
            if pos.pnl_pct > 0 and not self.hedger.get(pos.symbol):
                self.hedger.track_position(pos)

        # Update reversal scores and get symbols ready to hedge
        ready = self.hedger.update(positions, candles_map)

        for symbol in ready:
            if self.hedger.has_active_hedge(symbol):
                continue

            pos = next((p for p in positions if p.symbol == symbol), None)
            if not pos:
                continue

            params = self.hedger.get_hedge_params(
                symbol, pos.current_price, pos.leverage,
            )
            if not params:
                continue

            reasons_str = ", ".join(params["reasons"])
            logger.info("HEDGE OPENING on {} | reversal: {:.0f}% ({}) | "
                        "main: {} ${:.0f} +{:.1f}% | hedge: {} ${:.0f}",
                        symbol, params["reversal_score"] * 100, reasons_str,
                        "long" if pos.side == OrderSide.BUY else "short",
                        pos.notional_value, pos.pnl_pct,
                        params["side"].value, pos.notional_value * self.hedger.hedge_ratio)

            # Tighten the main position's trailing stop before hedging
            ts = self.trailing.get(symbol)
            if ts and not ts.breakeven_locked and pos.pnl_pct > 2.0:
                old_stop = ts.current_stop
                if pos.side == OrderSide.BUY:
                    tight_stop = pos.current_price * (1 - 1.5 / 100)
                    if tight_stop > ts.current_stop:
                        ts.current_stop = tight_stop
                else:
                    tight_stop = pos.current_price * (1 + 1.5 / 100)
                    if tight_stop < ts.current_stop:
                        ts.current_stop = tight_stop
                logger.info("Tightened main stop for {} before hedge: {:.6f} -> {:.6f}",
                            symbol, old_stop, ts.current_stop)

            market_type = MarketType(pos.market_type) if pos.market_type else MarketType.FUTURES

            order = await self.exchange.place_order(
                symbol=symbol,
                side=params["side"],
                order_type=OrderType.MARKET,
                amount=params["amount"],
                leverage=params["leverage"],
                market_type=market_type,
            )

            if order.status == OrderStatus.FILLED:
                self.hedger.activate(symbol, order.average_price, order.filled, order.id)

                # Register a tight trailing stop for the hedge
                hedge_side = params["side"]
                hedge_pos = Position(
                    symbol=symbol, side=hedge_side,
                    amount=params["amount"],
                    entry_price=order.average_price,
                    current_price=order.average_price,
                    leverage=params["leverage"],
                    market_type=market_type.value,
                )
                hedge_sym = f"{symbol}:hedge"
                self.trailing.register(
                    hedge_pos,
                    initial_stop_pct=self.hedger.hedge_stop_pct,
                    trail_pct=self.hedger.hedge_stop_pct * 0.5,
                )

                self._log_trade(
                    Signal(symbol=symbol,
                           action=SignalAction.SELL if hedge_side == OrderSide.SELL else SignalAction.BUY,
                           strategy="hedge", reason=reasons_str,
                           market_type=pos.market_type),
                    order, "hedge_open",
                )
                opened.append(order)

        # Clean up closed main positions
        active_symbols = {p.symbol for p in positions if p.amount > 0}
        for sym in list(self.hedger.active_pairs.keys()):
            if sym not in active_symbols:
                self.hedger.remove(sym)

        return opened

    # ------------------------------------------------------------------ #
    #  Close position
    # ------------------------------------------------------------------ #

    async def _close_position(self, signal: Signal) -> Order | None:
        positions = await self.exchange.fetch_positions(signal.symbol)
        if not positions:
            logger.info("No position to close for {}", signal.symbol)
            return None

        pos = positions[0]
        close_side = OrderSide.SELL if pos.side == OrderSide.BUY else OrderSide.BUY
        market_type = MarketType(pos.market_type) if pos.market_type else MarketType.SPOT

        order = await self.exchange.place_order(
            symbol=signal.symbol, side=close_side,
            order_type=OrderType.MARKET, amount=pos.amount,
            leverage=pos.leverage, market_type=market_type,
        )

        if order.status == OrderStatus.FILLED:
            pnl = pos.unrealized_pnl
            self.risk.record_pnl(pnl)
            self._log_trade(signal, order, "close", pnl)
            logger.info("Closed {} {} PnL: {:.2f}", signal.symbol, pos.side.value, pnl)
            self.scaler.remove(signal.symbol)

        return order

    # ------------------------------------------------------------------ #
    #  Stop management
    # ------------------------------------------------------------------ #

    async def check_stops(self) -> list[Order]:
        closed: list[Order] = []
        positions = await self.exchange.fetch_positions()
        balance_map = await self.exchange.fetch_balance()
        balance = balance_map.get("USDT", 0.0)

        stopped_symbols = self.trailing.update_all(positions)
        for symbol in stopped_symbols:
            ts = self.trailing.get(symbol)
            pnl_info = f" (locked PnL ~{ts.pnl_from_stop:+.1f}%)" if ts else ""
            liq_tag = " [LOW-LIQ]" if ts and ts.low_liquidity else ""

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

    # ------------------------------------------------------------------ #
    #  Quick trade expiry
    # ------------------------------------------------------------------ #

    async def close_expired_quick_trades(self, active_signals: list[Signal]) -> list[Order]:
        """Close quick trades that exceeded max hold time -- BUT only losers.

        RIDE THE WINNERS: if the trade is in profit when time expires,
        don't close it. Let the trailing stop handle the exit. The time
        limit exists to CUT LOSERS that are going nowhere, not to cap
        winners that are still running.
        """
        closed: list[Order] = []
        now = datetime.now(timezone.utc)
        positions = await self.exchange.fetch_positions()
        pos_map = {p.symbol: p for p in positions}

        for signal in active_signals:
            if not signal.quick_trade or not signal.max_hold_minutes:
                continue
            elapsed = (now - signal.timestamp).total_seconds() / 60
            if elapsed < signal.max_hold_minutes:
                continue

            pos = pos_map.get(signal.symbol)
            if pos and pos.pnl_pct > 1.0:
                # In meaningful profit -- let the trailing stop ride it
                logger.info("Quick trade {} expired but in profit ({:+.1f}%) -- letting trail ride",
                            signal.symbol, pos.pnl_pct)
                continue

            close_signal = Signal(
                symbol=signal.symbol, action=SignalAction.CLOSE,
                strategy=signal.strategy, reason="max_hold_time_exceeded",
                market_type=signal.market_type,
            )
            order = await self.execute_signal(close_signal)
            if order:
                closed.append(order)
                self.scaler.remove(signal.symbol)
                logger.info("Cut expired quick trade {} after {:.0f}m (not in profit)", signal.symbol, elapsed)

        return closed

    # ------------------------------------------------------------------ #
    #  Logging
    # ------------------------------------------------------------------ #

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
            "scale_mode": sp.mode.value if sp else "n/a",
            "scale_fill_pct": sp.fill_pct if sp else 0,
            "leverage": sp.current_leverage if sp else 0,
        })

    @property
    def trade_history(self) -> list[dict]:
        return list(self._trade_log)
