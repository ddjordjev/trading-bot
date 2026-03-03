from __future__ import annotations

import contextlib
import math
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from loguru import logger

from config.settings import Settings
from core.exchange.base import BaseExchange
from core.models import (
    Candle,
    MarketType,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Signal,
    SignalAction,
)
from core.orders.hedge import HedgeManager, HedgeState
from core.orders.scaler import PositionScaler, ScaledPosition, ScaleMode
from core.orders.trailing import TrailingStopManager
from core.orders.wick_scalp import WickScalpDetector
from core.risk.manager import RiskManager
from web.metrics import timed


class OrderManager:
    """Translates signals into orders, manages open positions, and enforces stops.

    Scaling modes: PYRAMID (DCA down, lever up) is the default. WINNERS for rare scalps.
    Hedging: when a profitable position shows reversal signs, open a counter-position.
    Wick scalping: when a PYRAMID position is getting wicked, open a quick counter-scalp
                   to profit from the wick while the main position DCA-s down.
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
        self.wick_scalper = WickScalpDetector()
        self._active_orders: list[Order] = []
        self._trade_log: list[dict[str, Any]] = []
        self._closed_scalers: dict[str, list[ScaledPosition]] = {}  # stashed before removal for logging
        self._scale_in_cooldowns: dict[str, datetime] = {}
        self._partial_take_cooldowns: dict[str, datetime] = {}
        self._hedge_cooldowns: dict[str, datetime] = {}
        self._ORDER_COOLDOWN_SECS = 60
        self._protection_orders: dict[str, dict[str, Any]] = {}
        self._protection_retry_after: dict[str, datetime] = {}
        self._protection_error_counts: dict[str, int] = {}
        self._protection_short_stop_floor: dict[str, float] = {}
        self._protection_long_stop_ceiling: dict[str, float] = {}
        self._protection_replace_min_interval_secs = 10
        self._protection_place_min_interval_secs = 10
        self._protection_alignment_tolerance_pct = 0.03
        self._protection_error_backoff_base_secs = 30
        self._protection_error_backoff_max_secs = 300
        self._orphan_protection_cleanup_interval_secs = 60
        self._last_orphan_protection_cleanup_at: datetime | None = None
        # Keep wick scalp strictly smaller than main exposure so it cannot flip
        # net direction and look like a "double trade" against the main idea.
        self._wick_scalp_max_main_fraction = 0.45

    def _is_fast_trailing_signal(self, signal: Signal) -> bool:
        strategy = str(getattr(signal, "strategy", "") or "").strip().lower()
        bot_id = str(getattr(self.settings, "bot_id", "") or "").strip().lower()
        if bot_id in {"extreme", "aggressive", "scalper"}:
            return True
        if bool(getattr(signal, "quick_trade", False)):
            return True
        return strategy.startswith("extreme_") or "wick" in strategy or strategy == "wick_scalp"

    # ------------------------------------------------------------------ #
    #  Signal execution
    # ------------------------------------------------------------------ #

    @timed("orders.execute_signal")
    async def execute_signal(self, signal: Signal, low_liquidity: bool = False, pyramid: bool = False) -> Order | None:
        balance_map = await self.exchange.fetch_balance()
        balance = self.settings.cap_balance(balance_map.get("USDT", 0.0))
        positions = await self.exchange.fetch_positions()

        if not self.risk.check_signal(signal, balance, positions):
            return None

        signal = self.risk.apply_stops(signal)

        if signal.action == SignalAction.CLOSE:
            return await self._close_position(signal)
        if signal.action == SignalAction.HOLD:
            return None

        return await self._open_position(signal, balance, low_liquidity, pyramid)

    async def _open_position(
        self, signal: Signal, balance: float, low_liquidity: bool = False, pyramid: bool = False
    ) -> Order | None:
        price = signal.suggested_price or 0
        if not (price > 0 and math.isfinite(price)):
            logger.warning("No valid price for {} (got {!r}), skipping", signal.symbol, signal.suggested_price)
            return None

        target_leverage = signal.leverage or self.settings.default_leverage
        market_type = MarketType(signal.market_type) if signal.market_type else MarketType.SPOT

        side = OrderSide.BUY if signal.action == SignalAction.BUY else OrderSide.SELL
        side_str = "long" if side == OrderSide.BUY else "short"

        if low_liquidity:
            amount = self.scaler.gambling_size(balance, price, target_leverage)
            sp = self.scaler.create(
                symbol=signal.symbol,
                side=side_str,
                strategy=signal.strategy,
                market_type=signal.market_type or "futures",
                leverage=target_leverage,
                low_liquidity=True,
            )
            actual_leverage = target_leverage
            logger.info("LOW-LIQ gambling bet on {} | ${:.0f} | size: {:.6f}", signal.symbol, amount * price, amount)

        elif pyramid:
            sp = self.scaler.create(
                symbol=signal.symbol,
                side=side_str,
                strategy=signal.strategy,
                market_type=signal.market_type or "futures",
                leverage=target_leverage,
                mode=ScaleMode.PYRAMID,
            )
            amount = sp.get_initial_amount(price)
            actual_leverage = sp.initial_leverage
            logger.info(
                "PYRAMID entry on {} | ${:.0f} initial (cap: ${:.0f}K) | lev: {}x (target: {}x)",
                signal.symbol,
                amount * price,
                sp.max_notional / 1000,
                actual_leverage,
                target_leverage,
            )
        else:
            sp = self.scaler.create(
                symbol=signal.symbol,
                side=side_str,
                strategy=signal.strategy,
                market_type=signal.market_type or "futures",
                leverage=target_leverage,
            )
            amount = sp.get_initial_amount(price)
            actual_leverage = target_leverage
            logger.info(
                "Scaled entry on {} | ${:.0f} initial (cap: ${:.0f}K notional)",
                signal.symbol,
                amount * price,
                sp.max_notional / 1000,
            )

        if amount <= 0:
            self.scaler.remove(signal.symbol)
            return None

        try:
            order = await self.exchange.place_order(
                symbol=signal.symbol,
                side=side,
                order_type=OrderType.MARKET,
                amount=amount,
                leverage=actual_leverage,
                market_type=market_type,
            )
        except Exception:
            # Ensure pre-created runtime state is not left behind on failed opens.
            self.scaler.remove(signal.symbol)
            self.trailing.remove(signal.symbol)
            self.trailing.remove(f"{signal.symbol}:hedge")
            self.trailing.remove(f"{signal.symbol}:wick")
            self.hedger.remove(signal.symbol)
            self.wick_scalper.close(signal.symbol)
            raise

        if order.status != OrderStatus.FILLED:
            self.scaler.remove(signal.symbol)
            self._active_orders.append(order)
            return order

        order.strategy = signal.strategy
        sp.record_add(order.filled, order.average_price)
        self._log_trade(signal, order, "open")
        logger.info(
            "Opened {} {} {} @ {:.6f} (phase: {}, mode: {})",
            signal.market_type,
            side.value,
            signal.symbol,
            order.average_price,
            sp.phase.value,
            sp.mode.value,
        )

        pos = Position(
            symbol=signal.symbol,
            side=side,
            amount=order.filled,
            entry_price=order.average_price,
            current_price=order.average_price,
            leverage=actual_leverage,
            market_type=market_type.value,
        )
        tightened = signal.tightened_stop or 0.0
        wick_tighten_enabled = bool(
            signal.quick_trade or signal.strategy.startswith("extreme_") or signal.strategy == "wick_scalp"
        )
        if pyramid:
            is_major = signal.symbol in self.settings.major_symbol_list
            pyramid_stop = max(sp.dca_interval_pct * 3, 5.0) if is_major else max(sp.dca_interval_pct * 8, 15.0)
            fast_trailing = self._is_fast_trailing_signal(signal)
            self.trailing.register(
                pos,
                initial_stop_pct=pyramid_stop,
                low_liquidity=low_liquidity,
                tightened_stop=tightened,
                wick_tighten_enabled=wick_tighten_enabled,
                trailing_mode="fast" if fast_trailing else "pullback",
                pullback_buffer_pct=float(getattr(self.settings, "pullback_trail_buffer_pct", 4.0) or 4.0),
            )
            logger.info(
                "PYRAMID stop for {}: {:.1f}% ({})",
                signal.symbol,
                pyramid_stop,
                "major — tighter" if is_major else "alt — wide DCA zone",
            )
        else:
            fast_trailing = self._is_fast_trailing_signal(signal)
            self.trailing.register(
                pos,
                low_liquidity=low_liquidity,
                tightened_stop=tightened,
                wick_tighten_enabled=wick_tighten_enabled,
                trailing_mode="fast" if fast_trailing else "pullback",
                pullback_buffer_pct=float(getattr(self.settings, "pullback_trail_buffer_pct", 4.0) or 4.0),
            )

        # Best-effort mirror of bot-owned protections to the exchange.
        await self._sync_symbol_protection(pos, self.trailing.get(signal.symbol))
        self._active_orders.append(order)
        return order

    @property
    def _is_extreme_bot(self) -> bool:
        return str(getattr(self.settings, "bot_id", "") or "").strip().lower() == "extreme"

    @staticmethod
    def _is_meaningful_price_change(old: float, new: float, min_delta_pct: float = 0.15) -> bool:
        if old <= 0 or new <= 0:
            return True
        return abs((new - old) / old) * 100.0 >= min_delta_pct

    @staticmethod
    def _pick_closest_stop_order(orders: list[Order], target_price: float) -> Order | None:
        if not orders:
            return None
        valid = [o for o in orders if float(o.stop_price or 0.0) > 0]
        if not valid:
            return None
        if target_price <= 0:
            return valid[0]
        return min(valid, key=lambda o: abs(float(o.stop_price or 0.0) - target_price))

    def _normalize_stop_trigger_price(self, pos: Position, desired_stop: float) -> float:
        """Keep SL trigger on the valid side of current price for futures exchanges."""
        if desired_stop <= 0:
            return desired_stop
        current_price = float(getattr(pos, "current_price", 0.0) or 0.0)
        if current_price <= 0 or not math.isfinite(current_price):
            return desired_stop

        min_gap = max(current_price * 0.002, 1e-8)
        if pos.side == OrderSide.BUY:
            # Long position closes via SELL stop -> trigger should be below current price.
            adjusted = min(desired_stop, current_price - min_gap)
            ceiling = float(self._protection_long_stop_ceiling.get(pos.symbol, 0.0) or 0.0)
            if ceiling > 0:
                adjusted = min(adjusted, ceiling)
        else:
            # Short position closes via BUY stop -> trigger should be above current price.
            adjusted = max(desired_stop, current_price + min_gap)
            floor = float(self._protection_short_stop_floor.get(pos.symbol, 0.0) or 0.0)
            if floor > 0:
                adjusted = max(adjusted, floor)

        if adjusted <= 0 or not math.isfinite(adjusted):
            return desired_stop
        return adjusted

    @staticmethod
    def _extract_error_current_price(error_text: str) -> float:
        match = re.search(r"current\[(\d+(?:\.\d+)?)\]", error_text)
        if not match:
            return 0.0
        raw = float(match.group(1))
        if raw <= 0:
            return 0.0
        # Bybit occasionally encodes prices in 1e8-style integer units in errors.
        if raw > 10000:
            raw = raw / 100000000.0
        return raw

    @staticmethod
    def _is_retryable_protection_exchange_error(error: Exception) -> bool:
        text = str(error).lower()
        return any(
            token in text
            for token in (
                'retcode":110092',
                "retcode 110092",
                "expect rising",
                "expect falling",
                "trigger_price",
                'retcode":110043',
                "retcode 110043",
                "leverage not modified",
            )
        )

    def _register_protection_error_backoff(self, symbol: str, error: Exception) -> bool:
        if not self._is_retryable_protection_exchange_error(error):
            return False
        error_text = str(error)
        lowered = error_text.lower()
        current_from_error = self._extract_error_current_price(error_text)
        if "expect rising" in lowered and current_from_error > 0:
            self._protection_short_stop_floor[symbol] = current_from_error * 1.002
        elif "expect falling" in lowered and current_from_error > 0:
            self._protection_long_stop_ceiling[symbol] = current_from_error * 0.998
        failures = self._protection_error_counts.get(symbol, 0) + 1
        self._protection_error_counts[symbol] = failures
        exponent = min(failures - 1, 3)
        delay_seconds = min(
            self._protection_error_backoff_max_secs,
            self._protection_error_backoff_base_secs * (2**exponent),
        )
        retry_after = datetime.now(UTC) + timedelta(seconds=delay_seconds)
        self._protection_retry_after[symbol] = retry_after
        logger.warning(
            "Protection sync {} paused {}s after exchange error (failure #{}): {}",
            symbol,
            delay_seconds,
            failures,
            error,
        )
        return True

    async def _cancel_protection_order(self, order_id: str, symbol: str, market_type: MarketType) -> None:
        if not order_id:
            return
        with contextlib.suppress(Exception):
            await self.exchange.cancel_order(order_id, symbol, market_type=market_type)

    async def _cancel_symbol_protection_orders(
        self,
        symbol: str,
        market_type: MarketType,
        orders: list[Order],
    ) -> int:
        cancelled = 0
        seen_ids: set[str] = set()
        for order in orders:
            order_id = str(getattr(order, "id", "") or "")
            if not order_id or order_id in seen_ids:
                continue
            seen_ids.add(order_id)
            order_type = getattr(order, "order_type", None)
            stop_price = float(getattr(order, "stop_price", 0.0) or 0.0)
            is_protection = order_type in {OrderType.STOP_LOSS, OrderType.TAKE_PROFIT} or stop_price > 0
            if not is_protection:
                continue
            await self._cancel_protection_order(order_id, symbol, market_type)
            cancelled += 1
        return cancelled

    async def _clear_symbol_protection(self, symbol: str, market_type: MarketType) -> None:
        state = self._protection_orders.pop(symbol, {})
        await self._cancel_protection_order(str(state.get("sl_order_id", "") or ""), symbol, market_type)
        await self._cancel_protection_order(str(state.get("tp_order_id", "") or ""), symbol, market_type)

    async def cleanup_orphan_protection_orders(self, positions: list[Position], force: bool = False) -> int:
        """Cancel stale futures SL/TP orders for symbols with no live position.

        These orders can survive bot restarts or runtime desync and then
        accumulate as "orphan SLs". Cleanup is throttled by default.
        """
        now = datetime.now(UTC)
        if not force and self._last_orphan_protection_cleanup_at is not None:
            elapsed = (now - self._last_orphan_protection_cleanup_at).total_seconds()
            if elapsed < self._orphan_protection_cleanup_interval_secs:
                return 0
        self._last_orphan_protection_cleanup_at = now

        live_symbols = {
            str(p.symbol or "")
            for p in positions
            if float(getattr(p, "amount", 0.0) or 0.0) > 0
            and str(getattr(p, "market_type", "") or "futures").lower() == "futures"
        }
        try:
            open_orders = await self.exchange.fetch_open_orders(market_type=MarketType.FUTURES)
        except Exception as e:
            logger.warning("Orphan SL cleanup: could not fetch open futures orders: {}", e)
            return 0

        cancelled = 0
        seen_ids: set[str] = set()
        for order in open_orders:
            symbol = str(getattr(order, "symbol", "") or "")
            if not symbol or symbol in live_symbols:
                continue
            order_id = str(getattr(order, "id", "") or "")
            if not order_id or order_id in seen_ids:
                continue
            seen_ids.add(order_id)

            order_type = getattr(order, "order_type", None)
            stop_price = float(getattr(order, "stop_price", 0.0) or 0.0)
            is_protection = order_type in {OrderType.STOP_LOSS, OrderType.TAKE_PROFIT} or stop_price > 0
            if not is_protection:
                continue

            try:
                await self.exchange.cancel_order(order_id, symbol, market_type=MarketType.FUTURES)
            except Exception as e:
                logger.debug("Orphan SL cleanup: cancel failed for {} {}: {}", symbol, order_id, e)
                continue
            cancelled += 1

            state = self._protection_orders.get(symbol)
            if isinstance(state, dict):
                if str(state.get("sl_order_id", "") or "") == order_id:
                    state.pop("sl_order_id", None)
                    state.pop("sl_price", None)
                if str(state.get("tp_order_id", "") or "") == order_id:
                    state.pop("tp_order_id", None)
                    state.pop("tp_price", None)
                if not state.get("sl_order_id") and not state.get("tp_order_id"):
                    self._protection_orders.pop(symbol, None)

        if cancelled:
            logger.info(
                "Orphan SL cleanup: cancelled {} stale protection order(s) for symbols with no live position",
                cancelled,
            )
        return cancelled

    async def ensure_orphan_position_protection(self, pos: Position) -> bool:
        """Best-effort emergency SL sync for a live position not managed by this bot."""
        try:
            market_type = MarketType(pos.market_type) if pos.market_type else MarketType.FUTURES
        except Exception:
            market_type = MarketType.FUTURES
        if market_type != MarketType.FUTURES:
            return False
        if float(getattr(pos, "amount", 0.0) or 0.0) <= 0:
            return False

        entry = float(getattr(pos, "entry_price", 0.0) or 0.0)
        current = float(getattr(pos, "current_price", 0.0) or 0.0)
        if entry <= 0 and current <= 0:
            return False

        ref_price = entry if entry > 0 else current
        stop_pct = max(0.8, float(self.settings.stop_loss_pct))
        if pos.side == OrderSide.BUY:
            emergency_stop = ref_price * (1 - stop_pct / 100.0)
            if current > 0:
                emergency_stop = min(emergency_stop, current * 0.995)
        else:
            emergency_stop = ref_price * (1 + stop_pct / 100.0)
            if current > 0:
                emergency_stop = max(emergency_stop, current * 1.005)
        if emergency_stop <= 0 or not math.isfinite(emergency_stop):
            return False

        synthetic_ts = SimpleNamespace(current_stop=emergency_stop)
        await self._sync_symbol_protection(pos, synthetic_ts)
        return True

    def _derive_extreme_tp(self, entry_price: float, side: OrderSide) -> float:
        tp_pct = max(0.5, float(self.settings.take_profit_pct))
        if entry_price <= 0:
            return 0.0
        if side == OrderSide.BUY:
            return entry_price * (1 + tp_pct / 100.0)
        return entry_price * (1 - tp_pct / 100.0)

    def _ratchet_extreme_tp(self, pos: Position, current_tp: float, bot_stop: float) -> float:
        if pos.current_price <= 0:
            return current_tp
        base_pct = max(0.5, float(self.settings.take_profit_pct) * 0.6)
        if pos.side == OrderSide.BUY:
            candidate = pos.current_price * (1 + base_pct / 100.0)
            return max(current_tp, candidate, self._derive_extreme_tp(pos.entry_price, pos.side))

        candidate = pos.current_price * (1 - base_pct / 100.0)
        floor = self._derive_extreme_tp(pos.entry_price, pos.side)
        if current_tp > 0:
            floor = min(floor, current_tp)
        return min(floor, candidate, bot_stop * 0.999 if bot_stop > 0 else candidate)

    async def _sync_symbol_protection(self, pos: Position, ts: Any | None) -> None:
        try:
            market_type = MarketType(pos.market_type) if pos.market_type else MarketType.FUTURES
        except Exception:
            market_type = MarketType.FUTURES
        if market_type != MarketType.FUTURES:
            logger.debug("Protection sync skip {}: market_type={}", pos.symbol, market_type.value)
            return

        amount = float(pos.amount or 0.0)
        if amount <= 0:
            logger.debug("Protection sync skip {}: amount <= 0", pos.symbol)
            return
        bot_stop = float(getattr(ts, "current_stop", 0.0) or 0.0) if ts else 0.0
        if bot_stop <= 0:
            logger.debug("Protection sync skip {}: bot_stop <= 0 (has_trail={})", pos.symbol, bool(ts))
            return
        exchange_stop = self._normalize_stop_trigger_price(pos, bot_stop)
        if self._is_meaningful_price_change(bot_stop, exchange_stop, min_delta_pct=0.001):
            logger.debug(
                "Protection sync {}: adjusted invalid stop trigger {:.6f} -> {:.6f} (current={:.6f}, side={})",
                pos.symbol,
                bot_stop,
                exchange_stop,
                float(getattr(pos, "current_price", 0.0) or 0.0),
                "long" if pos.side == OrderSide.BUY else "short",
            )

        symbol = pos.symbol
        retry_after = self._protection_retry_after.get(symbol)
        if retry_after and datetime.now(UTC) < retry_after:
            logger.debug("Protection sync {} paused until {}", symbol, retry_after.isoformat())
            return
        stop_side = OrderSide.SELL if pos.side == OrderSide.BUY else OrderSide.BUY
        state = self._protection_orders.setdefault(symbol, {})
        sl_order_id = str(state.get("sl_order_id", "") or "")
        sl_price = float(state.get("sl_price", 0.0) or 0.0)
        sl_replaced_or_created = False
        logger.debug(
            "Protection sync {}: side={} amount={:.6f} bot_stop={:.6f} prev_sl_id={} prev_sl={:.6f}",
            symbol,
            "long" if pos.side == OrderSide.BUY else "short",
            amount,
            bot_stop,
            sl_order_id or "none",
            sl_price,
        )

        open_orders: list[Order] = []
        with contextlib.suppress(Exception):
            open_orders = await self.exchange.fetch_open_orders(symbol=symbol, market_type=market_type)
        symbol_protection_orders = [
            o
            for o in open_orders
            if getattr(o, "order_type", None) in {OrderType.STOP_LOSS, OrderType.TAKE_PROFIT}
            or float(getattr(o, "stop_price", 0.0) or 0.0) > 0
        ]
        symbol_stop_orders = [
            o for o in symbol_protection_orders if getattr(o, "order_type", None) == OrderType.STOP_LOSS
        ]
        side_matched_stop_orders = [o for o in symbol_stop_orders if getattr(o, "side", None) == stop_side]
        adopted_order_id = ""

        if sl_order_id:
            try:
                sl_order = await self.exchange.fetch_order(sl_order_id, symbol, market_type=market_type)
                if sl_order.status not in {OrderStatus.OPEN, OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED}:
                    logger.warning(
                        "Protection sync {}: SL order {} status={} ; recreating",
                        symbol,
                        sl_order_id,
                        sl_order.status.value if hasattr(sl_order.status, "value") else str(sl_order.status),
                    )
                    sl_order_id = ""
                else:
                    fetched_stop = float(getattr(sl_order, "stop_price", 0.0) or 0.0)
                    if fetched_stop > 0:
                        sl_price = fetched_stop
            except Exception as e:
                logger.debug(
                    "Protection sync {}: SL id-verify miss for {} ({}), attempting open-order fallback",
                    symbol,
                    sl_order_id,
                    e,
                )
                try:
                    open_orders = await self.exchange.fetch_open_orders(symbol=symbol, market_type=market_type)
                    stop_orders = [o for o in open_orders if o.order_type == OrderType.STOP_LOSS]
                    fallback = self._pick_closest_stop_order(stop_orders, exchange_stop)
                    if fallback:
                        fallback_price = float(fallback.stop_price or 0.0)
                        logger.warning(
                            "Protection sync {}: adopted fallback SL order {} @ {:.6f} (was id={})",
                            symbol,
                            fallback.id,
                            fallback_price,
                            sl_order_id,
                        )
                        sl_order_id = fallback.id
                        sl_price = fallback_price
                    else:
                        logger.warning(
                            "Protection sync {}: no fallback SL order found after id-verify miss; will recreate",
                            symbol,
                        )
                        sl_order_id = ""
                except Exception as fallback_err:
                    logger.debug(
                        "Protection sync {}: fallback open-order lookup failed: {}",
                        symbol,
                        fallback_err,
                    )
        elif side_matched_stop_orders:
            adopted = self._pick_closest_stop_order(side_matched_stop_orders, bot_stop)
            if adopted:
                sl_order_id = str(adopted.id or "")
                adopted_order_id = sl_order_id
                sl_price = float(adopted.stop_price or 0.0)
                logger.debug(
                    "Protection sync {}: adopted existing side-matched SL order {} @ {:.6f}",
                    symbol,
                    sl_order_id or "unknown",
                    sl_price,
                )

        # Keep exactly one stop-loss per symbol+side. If the exchange already has
        # multiple same-side SL orders, cancel extras proactively to prevent drift.
        if side_matched_stop_orders:
            keep_id = sl_order_id or adopted_order_id
            duplicate_ids = [
                str(o.id or "") for o in side_matched_stop_orders if str(o.id or "") and str(o.id or "") != keep_id
            ]
            if duplicate_ids:
                logger.warning(
                    "Protection sync {}: found {} duplicate same-side SL order(s); cancelling extras {}",
                    symbol,
                    len(duplicate_ids),
                    ", ".join(sorted(duplicate_ids)),
                )
                for duplicate_id in duplicate_ids:
                    await self._cancel_protection_order(duplicate_id, symbol, market_type)

        should_replace = bool(
            sl_order_id
            and self._is_meaningful_price_change(
                sl_price,
                exchange_stop,
                min_delta_pct=self._protection_alignment_tolerance_pct,
            )
        )
        if should_replace:
            last_replace_at = state.get("sl_last_replace_at")
            if isinstance(last_replace_at, datetime):
                elapsed = (datetime.now(UTC) - last_replace_at).total_seconds()
                if elapsed < self._protection_replace_min_interval_secs:
                    logger.debug(
                        "Protection sync {}: skipping SL replace due to {}s throttle (elapsed={:.2f}s)",
                        symbol,
                        self._protection_replace_min_interval_secs,
                        elapsed,
                    )
                    should_replace = False

        if should_replace:
            logger.debug(
                "Protection sync {}: replacing SL order {} due to price change {:.6f} -> {:.6f}",
                symbol,
                sl_order_id,
                sl_price,
                exchange_stop,
            )
            await self._cancel_protection_order(sl_order_id, symbol, market_type)
            sl_order_id = ""

        if not sl_order_id:
            last_place_at = state.get("sl_last_place_at")
            if isinstance(last_place_at, datetime):
                elapsed = (datetime.now(UTC) - last_place_at).total_seconds()
                if elapsed < self._protection_place_min_interval_secs:
                    logger.debug(
                        "Protection sync {}: skipping SL create due to {}s cadence gate (elapsed={:.2f}s)",
                        symbol,
                        self._protection_place_min_interval_secs,
                        elapsed,
                    )
                    return
            try:
                sl_order = await self.exchange.place_order(
                    symbol=symbol,
                    side=stop_side,
                    order_type=OrderType.STOP_LOSS,
                    amount=amount,
                    stop_price=exchange_stop,
                    leverage=pos.leverage,
                    market_type=market_type,
                )
            except Exception as e:
                msg = str(e).lower()
                if "-4045" in msg or "max stop order limit" in msg:
                    self._protection_retry_after[symbol] = datetime.now(UTC) + timedelta(seconds=30)
                    logger.warning(
                        "Protection sync {} paused 30s due to stop-order limit: {}",
                        symbol,
                        e,
                    )
                    return
                if self._register_protection_error_backoff(symbol, e):
                    return
                raise
            sl_order_id = sl_order.id
            logger.info("Protection SL synced on {} @ {:.6f}", symbol, exchange_stop)
            self._protection_retry_after.pop(symbol, None)
            self._protection_error_counts.pop(symbol, None)
            self._protection_short_stop_floor.pop(symbol, None)
            self._protection_long_stop_ceiling.pop(symbol, None)
            state["sl_last_replace_at"] = datetime.now(UTC)
            state["sl_last_place_at"] = datetime.now(UTC)
            sl_replaced_or_created = True

        state["sl_order_id"] = sl_order_id
        if sl_replaced_or_created:
            state["sl_price"] = exchange_stop
        elif sl_price > 0:
            # Keep the last confirmed exchange SL price when no replacement
            # happened, so diff checks compare CEX vs bot target correctly.
            state["sl_price"] = sl_price

        if not self._is_extreme_bot:
            tp_order_id = str(state.get("tp_order_id", "") or "")
            if tp_order_id:
                logger.debug("Protection sync {}: removing stale TP order {} (non-extreme bot)", symbol, tp_order_id)
                await self._cancel_protection_order(tp_order_id, symbol, market_type)
            state.pop("tp_order_id", None)
            state.pop("tp_price", None)
            return

        current_tp = float(state.get("tp_price", 0.0) or 0.0)
        target_tp = self._ratchet_extreme_tp(pos, current_tp, bot_stop)
        if target_tp <= 0:
            return

        tp_order_id = str(state.get("tp_order_id", "") or "")
        if tp_order_id:
            try:
                tp_order = await self.exchange.fetch_order(tp_order_id, symbol, market_type=market_type)
                if tp_order.status not in {OrderStatus.OPEN, OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED}:
                    logger.warning(
                        "Protection sync {}: TP order {} status={} ; recreating",
                        symbol,
                        tp_order_id,
                        tp_order.status.value if hasattr(tp_order.status, "value") else str(tp_order.status),
                    )
                    tp_order_id = ""
            except Exception as e:
                logger.debug("Protection sync {}: cannot verify TP order state: {}", symbol, e)

        if tp_order_id and self._is_meaningful_price_change(current_tp, target_tp):
            logger.debug(
                "Protection sync {}: replacing TP order {} due to target change {:.6f} -> {:.6f}",
                symbol,
                tp_order_id,
                current_tp,
                target_tp,
            )
            await self._cancel_protection_order(tp_order_id, symbol, market_type)
            tp_order_id = ""

        if not tp_order_id:
            try:
                tp_order = await self.exchange.place_order(
                    symbol=symbol,
                    side=stop_side,
                    order_type=OrderType.TAKE_PROFIT,
                    amount=amount,
                    stop_price=target_tp,
                    leverage=pos.leverage,
                    market_type=market_type,
                )
            except Exception as e:
                msg = str(e).lower()
                if "-4045" in msg or "max stop order limit" in msg:
                    self._protection_retry_after[symbol] = datetime.now(UTC) + timedelta(seconds=30)
                    logger.warning(
                        "Protection TP sync {} paused 30s due to stop-order limit: {}",
                        symbol,
                        e,
                    )
                    return
                if self._register_protection_error_backoff(symbol, e):
                    return
                raise
            tp_order_id = tp_order.id
            logger.info("Protection TP synced on {} @ {:.6f}", symbol, target_tp)
            self._protection_retry_after.pop(symbol, None)
            self._protection_error_counts.pop(symbol, None)

        state["tp_order_id"] = tp_order_id
        state["tp_price"] = target_tp

    # ------------------------------------------------------------------ #
    #  Scaling: add to positions (both WINNERS and PYRAMID)
    # ------------------------------------------------------------------ #

    MAX_DCA_ADDS_PER_TICK = 1

    async def try_scale_in(self) -> list[Order]:
        """Add to existing positions (winners or DCA-down).

        Capped to MAX_DCA_ADDS_PER_TICK per tick to prevent rapid
        balance depletion when multiple positions qualify simultaneously.
        """
        added: list[Order] = []
        positions = await self.exchange.fetch_positions()
        prices = {p.symbol: p.current_price for p in positions}
        to_add = self.scaler.get_symbols_to_add(prices)

        for symbol, amount in to_add:
            if len(added) >= self.MAX_DCA_ADDS_PER_TICK:
                break

            cd = self._scale_in_cooldowns.get(symbol)
            if cd and (datetime.now(UTC) - cd).total_seconds() < self._ORDER_COOLDOWN_SECS:
                continue

            sp = self.scaler.get(symbol)
            if not sp:
                continue

            pos = next((p for p in positions if p.symbol == symbol), None)
            if not pos:
                continue

            side = OrderSide.BUY if sp.side == "long" else OrderSide.SELL
            market_type = MarketType(sp.market_type) if sp.market_type else MarketType.FUTURES

            tag = "DCA DOWN" if sp.mode == ScaleMode.PYRAMID else "SCALE UP"
            logger.info(
                "{} into {} | add #{} | amount: {:.6f} | profit: {:.2f}% | avg: {:.6f}",
                tag,
                symbol,
                sp.adds + 1,
                amount,
                pos.pnl_pct,
                sp.avg_entry_price,
            )

            order = await self.exchange.place_order(
                symbol=symbol,
                side=side,
                order_type=OrderType.MARKET,
                amount=amount,
                leverage=sp.current_leverage,
                market_type=market_type,
            )

            if order.status == OrderStatus.FILLED:
                self._scale_in_cooldowns.pop(symbol, None)
                sp.record_add(order.filled, order.average_price)
                self._log_trade(
                    Signal(
                        symbol=symbol,
                        action=SignalAction.BUY if side == OrderSide.BUY else SignalAction.SELL,
                        strategy=sp.strategy,
                        reason=f"{sp.mode.value}_add_#{sp.adds}",
                        market_type=sp.market_type,
                    ),
                    order,
                    "scale_in",
                )

                ts = self.trailing.get(symbol)
                if ts:
                    ts.entry_price = sp.avg_entry_price
                    logger.info("Updated trail entry for {} to avg: {:.6f}", symbol, sp.avg_entry_price)

                added.append(order)
            else:
                self._scale_in_cooldowns[symbol] = datetime.now(UTC)

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
            logger.info(
                "LEVER UP {} | {}x -> {}x | avg entry: {:.6f} | current: {:.6f}",
                symbol,
                sp.current_leverage,
                new_lev,
                sp.avg_entry_price,
                prices.get(symbol, 0),
            )

            try:
                await self.exchange.set_leverage(symbol, new_lev)
                sp.record_lever_up(new_lev)

                # Lock break-even on the trailing stop
                ts = self.trailing.get(symbol)
                if ts and sp.breakeven_after_lever:
                    ts.breakeven_locked = True
                    ts.current_stop = sp.avg_entry_price
                    logger.info(
                        "BREAK-EVEN locked for {} after leverage raise (stop -> {:.6f})", symbol, sp.avg_entry_price
                    )

                levered.append(symbol)
            except Exception as e:
                logger.error("Failed to raise leverage on {}: {}", symbol, e)

        return levered

    # ------------------------------------------------------------------ #
    #  PYRAMID: partial profit take (pull money out)
    # ------------------------------------------------------------------ #

    async def try_partial_take(self, profit_taking_aggression: float = 1.0) -> list[Order]:
        """For PYRAMID positions: close a portion to pull capital off the table.

        After leverage is raised and position is in deeper profit,
        sell e.g. 30% to reduce risk. The remaining 70% rides with trail.

        When profit_taking_aggression > 1.0 (daily target not yet secured),
        partials trigger at a lower profit threshold — take profits early.
        """
        taken: list[Order] = []
        positions = await self.exchange.fetch_positions()
        prices = {p.symbol: p.current_price for p in positions}

        to_take = self.scaler.get_symbols_for_partial_take(prices, profit_taking_aggression)
        for symbol, amount in to_take:
            cd = self._partial_take_cooldowns.get(symbol)
            if cd and (datetime.now(UTC) - cd).total_seconds() < self._ORDER_COOLDOWN_SECS:
                continue

            sp = self.scaler.get(symbol)
            if not sp:
                continue

            pos = next((p for p in positions if p.symbol == symbol), None)
            if not pos:
                continue

            close_side = OrderSide.SELL if sp.side == "long" else OrderSide.BUY
            market_type = MarketType(sp.market_type) if sp.market_type else MarketType.FUTURES

            # Cap to exchange position size so we never request more than we have
            # (scaler can drift from exchange e.g. rounding; over-close would full-close by mistake)
            amount = min(amount, pos.amount)
            if amount <= 0:
                continue

            logger.info(
                "PARTIAL TAKE on {} | closing {:.4f} ({:.0f}%) | profit: {:.2f}%",
                symbol,
                amount,
                sp.partial_take_pct,
                pos.pnl_pct,
            )

            order = await self.exchange.place_order(
                symbol=symbol,
                side=close_side,
                order_type=OrderType.MARKET,
                amount=amount,
                leverage=sp.current_leverage,
                market_type=market_type,
            )

            if order.status == OrderStatus.FILLED:
                self._partial_take_cooldowns.pop(symbol, None)
                pnl_portion = pos.unrealized_pnl * (amount / pos.amount) if pos.amount > 0 else 0
                self.risk.record_pnl(pnl_portion)
                sp.record_partial_close(order.filled)
                self._log_trade(
                    Signal(
                        symbol=symbol,
                        action=SignalAction.CLOSE,
                        strategy=sp.strategy,
                        reason="partial_take_profit",
                        market_type=sp.market_type,
                    ),
                    order,
                    "partial_close",
                    pnl_portion,
                )
                taken.append(order)
            else:
                self._partial_take_cooldowns[symbol] = datetime.now(UTC)

        return taken

    async def manual_take_profit(self, symbol: str, pct: float) -> Order | None:
        """Close a requested percentage of an open position with bookkeeping."""
        positions = await self.exchange.fetch_positions()
        pos = next((p for p in positions if p.symbol == symbol and p.amount > 0), None)
        if not pos:
            return None

        sp = self.scaler.get(symbol)
        if not sp:
            return None

        close_pct = max(1.0, min(100.0, pct))
        amount = pos.amount * (close_pct / 100.0)
        amount = min(amount, pos.amount)
        if amount <= 0:
            return None

        close_side = OrderSide.SELL if sp.side == "long" else OrderSide.BUY
        market_type = MarketType(sp.market_type) if sp.market_type else MarketType.FUTURES

        order = await self.exchange.place_order(
            symbol=symbol,
            side=close_side,
            order_type=OrderType.MARKET,
            amount=amount,
            leverage=sp.current_leverage,
            market_type=market_type,
        )
        if order.status != OrderStatus.FILLED:
            return None

        pnl_portion = pos.unrealized_pnl * (amount / pos.amount) if pos.amount > 0 else 0.0
        self.risk.record_pnl(pnl_portion)
        sp.record_partial_close(order.filled)
        self._log_trade(
            Signal(
                symbol=symbol,
                action=SignalAction.CLOSE,
                strategy=sp.strategy,
                reason="manual_take_profit",
                market_type=sp.market_type,
            ),
            order,
            "manual_partial_close",
            pnl_portion,
        )
        return order

    # ------------------------------------------------------------------ #
    #  Hedging: counter-positions on reversal signals
    # ------------------------------------------------------------------ #

    async def try_hedge(self, candles_map: dict[str, list[Candle]]) -> list[Order]:
        """Check profitable positions for reversal signals and open hedges.

        Example: Long $2500 BTC at +5%. RSI overextended, volume fading.
        -> Tighten stop on the long
        -> Open $500 short (20% of main) with tight 1% stop
        -> If reversal: short prints, long stopped at profit
        -> If no reversal: short stopped for tiny loss, long keeps running
        """

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

            cd = self._hedge_cooldowns.get(symbol)
            if cd and (datetime.now(UTC) - cd).total_seconds() < self._ORDER_COOLDOWN_SECS:
                continue

            main_pos = next((p for p in positions if p.symbol == symbol), None)
            if not main_pos:
                continue

            params = self.hedger.get_hedge_params(
                symbol,
                main_pos.current_price,
                main_pos.leverage,
            )
            if not params:
                continue

            reasons_str = ", ".join(params["reasons"])
            logger.info(
                "HEDGE OPENING on {} | reversal: {:.0f}% ({}) | main: {} ${:.0f} +{:.1f}% | hedge: {} ${:.0f}",
                symbol,
                params["reversal_score"] * 100,
                reasons_str,
                "long" if main_pos.side == OrderSide.BUY else "short",
                main_pos.notional_value,
                main_pos.pnl_pct,
                params["side"].value,
                main_pos.notional_value * self.hedger.hedge_ratio,
            )

            # Tighten the main position's trailing stop before hedging
            ts = self.trailing.get(symbol)
            if ts and not ts.breakeven_locked and main_pos.pnl_pct > 2.0:
                old_stop = ts.current_stop
                if main_pos.side == OrderSide.BUY:
                    tight_stop = main_pos.current_price * (1 - 1.5 / 100)
                    if tight_stop > ts.current_stop:
                        ts.current_stop = tight_stop
                else:
                    tight_stop = main_pos.current_price * (1 + 1.5 / 100)
                    if tight_stop < ts.current_stop:
                        ts.current_stop = tight_stop
                logger.info(
                    "Tightened main stop for {} before hedge: {:.6f} -> {:.6f}", symbol, old_stop, ts.current_stop
                )

            market_type = MarketType(main_pos.market_type) if main_pos.market_type else MarketType.FUTURES

            order = await self.exchange.place_order(
                symbol=symbol,
                side=params["side"],
                order_type=OrderType.MARKET,
                amount=params["amount"],
                leverage=params["leverage"],
                market_type=market_type,
            )

            if order.status == OrderStatus.FILLED:
                self._hedge_cooldowns.pop(symbol, None)
                self.hedger.activate(symbol, order.average_price, order.filled, order.id, params["leverage"])

                hedge_side = params["side"]
                hedge_pos = Position(
                    symbol=symbol,
                    side=hedge_side,
                    amount=params["amount"],
                    entry_price=order.average_price,
                    current_price=order.average_price,
                    leverage=params["leverage"],
                    market_type=market_type.value,
                )
                self.trailing.register(
                    hedge_pos,
                    initial_stop_pct=self.hedger.hedge_stop_pct,
                    trail_pct=self.hedger.hedge_stop_pct * 0.5,
                    key=f"{symbol}:hedge",
                )

                self._log_trade(
                    Signal(
                        symbol=symbol,
                        action=SignalAction.SELL if hedge_side == OrderSide.SELL else SignalAction.BUY,
                        strategy="hedge",
                        reason=reasons_str,
                        market_type=main_pos.market_type,
                    ),
                    order,
                    "hedge_open",
                )
                opened.append(order)
            else:
                self._hedge_cooldowns[symbol] = datetime.now(UTC)

        # Clean up closed main positions
        active_symbols = {p.symbol for p in positions if p.amount > 0}
        for sym in list(self.hedger.active_pairs.keys()):
            if sym not in active_symbols:
                self.hedger.remove(sym)

        return opened

    # ------------------------------------------------------------------ #
    #  Wick scalping: counter-trade wicks while PYRAMID DCA-s
    # ------------------------------------------------------------------ #

    async def try_wick_scalps(self) -> list[Order]:
        """Detect wicks on PYRAMID positions and open quick counter-scalps.

        When a PYRAMID long is getting wicked down, open a short scalp to
        ride the wick. When the wick reverses, the scalp profits and the
        main position gets a better DCA entry. Double win.
        """
        opened: list[Order] = []
        positions = await self.exchange.fetch_positions()

        for pos in positions:
            sp = self.scaler.get(pos.symbol)
            if not sp or sp.mode != ScaleMode.PYRAMID:
                continue

            self.wick_scalper.feed_price(pos.symbol, pos.current_price)

            scalp = self.wick_scalper.check_for_wick(
                symbol=pos.symbol,
                main_side=sp.side,
                current_price=pos.current_price,
                entry_price=sp.avg_entry_price,
            )
            if not scalp:
                continue

            scalp_side_order = OrderSide.SELL if scalp.scalp_side == "short" else OrderSide.BUY
            market_type = MarketType(sp.market_type) if sp.market_type else MarketType.FUTURES

            scalp_dollars = self.settings.initial_risk_amount
            scalp_leverage = sp.target_leverage
            if pos.current_price <= 0:
                continue
            raw_scalp_amount = (scalp_dollars * scalp_leverage) / pos.current_price
            main_amount = float(pos.amount or 0.0)
            max_scalp_amount = max(0.0, main_amount * self._wick_scalp_max_main_fraction)
            scalp_amount = min(raw_scalp_amount, max_scalp_amount) if max_scalp_amount > 0 else 0.0
            if scalp_amount <= 0:
                continue
            if scalp_amount < raw_scalp_amount:
                logger.info(
                    "WICK SCALP cap on {}: raw={:.6f} -> capped={:.6f} (max {:.0f}% of main size)",
                    pos.symbol,
                    raw_scalp_amount,
                    scalp_amount,
                    self._wick_scalp_max_main_fraction * 100.0,
                )

            logger.info(
                "WICK SCALP on {} | main={} pyramid | scalp={} ${:.0f} @ {}x",
                pos.symbol,
                sp.side,
                scalp.scalp_side,
                scalp_dollars,
                scalp_leverage,
            )

            order = await self.exchange.place_order(
                symbol=pos.symbol,
                side=scalp_side_order,
                order_type=OrderType.MARKET,
                amount=scalp_amount,
                leverage=scalp_leverage,
                market_type=market_type,
            )

            if order.status == OrderStatus.FILLED:
                self.wick_scalper.activate(
                    pos.symbol,
                    scalp,
                    order.average_price,
                    order.filled,
                    order.id,
                )

                scalp_pos = Position(
                    symbol=pos.symbol,
                    side=scalp_side_order,
                    amount=scalp_amount,
                    entry_price=order.average_price,
                    current_price=order.average_price,
                    leverage=scalp_leverage,
                    market_type=market_type.value,
                )
                self.trailing.register(
                    scalp_pos,
                    initial_stop_pct=scalp.stop_pct,
                    trail_pct=scalp.trail_pct,
                    key=f"{pos.symbol}:wick",
                )

                self._log_trade(
                    Signal(
                        symbol=pos.symbol,
                        action=SignalAction.SELL if scalp.scalp_side == "short" else SignalAction.BUY,
                        strategy="wick_scalp",
                        reason=f"wick on {sp.side} pyramid",
                        market_type=sp.market_type,
                    ),
                    order,
                    "wick_scalp_open",
                )
                opened.append(order)

        # Close expired wick scalps (partial close — only the scalp amount)
        for sym in self.wick_scalper.get_expired():
            scalp = self.wick_scalper.get(sym)
            if not scalp or scalp.amount <= 0:
                continue
            close_side = OrderSide.BUY if scalp.scalp_side == "short" else OrderSide.SELL
            sp = self.scaler.get(sym)
            market_type = MarketType(sp.market_type) if sp and sp.market_type else MarketType.FUTURES

            close_order = await self.exchange.place_order(
                symbol=sym,
                side=close_side,
                order_type=OrderType.MARKET,
                amount=scalp.amount,
                leverage=scalp.leverage,
                market_type=market_type,
            )
            if close_order and close_order.status == OrderStatus.FILLED:
                exit_price = close_order.average_price or 0
                entry_price = scalp.entry_price
                pnl = 0.0
                if entry_price > 0 and exit_price > 0:
                    if scalp.scalp_side == "long":
                        pnl = (exit_price - entry_price) * scalp.amount
                    else:
                        pnl = (entry_price - exit_price) * scalp.amount
                self.risk.record_pnl(pnl)
                self.wick_scalper.close(sym, pnl)
                self.trailing.remove(f"{sym}:wick")
                self._log_trade(
                    Signal(
                        symbol=sym,
                        action=SignalAction.CLOSE,
                        strategy="wick_scalp",
                        reason="wick_scalp_expired",
                        market_type=market_type.value,
                    ),
                    close_order,
                    "wick_scalp_close",
                    pnl,
                )
                opened.append(close_order)

        self.wick_scalper.cleanup()
        return opened

    # ------------------------------------------------------------------ #
    #  Close position
    # ------------------------------------------------------------------ #

    async def _close_position(self, signal: Signal) -> Order | None:
        positions = await self.exchange.fetch_positions(signal.symbol)
        if not positions:
            logger.info("No position to close for {}", signal.symbol)
            return None

        last_order: Order | None = None
        for pos in positions:
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
                order.strategy = signal.strategy
                pnl = pos.unrealized_pnl
                self.risk.record_pnl(pnl)
                self._log_trade(signal, order, "close", pnl)
                logger.info("Closed {} {} PnL: {:.2f}", signal.symbol, pos.side.value, pnl)
                last_order = order

        if last_order:
            sp = self.scaler.get(signal.symbol)
            if sp:
                self._closed_scalers.setdefault(signal.symbol, []).append(sp)
            self.scaler.remove(signal.symbol)
            self.trailing.remove(signal.symbol)
            self.trailing.remove(f"{signal.symbol}:hedge")
            self.trailing.remove(f"{signal.symbol}:wick")
            self.hedger.remove(signal.symbol)
            self.wick_scalper.close(signal.symbol)
            self._scale_in_cooldowns.pop(signal.symbol, None)
            self._partial_take_cooldowns.pop(signal.symbol, None)
            self._hedge_cooldowns.pop(signal.symbol, None)
            market_type = MarketType(signal.market_type) if signal.market_type else MarketType.FUTURES
            await self._clear_symbol_protection(signal.symbol, market_type)

        return last_order

    # ------------------------------------------------------------------ #
    #  Stop management
    # ------------------------------------------------------------------ #

    @timed("orders.check_stops")
    async def check_stops(self) -> list[Order]:
        closed: list[Order] = []
        positions = await self.exchange.fetch_positions()
        balance_map = await self.exchange.fetch_balance()
        balance = self.settings.cap_balance(balance_map.get("USDT", 0.0))

        stopped_keys = self.trailing.update_all(positions)
        stopped_base_symbols: set[str] = set()

        for key in stopped_keys:
            ts = self.trailing.get(key)
            pnl_info = f" (locked PnL ~{ts.pnl_from_stop:+.1f}%)" if ts else ""
            liq_tag = " [LOW-LIQ]" if ts and ts.low_liquidity else ""

            if ts and ts.activated:
                reason = "trailing_stop"
            elif ts and ts.breakeven_locked:
                reason = "breakeven_stop"
            else:
                reason = "initial_stop"

            logger.info("Stop triggered for {}{}{} (reason: {})", key, pnl_info, liq_tag, reason)

            order: Order | None = None

            if key.endswith(":hedge"):
                symbol = key.rsplit(":", 1)[0]
                order = await self._close_sub_position(symbol, self.hedger, "hedge")
            elif key.endswith(":wick"):
                symbol = key.rsplit(":", 1)[0]
                order = await self._close_sub_position_wick(symbol)
            else:
                symbol = key
                signal = Signal(
                    symbol=symbol,
                    action=SignalAction.CLOSE,
                    strategy="trailing_stop",
                    reason=reason,
                    market_type="futures",
                )
                order = await self.execute_signal(signal)
                if order:
                    self.trailing.remove(f"{symbol}:hedge")
                    self.trailing.remove(f"{symbol}:wick")
                    self.hedger.remove(symbol)
                    self.wick_scalper.close(symbol)

            if order:
                closed.append(order)
                self.trailing.remove(key)
                stopped_base_symbols.add(symbol)

        active_symbols = {p.symbol for p in positions if p.amount > 0}
        for symbol, _state in list(self._protection_orders.items()):
            if symbol in active_symbols:
                continue
            await self._clear_symbol_protection(symbol, MarketType.FUTURES)
            self._protection_orders.pop(symbol, None)

        for pos in positions:
            if pos.symbol in stopped_base_symbols:
                continue
            ts = self.trailing.get(pos.symbol)
            if ts:
                try:
                    await self._sync_symbol_protection(pos, ts)
                except Exception as e:
                    logger.warning("Protection sync failed for {}: {}", pos.symbol, e)

        for pos in positions:
            if pos.symbol in stopped_base_symbols:
                continue
            if self.risk.check_liquidation(pos, balance):
                logger.critical("LIQUIDATION RISK for {} - closing immediately!", pos.symbol)
                signal = Signal(
                    symbol=pos.symbol,
                    action=SignalAction.CLOSE,
                    strategy="risk_manager",
                    reason="liquidation_risk",
                    market_type=pos.market_type,
                )
                order = await self.execute_signal(signal)
                if order:
                    closed.append(order)
                    self.trailing.remove(pos.symbol)
                    self.trailing.remove(f"{pos.symbol}:hedge")
                    self.trailing.remove(f"{pos.symbol}:wick")
                    self.hedger.remove(pos.symbol)
                    self.wick_scalper.close(pos.symbol)

        return closed

    async def _close_sub_position(self, symbol: str, hedger: HedgeManager, tag: str) -> Order | None:
        """Close a hedge sub-position by placing a counter-order for just the hedge amount."""
        pair = hedger.get(symbol)
        if not pair or pair.state != HedgeState.ACTIVE:
            return None

        close_side = OrderSide.BUY if pair.hedge_side == "short" else OrderSide.SELL
        hedge_amount = pair.hedge_size / pair.hedge_entry if pair.hedge_entry > 0 else 0
        if hedge_amount <= 0:
            return None

        order = await self.exchange.place_order(
            symbol=symbol,
            side=close_side,
            order_type=OrderType.MARKET,
            amount=hedge_amount,
            leverage=pair.hedge_leverage or self.settings.default_leverage,
            market_type=MarketType.FUTURES,
        )
        if order and order.status == OrderStatus.FILLED:
            exit_price = order.average_price or 0
            entry_price = pair.hedge_entry
            pnl = 0.0
            if entry_price > 0 and exit_price > 0:
                if pair.hedge_side == "long":
                    pnl = (exit_price - entry_price) * hedge_amount
                else:
                    pnl = (entry_price - exit_price) * hedge_amount
            self.risk.record_pnl(pnl)
            hedger.close(symbol)
            self._log_trade(
                Signal(symbol=symbol, action=SignalAction.CLOSE, strategy=tag, reason=f"{tag}_stop_hit"),
                order,
                f"{tag}_close",
                pnl,
            )
            return order
        return None

    async def _close_sub_position_wick(self, symbol: str) -> Order | None:
        """Close a wick scalp sub-position."""
        scalp = self.wick_scalper.get(symbol)
        if not scalp or scalp.amount <= 0:
            return None

        close_side = OrderSide.BUY if scalp.scalp_side == "short" else OrderSide.SELL
        sp = self.scaler.get(symbol)
        market_type = MarketType(sp.market_type) if sp and sp.market_type else MarketType.FUTURES

        order = await self.exchange.place_order(
            symbol=symbol,
            side=close_side,
            order_type=OrderType.MARKET,
            amount=scalp.amount,
            leverage=scalp.leverage,
            market_type=market_type,
        )
        if order and order.status == OrderStatus.FILLED:
            exit_price = order.average_price or 0
            entry_price = scalp.entry_price
            pnl = 0.0
            if entry_price > 0 and exit_price > 0:
                if scalp.scalp_side == "long":
                    pnl = (exit_price - entry_price) * scalp.amount
                else:
                    pnl = (entry_price - exit_price) * scalp.amount
            self.risk.record_pnl(pnl)
            self.wick_scalper.close(symbol, pnl)
            self._log_trade(
                Signal(symbol=symbol, action=SignalAction.CLOSE, strategy="wick_scalp", reason="wick_stop_hit"),
                order,
                "wick_scalp_close",
                pnl,
            )
            return order
        return None

    # ------------------------------------------------------------------ #
    #  Stale loser detection
    # ------------------------------------------------------------------ #

    def has_stale_losers(self, positions: list[Position]) -> bool:
        """True if any non-pyramid position is losing AND older than half
        the short-term max hold time.  Used by the bot to reduce aggression
        for new entries so existing losers get resolved first."""
        threshold = self.settings.short_term_max_hold_minutes / 2
        now = datetime.now(UTC)
        pos_map = {p.symbol: p for p in positions}
        for sym, sp in self.scaler.active_positions.items():
            if sp.mode == ScaleMode.PYRAMID:
                continue
            age_min = (now - sp.created_at).total_seconds() / 60
            if age_min < threshold:
                continue
            pos = pos_map.get(sym)
            if pos and pos.pnl_pct <= 0:
                return True
        return False

    # ------------------------------------------------------------------ #
    #  Quick trade expiry
    # ------------------------------------------------------------------ #

    async def close_expired_quick_trades(self, active_signals: list[Signal]) -> list[Order]:
        """Close quick trades that exceeded max hold time -- BUT only losers.

        RIDE THE WINNERS: if the trade is in profit when time expires,
        don't close it. Let the trailing stop handle the exit. The time
        limit exists to CUT LOSERS that are going nowhere, not to cap
        winners that are still running.

        Also closes non-pyramid (WINNERS-mode) positions that have been
        open longer than short_term_max_hold_minutes and are still in a
        loss.  Pyramid positions are left alone — they recover via DCA.
        """
        closed: list[Order] = []
        now = datetime.now(UTC)
        positions = await self.exchange.fetch_positions()
        pos_map = {p.symbol: p for p in positions}

        for signal in active_signals:
            if not signal.quick_trade or not signal.max_hold_minutes:
                continue
            elapsed = (now - signal.timestamp).total_seconds() / 60
            if elapsed < signal.max_hold_minutes:
                continue

            pos = pos_map.get(signal.symbol)
            is_extreme = signal.strategy.startswith("extreme_")
            profit_floor = 0.0 if is_extreme else 1.0
            if pos and pos.pnl_pct > profit_floor:
                logger.info(
                    "Quick trade {} expired but in profit ({:+.1f}%) -- letting trail ride", signal.symbol, pos.pnl_pct
                )
                continue

            close_signal = Signal(
                symbol=signal.symbol,
                action=SignalAction.CLOSE,
                strategy=signal.strategy,
                reason="max_hold_time_exceeded",
                market_type=signal.market_type,
            )
            order = await self.execute_signal(close_signal)
            if order:
                closed.append(order)
                logger.info("Cut expired quick trade {} after {:.0f}m (not in profit)", signal.symbol, elapsed)

        closed_symbols = {o.symbol for o in closed}
        stale_max = self.settings.short_term_max_hold_minutes
        for sym, sp in list(self.scaler.active_positions.items()):
            if sym in closed_symbols or sp.mode == ScaleMode.PYRAMID:
                continue
            age_min = (now - sp.created_at).total_seconds() / 60
            if age_min < stale_max:
                continue
            pos = pos_map.get(sym)
            if pos and pos.pnl_pct > 1.0:
                continue
            close_signal = Signal(
                symbol=sym,
                action=SignalAction.CLOSE,
                strategy=sp.strategy,
                reason="short_term_max_hold_exceeded",
                market_type=sp.market_type,
            )
            order = await self.execute_signal(close_signal)
            if order:
                closed.append(order)
                logger.info(
                    "Cut stale short-term position {} after {:.0f}m (mode={}, not in profit)",
                    sym,
                    age_min,
                    sp.mode.value,
                )

        return closed

    # ------------------------------------------------------------------ #
    #  Logging
    # ------------------------------------------------------------------ #

    def _log_trade(self, signal: Signal, order: Order, action: str, pnl: float = 0.0) -> None:
        sp = self.scaler.get(signal.symbol)
        self._trade_log.append(
            {
                "timestamp": datetime.now(UTC).isoformat(),
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
            }
        )

    @property
    def trade_history(self) -> list[dict[str, Any]]:
        return list(self._trade_log)

    def set_structure_guard(self, symbol: str, level: float) -> None:
        """Update structure-based stop guard for an active main position."""
        self.trailing.set_structure_guard(symbol, level)
