from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp
from loguru import logger

from config.settings import Settings, get_settings
from core.exchange import BaseExchange, create_exchange
from core.extreme import ExtremeWatcher
from core.market_schedule import get_market_schedule
from core.models import Candle, MarketType, Signal, SignalAction
from core.models.order import Order
from core.models.signal import TickUrgency
from core.orders import OrderManager
from core.orders.scaler import ScaleMode
from core.patterns import PatternDetector, StructureAnalyzer
from core.risk import RiskManager
from core.risk.daily_target import DailyTargetTracker, DailyTier
from core.risk.market_filter import MarketQualityFilter
from db.models import TradeRecord
from intel import MarketCondition
from intel.market_intel import MarketRegime
from news import NewsItem
from notifications import NotificationType, Notifier
from shared.models import (
    AnalyticsSnapshot,
    BotDeploymentStatus,
    DeploymentLevel,
    ExtremeWatchlist,
    IntelSnapshot,
    SignalPriority,
    TradeProposal,
    TradeQueue,
)
from strategies import BUILTIN_STRATEGIES
from strategies.base import BaseStrategy
from validators import ValidationResult, get_validator
from volatility import SpikeEvent, VolatilityDetector

# PYRAMID (DCA in) is the DEFAULT for all strategies. Nobody can predict exact
# bottoms, and market makers deliberately wick through expected support to grab
# stop-loss liquidity. Instead of getting stopped out, we DCA into the wick.
# Only these scalp-only strategies use WINNERS mode (add to winners only):
SCALP_ONLY_STRATEGIES: set[str] = set()  # currently none -- everything pyramids


class TradingBot:
    """Main bot orchestrator.

    Default mode: PYRAMID (DCA in). Nobody can predict exact bottoms.
    Market makers wick through expected support to grab liquidity, then
    reverse. We embrace the wick as a DCA entry instead of getting stopped out.

    PYRAMID (default for all strategies):
      Start with $50 at low leverage -> let it go red -> DCA down into wicks ->
      avg entry improves -> price recovers -> raise leverage -> take partial
      profit (pull capital out) -> lock break-even -> ride the rest

    WINNERS (rare, for ultra-short scalps only):
      Start with $50 -> add when in profit -> trail -> break-even lock

    MEXC-specific:
    - Low-liq coins: only gambling-sized bets, self-managed stops
    - Lock break-even at +5%, then trail
    - After a good day, allow tiny yolo bets on trending shitcoins
    """

    def __init__(self, settings: Settings | None = None, daily_target_pct: float = 10.0):
        self.settings = settings or get_settings()
        self.exchange: BaseExchange = create_exchange(self.settings)
        self.risk = RiskManager(self.settings)
        self.orders = OrderManager(self.exchange, self.risk, self.settings)
        self.notifier = Notifier(self.settings)
        self.volatility = VolatilityDetector(self.settings)
        self.target = DailyTargetTracker(
            daily_target_pct=daily_target_pct,
            compound=True,
            aggressive_mode=self.settings.is_paper_local(),
            bot_data_dir=Path(self.settings.data_dir),
        )
        self.market_filter = MarketQualityFilter(
            min_liquidity_volume=self.settings.min_liquidity_volume,
        )

        self._multibot = bool(self.settings.bot_id)

        self.news = None
        self.intel = None
        self.scanner = None
        self._hub_intel: IntelSnapshot | None = None
        self._hub_analytics: AnalyticsSnapshot | None = None
        self._hub_trade_queue: TradeQueue | None = None
        self._hub_extreme_watchlist: ExtremeWatchlist | None = None
        self._hub_intel_age: float = 999999
        self._hub_queue_updates: dict[str, Any] = {"consumed": [], "rejected": {}}
        self._last_bot_status: BotDeploymentStatus | None = None
        self.pattern_detector = PatternDetector(
            structure=StructureAnalyzer(swing_lookback=5, zone_tolerance_pct=0.3),
            min_confidence=0.3,
        )

        self._strategies: list[BaseStrategy] = []
        self._active_signals: list[Signal] = []
        self._recent_news: list[NewsItem] = []
        self._whale_alerted: set[str] = set()
        self._open_trades: dict[str, TradeRecord] = {}
        self._pending_hub_acks: dict[str, dict[str, Any]] = {}
        self._strategy_stats: dict[str, dict[str, Any]] = {}
        self._running = False
        self._tick_interval = self.settings.tick_interval_idle
        self._status_interval = 300
        self._last_status_log: datetime | None = None
        self._started_at: datetime | None = None
        self._warmup_minutes = 3  # no queue processing for first N minutes
        self._hub_session: aiohttp.ClientSession | None = None
        self._hub_tasks: set[asyncio.Task[None]] = set()
        self._stop_check_lock = asyncio.Lock()
        self._monitor_task: asyncio.Task[None] | None = None

        self.extreme_watcher = ExtremeWatcher(self.exchange, self.settings)
        self._last_extreme_eval: float = 0.0
        self._available_symbols: set[str] = set()
        self._enabled: bool = True  # hub-controlled enable flag
        self._hub_enabled: bool = True  # latest value from hub report response
        self._validator = get_validator(self.settings.bot_style)

    # -- Strategy Management --

    def add_strategy(
        self, name: str, symbol: str, market_type: str = "spot", leverage: int = 0, **params: object
    ) -> None:
        if not self.settings.is_market_type_allowed(market_type):
            fallback = "spot" if self.settings.spot_allowed else None
            if fallback and market_type != fallback:
                logger.warning(
                    "Market type '{}' not allowed — falling back to '{}' for {} ({})",
                    market_type,
                    fallback,
                    name,
                    symbol,
                )
                market_type = fallback
                leverage = 1
            else:
                logger.warning(
                    "Skipping strategy '{}' for {} — market type '{}' not allowed (allowed: {})",
                    name,
                    symbol,
                    market_type,
                    self.settings.allowed_market_types,
                )
                return

        lev = leverage or (self.settings.default_leverage if market_type == "futures" else 1)
        cls = BUILTIN_STRATEGIES.get(name)
        if not cls:
            raise ValueError(f"Unknown strategy: {name}. Available: {list(BUILTIN_STRATEGIES.keys())}")
        strategy = cls(symbol=symbol, market_type=market_type, leverage=lev, **params)
        self._strategies.append(strategy)
        mode = "WINNERS" if name in SCALP_ONLY_STRATEGIES else "PYRAMID"
        logger.info("Added strategy '{}' for {} ({}, {}x, mode={})", name, symbol, market_type, lev, mode)

    def add_custom_strategy(self, strategy: BaseStrategy) -> None:
        self._strategies.append(strategy)
        logger.info("Added custom strategy '{}' for {}", strategy.name, strategy.symbol)

    # -- Main Loop --

    async def start(self) -> None:
        logger.info("=" * 60)
        logger.info("TRADE BORG v0.7.0")
        logger.info("Mode: {}", self.settings.trading_mode.upper())
        logger.info("Exchange: {} | Allowed: {}", self.settings.exchange, self.settings.allowed_market_types)
        logger.info("Daily target: {:.0f}% (compounding)", self.target.daily_target_pct)
        logger.info("Strategies: {}", len(self._strategies))
        logger.info("Leverage: {}x default", self.settings.default_leverage)
        logger.info("DEFAULT mode: PYRAMID (DCA into wicks, lever up on recovery)")
        logger.info(
            "Scalp-only: {} (these use WINNERS mode instead)", SCALP_ONLY_STRATEGIES or "none -- everything pyramids"
        )
        logger.info(
            "Initial risk: ${:.0f} | Notional cap: ${:.0f}K",
            self.settings.initial_risk_amount,
            self.settings.max_notional_position / 1000,
        )
        logger.info("Gambling budget: {}% for low-liq coins", self.settings.gambling_budget_pct)
        self._check_data_dir_size()
        logger.info("=" * 60)

        # Early activation check: if hub says we're disabled, start in lean idle
        if self._multibot and not await self._check_initial_activation():
            logger.info("Bot NOT activated by hub — starting in lean idle mode")
            self._running = True
            self._started_at = datetime.now(UTC)
            await self._lean_idle_loop()
            return

        await self._full_start()

    async def _check_initial_activation(self) -> bool:
        """Query the hub for this bot's enabled status before heavy init."""
        from config.bot_profiles import PROFILES_BY_ID

        bot_id = self.settings.bot_id or "default"
        profile = PROFILES_BY_ID.get(bot_id)
        if profile and profile.is_default:
            return True

        hub_url = self.settings.hub_url
        if not hub_url:
            return True

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as sess:
                payload = {"bot_id": bot_id, "bot_style": self.settings.bot_style}
                async with sess.post(f"{hub_url}/internal/report", json=payload) as resp:
                    data = await resp.json()
                    enabled = data.get("enabled", True)
                    self._hub_enabled = enabled
                    return enabled
        except Exception:
            return True

    async def _lean_idle_loop(self) -> None:
        """Minimal loop for inactive bots — no exchange, no strategies, no hub communication.

        Checks a local activation file on the shared data volume every 10s.
        The file is created by the hub's toggle endpoint or manually.
        When found, breaks out and runs _full_start() for full initialization.
        """
        activate_path = Path(self.settings.data_dir) / "activate"
        logger.info("Idle — watching {} for activation", activate_path)
        while self._running:
            try:
                if activate_path.exists():
                    logger.info("Activation file found — performing full initialization")
                    activate_path.unlink(missing_ok=True)
                    await self._full_start()
                    return

                await asyncio.sleep(10)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Lean idle error: {}", e)
                await asyncio.sleep(10)

    async def _full_start(self) -> None:
        """Full initialization: exchange connect, strategy load, enter main loop."""
        await self._recover_state_from_hub()

        schedule = get_market_schedule()
        schedule.configure(fmp_api_key=self.settings.fmp_api_key)
        await schedule.refresh_holidays()
        logger.info("Market schedule: {}", schedule.summary())

        await self.exchange.connect()

        self._available_symbols: set[str] = set()
        try:
            futures_syms = await self.exchange.get_available_symbols(MarketType.FUTURES)
            spot_syms = await self.exchange.get_available_symbols(MarketType.SPOT)
            # ccxt returns futures as "BTC/USDT:USDT"; normalize to "BTC/USDT"
            normalized = {s.split(":")[0] for s in futures_syms}
            normalized |= set(spot_syms)
            self._available_symbols = normalized
            logger.info("Published {} available symbols for {}", len(self._available_symbols), self.settings.exchange)
        except Exception as e:
            logger.warning("Could not publish exchange symbols: {}", e)

        await self.notifier.start()

        balance_map = await self.exchange.fetch_balance()
        balance = self.settings.cap_balance(balance_map.get("USDT", 0.0))
        self.risk.reset_daily(balance)
        self.target.reset_day(balance)

        projected = self.target.projected_balance
        if self.settings.session_budget > 0:
            logger.info("Session budget: ${:.2f} (exchange has ${:.2f})", balance, balance_map.get("USDT", 0.0))
        logger.info("Starting balance: {:.2f} USDT", balance)
        logger.info(
            "Projections if target hit daily -> 1w: {:.0f} | 1mo: {:.0f} | 3mo: {:.0f}",
            projected["1_week"],
            projected["1_month"],
            projected["3_months"],
        )

        self._running = True
        self._started_at = datetime.now(UTC)
        self._monitor_task = asyncio.create_task(self._fast_monitor_loop())
        await self._run_loop()

    async def stop(self) -> None:
        logger.info("Shutting down...")
        logger.info("Final status: {}", self.target.status_report())
        self._running = False
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._monitor_task
            self._monitor_task = None
        await self.extreme_watcher.stop()
        await self.notifier.stop()
        await self.exchange.disconnect()
        if self._hub_session:
            await self._hub_session.close()
            self._hub_session = None
        logger.info("Bot stopped")

    _HUB_POLL_INTERVAL = 5  # seconds — queue check cadence between full ticks

    async def _run_loop(self) -> None:
        while self._running:
            try:
                was_enabled = self._enabled
                self._enabled = self._check_enabled()

                if not was_enabled and self._enabled:
                    logger.info("Bot ENABLED by hub — resuming trading")

                if was_enabled and not self._enabled:
                    logger.info("Bot DISABLED by hub — winding down")
                    await self._wind_down()

                if self._enabled:
                    t0 = time.perf_counter()
                    await self._tick()
                    from web.metrics import record_event_loop_lag, record_tick

                    record_tick(time.perf_counter() - t0)
                    self._update_tick_interval()

                    remaining = self._tick_interval
                    while remaining > 0 and self._running and self._enabled:
                        sleep_time = min(self._HUB_POLL_INTERVAL, remaining)
                        loop_start = time.perf_counter()
                        await asyncio.sleep(sleep_time)
                        remaining -= sleep_time
                        record_event_loop_lag(time.perf_counter() - loop_start - sleep_time)
                        if remaining > 0 and self._running and self._enabled:
                            await self._quick_hub_check()
                else:
                    await self._idle_tick()
                    await asyncio.sleep(10)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Error in main loop: {}", e)
                await asyncio.sleep(10)

    async def _quick_hub_check(self) -> None:
        """Lightweight hub poll between full ticks — fetch queue proposal and process it."""
        hub_url = self.settings.hub_url
        if not hub_url or not self._multibot:
            return
        if not self._hub_session:
            self._hub_session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(force_close=True),
                timeout=aiohttp.ClientTimeout(total=10),
            )
        try:
            payload: dict[str, Any] = {
                "bot_id": self.settings.bot_id or "default",
                "bot_style": self.settings.bot_style,
            }
            if self._hub_queue_updates["consumed"] or self._hub_queue_updates["rejected"]:
                payload["queue_updates"] = self._hub_queue_updates
                self._hub_queue_updates = {"consumed": [], "rejected": {}}

            url = f"{hub_url.rstrip('/')}/internal/report"
            async with self._hub_session.post(url, json=payload) as resp:
                if resp.status != 200:
                    return
                body = await resp.json()
                if "enabled" in body:
                    self._hub_enabled = body["enabled"]
                if "trade_queue" in body:
                    self._hub_trade_queue = TradeQueue(**body["trade_queue"])
                for key in body.get("confirmed_keys", []):
                    self._pending_hub_acks.pop(key, None)
        except Exception as e:
            logger.warning("Quick hub check error: {}", e)
            return
        self._retry_pending_hub_trades()
        await self._process_trade_queue()

    def _check_enabled(self) -> bool:
        """Check hub-controlled enable flag (received via /internal/report response)."""
        return self._hub_enabled

    async def _idle_tick(self) -> None:
        """Minimal tick when disabled — report IDLE status so hub sees us."""
        status = BotDeploymentStatus(
            bot_id=self.settings.bot_id or "default",
            bot_style=self.settings.bot_style,
            exchange=self.settings.exchange.upper(),
            level=DeploymentLevel.IDLE,
            open_positions=0,
            max_positions=self.settings.effective_max_concurrent_positions,
            capacity_pct=100.0,
            daily_pnl_pct=0.0,
            daily_tier="idle",
            should_trade=False,
            manual_stop=False,
        )
        self._last_bot_status = status
        await self._report_dashboard_snapshot([])

    async def _wind_down(self) -> None:
        """Close all positions before going idle."""
        positions = await self.exchange.fetch_positions()
        active = [p for p in positions if p.amount > 0]
        if not active:
            logger.info("No open positions — going idle immediately")
            return
        logger.info("Winding down: closing {} positions", len(active))
        status = BotDeploymentStatus(
            bot_id=self.settings.bot_id or "default",
            bot_style=self.settings.bot_style,
            exchange=self.settings.exchange.upper(),
            level=DeploymentLevel.WINDING_DOWN,
            open_positions=len(active),
            max_positions=self.settings.effective_max_concurrent_positions,
            should_trade=False,
        )
        self._last_bot_status = status
        await self._close_all_positions("hub_disabled")

    async def _fast_monitor_loop(self) -> None:
        """Sub-second monitor for all time-critical actions.

        Runs every 1-2s, independent of the main tick interval. Handles:
        - CLOSE_ALL / STOP manual overrides (immediate)
        - Legendary day + reversal risk close (immediate)
        - Trailing stops, break-even, liquidation (via check_stops)
        - Expired quick trades
        - Extreme mover entries
        """
        while self._running:
            try:
                # --- Emergency overrides (always checked, even with no positions) ---

                if self.target.manual_close_all:
                    logger.critical("CLOSE_ALL detected (fast loop) — closing all positions NOW")
                    await self._close_all_positions("Manual CLOSE_ALL file")
                    self.target.clear_close_all()
                    await asyncio.sleep(2)
                    continue

                if self.target.tier.value == "legendary":
                    intel = self._read_shared_intel()
                    reversal_risk = intel is not None and intel.should_reduce_exposure
                    should_close, reason = self.target.should_close_all(reversal_risk)
                    if should_close:
                        logger.critical("LEGENDARY + REVERSAL (fast loop) — closing all: {}", reason)
                        await self._close_all_positions(reason)
                        await self.notifier.send(
                            NotificationType.DAILY_SUMMARY,
                            f"LEGENDARY DAY CLOSED: {self.target.todays_pnl_pct:+.1f}%",
                            reason,
                        )
                        await asyncio.sleep(2)
                        continue

                # --- Position management (only when positions exist) ---

                if not self.orders.trailing.active_stops and not self.orders.wick_scalper.active_scalps:
                    await asyncio.sleep(2)
                    continue

                async with self._stop_check_lock:
                    closed = await self.orders.check_stops()
                    for order in closed:
                        sp = self.orders.scaler.get(order.symbol)
                        stashed = self.orders._closed_scalers.get(order.symbol, [])
                        sp = sp or (stashed[0] if stashed else None)
                        entry_price = sp.avg_entry_price if sp and sp.avg_entry_price > 0 else 0
                        exit_price = order.average_price or order.price or 0
                        pnl = 0.0
                        if entry_price > 0 and exit_price > 0:
                            pos_side = (
                                sp.side
                                if sp
                                else (
                                    "long"
                                    if (order.side.value if hasattr(order.side, "value") else str(order.side))
                                    in ("buy", "long")
                                    else "short"
                                )
                            )
                            if pos_side == "long":
                                pnl = (exit_price - entry_price) * (order.filled or order.amount)
                            else:
                                pnl = (entry_price - exit_price) * (order.filled or order.amount)
                        await self.notifier.alert_stop_loss(order.symbol, entry_price, exit_price, pnl)
                        self._log_closed_trade(order, "stop")
                        self.target.record_trade(realized_pnl=pnl)

                    expired = await self.orders.close_expired_quick_trades(self._active_signals)
                    for order in expired:
                        self._log_closed_trade(order, "expired")
                        realized_pnl = self._calc_realized_pnl(order)
                        self.target.record_trade(realized_pnl=realized_pnl)

                extreme_signals = self.extreme_watcher.drain_signals()
                for sig in extreme_signals:
                    if not self.target.should_trade():
                        break
                    if len(self.orders.scaler.active_positions) >= self.settings.effective_max_concurrent_positions:
                        break
                    try:
                        await self._process_signal(sig, pyramid=False)
                        logger.info("Extreme entry: {} {} via {}", sig.action.value, sig.symbol, sig.strategy)
                    except Exception as e:
                        logger.error("Extreme signal error for {}: {}", sig.symbol, e)

                await asyncio.sleep(self.settings.tick_interval_scalp)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Fast monitor error: {}", e)
                await asyncio.sleep(2)

    def _update_tick_interval(self) -> None:
        """Adapt tick speed based on the most urgent open position.

        Priority (fastest wins): scalp (1s) > active (60s) > swing (300s) > idle (60s).
        """
        urgencies: set[TickUrgency] = set()
        for s in self._active_signals:
            urgencies.add(s.tick_urgency)
        if self.orders.wick_scalper.active_scalps:
            urgencies.add(TickUrgency.SCALP)

        if TickUrgency.SCALP in urgencies:
            new_interval = self.settings.tick_interval_scalp
        elif TickUrgency.ACTIVE in urgencies or self.orders.trailing.active_stops:
            new_interval = self.settings.tick_interval_active
        elif TickUrgency.SWING in urgencies:
            new_interval = self.settings.tick_interval_swing
        else:
            new_interval = self.settings.tick_interval_idle

        if new_interval != self._tick_interval:
            logger.info("Tick interval: {}s → {}s", self._tick_interval, new_interval)
            self._tick_interval = new_interval

    async def _tick(self) -> None:

        await self._fetch_intel()

        balance_map = await self.exchange.fetch_balance()
        raw_balance = balance_map.get("USDT", 0.0)
        balance = self.settings.cap_balance(raw_balance)

        positions = await self.exchange.fetch_positions()
        self.target.update_balance(raw_balance)

        pyramid_pnl = sum(
            p.unrealized_pnl
            for p in positions
            if p.amount > 0 and (sp := self.orders.scaler.get(p.symbol)) and sp.mode == ScaleMode.PYRAMID
        )
        self.target.update_pyramid_unrealized(pyramid_pnl)

        logger.debug(
            "=== TICK === bal=${:.2f} pnl={:+.2f}% tier={} aggr={:.2f} trade={}",
            balance,
            self.target.todays_pnl_pct,
            self.target.tier.value,
            self.target.aggression_multiplier(),
            self.target.should_trade(),
        )

        # 0. Manual override (fast loop checks every ~1-2s; this is a fallback)
        if self.target.manual_close_all:
            logger.critical("CLOSE_ALL detected — closing all positions NOW")
            await self._close_all_positions("Manual CLOSE_ALL file")
            self.target.clear_close_all()
            return

        # 1. Check trailing stops and liquidation (fast monitor handles sub-second
        #    checks; this is the fallback that also covers liquidation risk)
        async with self._stop_check_lock:
            closed = await self.orders.check_stops()
            for order in closed:
                sp = self.orders.scaler.get(order.symbol)
                stashed = self.orders._closed_scalers.get(order.symbol, [])
                sp = sp or (stashed[0] if stashed else None)
                entry_price = sp.avg_entry_price if sp and sp.avg_entry_price > 0 else 0
                exit_price = order.average_price or order.price or 0
                pnl = 0.0
                if entry_price > 0 and exit_price > 0:
                    pos_side = (
                        sp.side
                        if sp
                        else (
                            "long"
                            if (order.side.value if hasattr(order.side, "value") else str(order.side))
                            in ("buy", "long")
                            else "short"
                        )
                    )
                    if pos_side == "long":
                        pnl = (exit_price - entry_price) * (order.filled or order.amount)
                    else:
                        pnl = (entry_price - exit_price) * (order.filled or order.amount)
                await self.notifier.alert_stop_loss(order.symbol, entry_price, exit_price, pnl)
                self._log_closed_trade(order, "stop")
                self.target.record_trade(realized_pnl=pnl)

        await asyncio.sleep(0)  # yield to event loop (dashboard, etc.)

        # 2. Scale into positions (both WINNERS adds and PYRAMID DCA-downs)
        scale_orders = await self.orders.try_scale_in()
        if scale_orders:
            logger.info("Scaled into {} position(s) this tick", len(scale_orders))

        await asyncio.sleep(0)

        # 3. PYRAMID: check if any positions are ready for leverage raise
        levered = await self.orders.try_lever_up()
        if levered:
            for sym in levered:
                sp = self.orders.scaler.get(sym)
                lev_info = f"{sp.initial_leverage}x -> {sp.current_leverage}x" if sp else "?"
                logger.info("Leverage raised on {} ({})", sym, lev_info)

        # 4. PYRAMID: take partial profit on levered-up positions
        #    When daily target not yet secured, take profits more aggressively
        pt_aggression = self.target.profit_taking_aggression
        self.orders.trailing.set_profit_taking_mode(pt_aggression)
        partials = await self.orders.try_partial_take(pt_aggression)
        if partials:
            logger.info("Partial profit taken on {} position(s) (aggr={:.1f}x)", len(partials), pt_aggression)

        # 5. Whale position alert: $100K+ notional at 20%+ profit
        await self._check_whale_positions()

        # 6. Wick scalps: counter-trade wicks while PYRAMID DCA-s
        try:
            wick_orders = await self.orders.try_wick_scalps()
            if wick_orders:
                logger.info("Wick scalp: {} order(s) this tick", len(wick_orders))
        except Exception as e:
            logger.error("Wick scalp error: {}", e)

        # 7. Close expired quick trades
        expired = await self.orders.close_expired_quick_trades(self._active_signals)
        for order in expired:
            self._log_closed_trade(order, "expired")
            realized_pnl = self._calc_realized_pnl(order)
            self.target.record_trade(realized_pnl=realized_pnl)

        await asyncio.sleep(0)

        # 8. Process trade queue (CRITICAL -> DAILY -> SWING)
        await self._process_trade_queue()

        # 9. Read market intelligence from hub (no local fallback)
        intel_condition = self._read_shared_intel()

        # 10. Legendary day check (fast loop handles close; tick sends ride email)
        if self.target.tier.value == "legendary":
            reversal_risk = intel_condition is not None and intel_condition.should_reduce_exposure
            should_close, reason = self.target.should_close_all(reversal_risk)
            if should_close:
                logger.critical("LEGENDARY + REVERSAL (tick fallback) — closing all: {}", reason)
                await self._close_all_positions(reason)
                await self.notifier.send(
                    NotificationType.DAILY_SUMMARY,
                    f"LEGENDARY DAY CLOSED: {self.target.todays_pnl_pct:+.1f}%",
                    reason,
                )
                return

            if not self.target.legendary_email_sent:
                ride_reason = self.target.legendary_ride_reason("Intel via hub")
                logger.info("LEGENDARY DAY — letting it ride, emailing owner")
                await self.notifier.send(
                    NotificationType.DAILY_SUMMARY,
                    f"LEGENDARY DAY RIDING: {self.target.todays_pnl_pct:+.1f}% — positions open",
                    ride_reason,
                )

        # 11. Trading gates
        allow_new_entries = self.target.should_trade()
        base_aggression = self.target.aggression_multiplier()
        aggression = base_aggression
        allow_gambling = self.target.todays_pnl_pct > 10

        if intel_condition:
            aggression *= intel_condition.position_size_multiplier
            if intel_condition.should_reduce_exposure:
                aggression *= 0.7

            logger.debug(
                "Gates: allow={} tier={} base_aggr={:.2f} intel_mult={:.2f} "
                "reduce={} regime={} final_aggr={:.3f} gambling={}",
                allow_new_entries,
                self.target.tier.value,
                base_aggression,
                intel_condition.position_size_multiplier,
                intel_condition.should_reduce_exposure,
                intel_condition.regime.value,
                aggression,
                allow_gambling,
            )
        else:
            logger.debug(
                "Gates: allow={} tier={} aggr={:.2f} gambling={} (no intel)",
                allow_new_entries,
                self.target.tier.value,
                aggression,
                allow_gambling,
            )

        await asyncio.sleep(0)

        # 12. Fetch candles for held positions only (hedge + volatility checks)
        # Strategy execution is now centralized in the hub's SignalGenerator.
        # New entries come from the trade queue (step 8).
        candles_map: dict[str, list[Candle]] = {}
        positions = await self.exchange.fetch_positions()

        if self.orders.has_stale_losers(positions):
            aggression *= 0.5
            logger.info("Stale short-term losers detected — halving aggression to {:.3f}", aggression)

        held_symbols = [p.symbol for p in positions if p.amount > 0]
        for sym in held_symbols:
            try:
                candles = await self.exchange.fetch_candles(sym, "1m", limit=200)
                ticker = await self.exchange.fetch_ticker(sym)
                candles_map[sym] = candles

                spike = self.volatility.update(ticker)
                if spike:
                    await self._handle_spike(spike)
            except Exception as e:
                logger.warning("Candle fetch for {} failed: {}", sym, e)
            await asyncio.sleep(0)

        # 13. Hedge check: open counter-positions on reversal signals
        if self.settings.hedge_enabled:
            try:
                hedges = await self.orders.try_hedge(candles_map)
                if hedges:
                    logger.info("Opened {} hedge position(s)", len(hedges))
            except Exception as e:
                logger.error("Hedge tick error: {}", e)

        # 13.5 Extreme mover evaluation (every ~30s)
        now_mono = time.monotonic()
        if self.settings.extreme_enabled and now_mono - self._last_extreme_eval >= self.settings.extreme_eval_interval:
            try:
                await self._evaluate_extreme_candidates()
                self._last_extreme_eval = now_mono
            except Exception as e:
                logger.error("Extreme eval error: {}", e)

        # 14. Write deployment status for monitor service
        try:
            await self._write_deployment_status()
        except Exception as e:
            logger.error("Failed to write deployment status: {}", e, exc_info=True)

        # 15. Status + daily reset
        await self._log_status()
        await self._check_daily_reset()

    # -- Trade Queue Consumer (advisory, never forced) -- #

    MAX_QUEUE_EXECUTIONS_PER_TICK = 1
    _PENDING_HUB_ACKS_MAX = 200

    async def _process_trade_queue(self) -> None:
        """Consume proposals from the monitor's trade queue.

        The queue is strictly advisory — the bot only acts when it has
        genuine spare capacity, budget, and secured positions.  A
        successful day means existing trades are protected; new queue
        items are not forced just because they exist.

        Safeguards against rapid balance drain on boot:
        - Warmup: no queue processing for the first N minutes
        - Per-tick cap: at most MAX_QUEUE_EXECUTIONS_PER_TICK per tick
        """
        if self._started_at:
            uptime_min = (datetime.now(UTC) - self._started_at).total_seconds() / 60
            if uptime_min < self._warmup_minutes:
                logger.debug(
                    "Queue: warmup ({:.0f}s / {}m) — skipping",
                    uptime_min * 60,
                    self._warmup_minutes,
                )
                return

        queue = self._hub_trade_queue
        if not queue:
            return

        if queue.pending_count == 0:
            return

        positions = await self.exchange.fetch_positions()
        active_count = sum(1 for p in positions if p.amount > 0)
        max_pos = self.settings.effective_max_concurrent_positions
        free_slots = max(0, max_pos - active_count)

        tier = self.target.tier
        allow_new = self.target.should_trade()
        aggression = self.target.aggression_multiplier()
        pnl_pct = self.target.todays_pnl_pct

        avg_health = 0.0
        all_in_profit = False
        if active_count > 0:
            pnls = [p.pnl_pct for p in positions if p.amount > 0]
            avg_health = sum(pnls) / len(pnls)
            all_in_profit = all(pnl >= 0 for pnl in pnls)
        positions_secured = active_count == 0 or avg_health >= 0

        if all_in_profit and active_count > 0 and not self.target.manual_stop:
            allow_new = True
            aggression = max(aggression, 1.0)
            logger.info(
                "All {} positions in profit — all-in mode: aggression={:.2f}, free_slots={}, available for new entries",
                active_count,
                aggression,
                free_slots,
            )

        executed = 0
        tick_limit = self.MAX_QUEUE_EXECUTIONS_PER_TICK
        consumed_ids: list[str] = []
        rejected: dict[str, str] = {}

        my_exchange = self.settings.exchange.upper()

        # --- CRITICAL: time-sensitive, but still respect capacity + tick cap ---
        for proposal in queue.get_actionable(SignalPriority.CRITICAL):
            if executed >= tick_limit:
                break
            if not self._symbol_available(proposal.symbol) or my_exchange in proposal.unsupported_exchanges:
                queue.mark_rejected(proposal.id, f"symbol not on {my_exchange}")
                rejected[proposal.id] = f"symbol not on {my_exchange}"
                continue
            if not self.settings.is_market_type_allowed(proposal.market_type):
                queue.mark_rejected(proposal.id, f"market type '{proposal.market_type}' not allowed")
                rejected[proposal.id] = f"market type '{proposal.market_type}' not allowed"
                continue
            if free_slots <= 0:
                queue.mark_rejected(proposal.id, "no free slots")
                rejected[proposal.id] = "no free slots"
                continue
            if not allow_new:
                queue.mark_rejected(proposal.id, f"tier={tier.value} — not trading")
                rejected[proposal.id] = f"tier={tier.value} — not trading"
                continue
            if proposal.strength * aggression < 0.2:
                queue.mark_rejected(proposal.id, "strength too low after aggression")
                rejected[proposal.id] = "strength too low after aggression"
                continue

            ok = await self._execute_proposal(proposal, aggression)
            if ok:
                queue.mark_consumed(proposal.id)
                consumed_ids.append(proposal.id)
                free_slots -= 1
                executed += 1
            else:
                queue.mark_rejected(proposal.id, "execution failed")
                rejected[proposal.id] = "execution failed"

        # --- DAILY: need at least 1 free slot and tier allows trading ---
        # All positions green = capital is safe, always allow new entries
        budget_ok = (
            all_in_profit
            or tier in (DailyTier.BUILDING, DailyTier.LOSING)
            or (tier == DailyTier.STRONG and positions_secured)
        )

        if allow_new and free_slots >= 1 and budget_ok:
            for proposal in queue.get_actionable(SignalPriority.DAILY):
                if executed >= tick_limit:
                    break
                if not self._symbol_available(proposal.symbol) or my_exchange in proposal.unsupported_exchanges:
                    queue.mark_rejected(proposal.id, f"symbol not on {my_exchange}")
                    rejected[proposal.id] = f"symbol not on {my_exchange}"
                    continue
                if not self.settings.is_market_type_allowed(proposal.market_type):
                    queue.mark_rejected(proposal.id, f"market type '{proposal.market_type}' not allowed")
                    rejected[proposal.id] = f"market type '{proposal.market_type}' not allowed"
                    continue
                if free_slots <= 0:
                    break
                if proposal.strength * aggression < 0.3:
                    queue.mark_rejected(proposal.id, "daily: strength too low")
                    rejected[proposal.id] = "daily: strength too low"
                    continue

                ok = await self._execute_proposal(proposal, aggression)
                if ok:
                    queue.mark_consumed(proposal.id)
                    consumed_ids.append(proposal.id)
                    free_slots -= 1
                    executed += 1
                else:
                    queue.mark_rejected(proposal.id, "execution failed")
                    rejected[proposal.id] = "execution failed"

        # --- SWING: need at least 1 free slot and not deeply in the red ---
        swing_ok = free_slots >= 1 and (all_in_profit or pnl_pct >= -3.0)

        if allow_new and swing_ok:
            for proposal in queue.get_actionable(SignalPriority.SWING):
                if executed >= tick_limit:
                    break
                if not self._symbol_available(proposal.symbol) or my_exchange in proposal.unsupported_exchanges:
                    queue.mark_rejected(proposal.id, f"symbol not on {my_exchange}")
                    rejected[proposal.id] = f"symbol not on {my_exchange}"
                    continue
                if not self.settings.is_market_type_allowed(proposal.market_type):
                    queue.mark_rejected(proposal.id, f"market type '{proposal.market_type}' not allowed")
                    rejected[proposal.id] = f"market type '{proposal.market_type}' not allowed"
                    continue
                if free_slots <= 0:
                    break
                if proposal.strength * aggression < 0.3:
                    continue

                ok = await self._execute_swing_proposal(proposal, aggression)
                if ok:
                    queue.mark_consumed(proposal.id)
                    consumed_ids.append(proposal.id)
                    free_slots -= 1
                    executed += 1
                else:
                    queue.mark_rejected(proposal.id, "swing execution failed")
                    rejected[proposal.id] = "swing execution failed"

        if consumed_ids or rejected:
            self._hub_queue_updates["consumed"].extend(consumed_ids)
            self._hub_queue_updates["rejected"].update(rejected)

        if executed > 0:
            logger.info(
                "Queue: executed {} proposal(s) this tick (remaining: C={} D={} S={})",
                executed,
                len(queue.get_actionable(SignalPriority.CRITICAL)),
                len(queue.get_actionable(SignalPriority.DAILY)),
                len(queue.get_actionable(SignalPriority.SWING)),
            )

    async def _validate_proposal(self, proposal: TradeProposal) -> ValidationResult:
        """Run the bot-type-specific validator on a single proposal."""
        try:
            candles = await self.exchange.fetch_candles(proposal.symbol, "1m", limit=50)
            ticker = await self.exchange.fetch_ticker(proposal.symbol)
        except Exception as e:
            return ValidationResult(valid=False, reason=f"data fetch failed: {e}")

        return self._validator.validate(candles, ticker, proposal.side, proposal.strategy)

    async def _execute_proposal(self, proposal: TradeProposal, aggression: float) -> bool:
        """Convert a queue proposal into a trading signal and execute it."""
        my_exchange = self.settings.exchange.upper()
        if my_exchange in proposal.unsupported_exchanges:
            logger.debug("Queue skip {}: tagged not-for-{}", proposal.symbol, my_exchange)
            return False
        if not self._symbol_available(proposal.symbol):
            logger.debug("Queue skip {}: not on {}", proposal.symbol, my_exchange)
            return False

        result = await self._validate_proposal(proposal)
        if not result.valid:
            logger.info("Queue reject {}: validator says '{}' ({})", proposal.symbol, result.reason, proposal.strategy)
            return False

        try:
            ticker = await self.exchange.fetch_ticker(proposal.symbol)
            price = ticker.last
        except Exception as e:
            logger.error("Queue: can't fetch price for {}: {}", proposal.symbol, e)
            return False

        action = SignalAction.BUY if proposal.side == "long" else SignalAction.SELL
        sig = Signal(
            symbol=proposal.symbol,
            action=action,
            strength=min(1.0, proposal.strength * aggression),
            strategy=proposal.strategy,
            reason=f"[QUEUE/{proposal.priority.value}] {proposal.reason}",
            suggested_price=price,
            market_type=proposal.market_type,
            leverage=proposal.leverage,
            quick_trade=proposal.quick_trade,
            max_hold_minutes=proposal.max_hold_minutes or None,
            tick_urgency=TickUrgency(proposal.tick_urgency)
            if proposal.tick_urgency in {e.value for e in TickUrgency}
            else TickUrgency.ACTIVE,
        )

        logger.info(
            "Queue exec [{}/{}]: {} {} {} (str={:.2f})",
            proposal.priority.value,
            proposal.strategy,
            proposal.side.upper(),
            proposal.symbol,
            proposal.reason[:60],
            sig.strength,
        )

        use_pyramid = not proposal.quick_trade
        try:
            await self._process_signal(sig, pyramid=use_pyramid)
            return True
        except Exception as e:
            logger.error("Queue execution error for {}: {}", proposal.symbol, e)
            return False

    async def _execute_swing_proposal(self, proposal: TradeProposal, aggression: float) -> bool:
        """Execute a swing proposal using its entry plan.

        Swing proposals include a full plan: entry zone, DCA levels,
        stop loss, take profit targets, leverage ramp.  The bot places
        the initial entry and lets PYRAMID mode handle the rest.
        """
        my_exchange = self.settings.exchange.upper()
        if my_exchange in proposal.unsupported_exchanges:
            logger.debug("Swing skip {}: tagged not-for-{}", proposal.symbol, my_exchange)
            return False
        if not self._symbol_available(proposal.symbol):
            logger.debug("Swing skip {}: not on {}", proposal.symbol, my_exchange)
            return False

        result = await self._validate_proposal(proposal)
        if not result.valid:
            logger.info("Swing reject {}: validator says '{}' ({})", proposal.symbol, result.reason, proposal.strategy)
            return False

        plan = proposal.entry_plan
        strength = min(1.0, proposal.strength * aggression)

        try:
            ticker = await self.exchange.fetch_ticker(proposal.symbol)
            price = ticker.last
        except Exception as e:
            logger.error("Queue: can't fetch price for {}: {}", proposal.symbol, e)
            return False

        action = SignalAction.BUY if proposal.side == "long" else SignalAction.SELL
        leverage = plan.initial_leverage if plan else proposal.leverage

        sig = Signal(
            symbol=proposal.symbol,
            action=action,
            strength=strength,
            strategy=proposal.strategy,
            reason=f"[QUEUE/swing] {proposal.reason}",
            suggested_price=price,
            market_type=proposal.market_type,
            leverage=leverage,
            suggested_stop_loss=plan.stop_loss if plan and plan.stop_loss else None,
            suggested_take_profit=(plan.take_profit_targets[0] if plan and plan.take_profit_targets else None),
        )

        plan_notes = plan.notes if plan else "no plan"
        logger.info(
            "Swing entry [{}/{}]: {} {} (lev={}x) — {}",
            proposal.strategy,
            proposal.symbol,
            proposal.side.upper(),
            proposal.symbol,
            leverage,
            plan_notes[:80],
        )

        try:
            await self._process_signal(sig, pyramid=True)
            return True
        except Exception as e:
            logger.error("Swing execution error for {}: {}", proposal.symbol, e)
            return False

    def _check_data_dir_size(self) -> None:
        """Log data directory size on startup. Warn if > 10 MB."""
        data_path = Path(self.settings.data_dir)
        if not data_path.exists():
            return
        total = sum(f.stat().st_size for f in data_path.rglob("*") if f.is_file())
        size_mb = total / (1024 * 1024)
        if size_mb > 10:
            logger.warning("Data dir {} is {:.1f} MB — consider cleanup", data_path, size_mb)
        else:
            logger.info("Data dir: {:.2f} MB", size_mb)

    def _log_opened_trade(self, signal: Signal, order: Order, *, low_liquidity: bool = False) -> None:
        """Record a trade open in memory and push to hub."""
        try:
            now = datetime.now(UTC)
            sp = self.orders.scaler.get(signal.symbol)
            hub_intel = self._hub_intel

            record = TradeRecord(
                symbol=signal.symbol,
                side=order.side.value if hasattr(order.side, "value") else str(order.side),
                strategy=signal.strategy,
                action="open",
                scale_mode=sp.mode.value if sp else "",
                entry_price=order.average_price or order.price or 0,
                amount=order.filled or order.amount,
                leverage=order.leverage or (sp.current_leverage if sp else 1),
                was_low_liquidity=low_liquidity or (sp.low_liquidity if sp else False),
                market_regime=hub_intel.regime if hub_intel else "",
                fear_greed=hub_intel.fear_greed if hub_intel else 50,
                daily_tier=self.target.tier.value,
                daily_pnl_at_entry=self.target.todays_pnl_pct,
                signal_strength=signal.strength,
                hour_utc=now.hour,
                day_of_week=now.weekday(),
                opened_at=now.isoformat(),
            )
            self._open_trades[signal.symbol] = record
            logger.debug("Opened trade for {} (in-memory)", signal.symbol)
            task = asyncio.ensure_future(self._push_trade_to_hub(record))
            self._hub_tasks.add(task)
            task.add_done_callback(self._hub_tasks.discard)
        except Exception as e:
            logger.error("Failed to log opened trade: {}", e)

    def _calc_realized_pnl(self, order: Order) -> float:
        """Extract the realized PnL from a close order for deposit detection."""
        try:
            sp = self.orders.scaler.get(order.symbol)
            if not sp:
                stashed = self.orders._closed_scalers.get(order.symbol, [])
                sp = stashed[0] if stashed else None
            exit_price = order.average_price or order.price or 0
            entry_price = sp.avg_entry_price if sp and sp.avg_entry_price > 0 else exit_price
            amount = order.filled or order.amount
            if entry_price > 0 and exit_price > 0:
                order_side = order.side.value if hasattr(order.side, "value") else str(order.side)
                pos_side = sp.side if sp else ("long" if order_side in ("buy", "long") else "short")
                if pos_side == "long":
                    return (exit_price - entry_price) * amount
                return (entry_price - exit_price) * amount
        except Exception:
            pass
        return 0.0

    def _log_closed_trade(self, order: Order, close_reason: str = "") -> None:
        """Record a trade close in memory and push to hub."""
        try:
            now = datetime.now(UTC)
            sp = self.orders.scaler.get(order.symbol)
            if not sp:
                stashed = self.orders._closed_scalers.get(order.symbol, [])
                sp = stashed.pop(0) if stashed else None
            hub_intel = self._hub_intel

            exit_price = order.average_price or order.price or 0
            entry_price = sp.avg_entry_price if sp and sp.avg_entry_price > 0 else exit_price
            amount = order.filled or order.amount
            leverage = order.leverage or (sp.current_leverage if sp else 1)
            order_side = order.side.value if hasattr(order.side, "value") else str(order.side)
            pos_side = sp.side if sp else ("long" if order_side in ("buy", "long") else "short")

            pnl_usd = 0.0
            pnl_pct = 0.0
            if entry_price > 0 and exit_price > 0:
                if pos_side == "long":
                    pnl_usd = (exit_price - entry_price) * amount
                    pnl_pct = (exit_price - entry_price) / entry_price * 100
                else:
                    pnl_usd = (entry_price - exit_price) * amount
                    pnl_pct = (entry_price - exit_price) / entry_price * 100

            open_rec = self._open_trades.pop(order.symbol, None)
            opened_at = open_rec.opened_at if open_rec else ""
            hold_minutes = 0.0
            if opened_at:
                try:
                    from datetime import datetime as dt

                    opened_dt = dt.fromisoformat(opened_at)
                    hold_minutes = (now - opened_dt).total_seconds() / 60
                except Exception:
                    pass

            record = TradeRecord(
                symbol=order.symbol,
                side=pos_side,
                strategy=order.strategy or close_reason,
                action="close",
                scale_mode=sp.mode.value if sp else "",
                entry_price=entry_price,
                exit_price=exit_price,
                amount=amount,
                leverage=leverage,
                pnl_usd=pnl_usd,
                pnl_pct=pnl_pct,
                is_winner=pnl_usd > 0,
                hold_minutes=hold_minutes,
                dca_count=sp.adds if sp else 0,
                was_quick_trade=False,
                was_low_liquidity=sp.low_liquidity if sp else False,
                market_regime=hub_intel.regime if hub_intel else "",
                fear_greed=hub_intel.fear_greed if hub_intel else 50,
                daily_tier=self.target.tier.value,
                daily_pnl_at_entry=self.target.todays_pnl_pct,
                signal_strength=0,
                hour_utc=now.hour,
                day_of_week=now.weekday(),
                opened_at=opened_at,
                closed_at=now.isoformat(),
            )

            self._update_strategy_stats(record)
            logger.debug("Closed trade for {} PnL={:.2f} (in-memory)", order.symbol, pnl_usd)

            task = asyncio.ensure_future(self._push_trade_to_hub(record))
            self._hub_tasks.add(task)
            task.add_done_callback(self._hub_tasks.discard)
            self._whale_alerted.discard(order.symbol)
        except Exception as e:
            logger.error("Failed to log closed trade: {}", e)

    async def _close_all_positions(self, reason: str) -> None:
        """Emergency close all open positions."""
        positions = await self.exchange.fetch_positions()
        for pos in positions:
            if pos.amount <= 0:
                continue
            signal = Signal(
                symbol=pos.symbol,
                action=SignalAction.CLOSE,
                strategy="manual_override",
                reason=reason,
                market_type=pos.market_type,
            )
            try:
                order = await self.orders.execute_signal(signal)
                if order:
                    self._log_closed_trade(order, reason)
                    realized_pnl = self._calc_realized_pnl(order)
                    self.target.record_trade(realized_pnl=realized_pnl)
                logger.info("Closed {} ({})", pos.symbol, reason)
            except Exception as e:
                logger.error("Failed to close {}: {}", pos.symbol, e)

    # -- Whale position alerts -- #

    WHALE_NOTIONAL_THRESHOLD = 100_000.0
    WHALE_PROFIT_PCT_THRESHOLD = 20.0

    async def _check_whale_positions(self) -> None:
        """Alert once per position when it hits $100K+ notional AND 20%+ profit."""
        positions = await self.exchange.fetch_positions()
        prices = {p.symbol: p.current_price for p in positions if p.amount > 0}

        for sym, sp in self.orders.scaler.active_positions.items():
            if sym in self._whale_alerted:
                continue

            price = prices.get(sym, 0)
            if price <= 0:
                continue

            notional = sp.current_size * price * sp.current_leverage
            profit_pct = sp._current_profit_pct(price)

            if notional >= self.WHALE_NOTIONAL_THRESHOLD and profit_pct >= self.WHALE_PROFIT_PCT_THRESHOLD:
                profit_usd = sp.current_size * (price - sp.avg_entry_price)
                if sp.side == "short":
                    profit_usd = sp.current_size * (sp.avg_entry_price - price)

                dashboard_url = f"http://localhost:{self.settings.dashboard_port}"
                await self.notifier.alert_whale_position(
                    symbol=sym,
                    notional=notional,
                    profit_pct=profit_pct,
                    profit_usd=profit_usd,
                    entry_price=sp.avg_entry_price,
                    current_price=price,
                    leverage=sp.current_leverage,
                    adds=sp.adds,
                    dashboard_url=dashboard_url,
                )
                self._whale_alerted.add(sym)
                logger.info("WHALE ALERT sent for {} -- ${:.0f} notional at +{:.1f}%", sym, notional, profit_pct)

    # -- Shared State (inter-process communication) -- #

    async def _write_deployment_status(self) -> None:
        """Tell the monitor service how busy we are so it adjusts intensity."""
        positions = await self.exchange.fetch_positions()
        active = [p for p in positions if p.amount > 0]

        avg_health = 0.0
        worst = 0.0
        if active:
            pnls = [p.pnl_pct for p in active]
            avg_health = sum(pnls) / len(pnls)
            worst = min(pnls)

        # Determine deployment level
        capacity = 1.0 - (len(active) / max(self.settings.effective_max_concurrent_positions, 1))
        positions_healthy = avg_health > -1.0

        if len(active) == 0:
            level = DeploymentLevel.HUNTING
        elif worst < -5.0 or (len(active) > 0 and avg_health < -2.0):
            level = DeploymentLevel.STRESSED
        elif capacity <= 0.1 and positions_healthy:
            level = DeploymentLevel.DEPLOYED
        else:
            level = DeploymentLevel.ACTIVE

        status = BotDeploymentStatus(
            bot_id=self.settings.bot_id or "default",
            bot_style=self.settings.bot_style,
            exchange=self.settings.exchange.upper(),
            level=level,
            open_positions=len(active),
            max_positions=self.settings.effective_max_concurrent_positions,
            capacity_pct=capacity * 100,
            daily_pnl_pct=self.target.todays_pnl_pct,
            daily_tier=self.target.tier.value,
            avg_position_health=avg_health,
            worst_position_pnl=worst,
            should_trade=self.target.should_trade(),
            manual_stop=self.target.manual_stop,
        )
        self._last_bot_status = status
        await self._report_dashboard_snapshot(active)

    async def _report_dashboard_snapshot(self, active_positions: list[Any]) -> None:
        """Build and send dashboard snapshot to the central hub."""
        import time as _time

        pos_list = []
        for pos in active_positions:
            ts = self.orders.trailing.active_stops.get(pos.symbol)
            sp = self.orders.scaler.get(pos.symbol)
            pos_list.append(
                {
                    "symbol": pos.symbol,
                    "side": pos.side.value if hasattr(pos.side, "value") else str(pos.side),
                    "amount": pos.amount,
                    "entry_price": pos.entry_price,
                    "current_price": pos.current_price,
                    "pnl_pct": pos.pnl_pct,
                    "pnl_usd": pos.unrealized_pnl,
                    "leverage": pos.leverage,
                    "market_type": pos.market_type,
                    "strategy": pos.strategy,
                    "stop_loss": (ts.current_stop if ts else pos.stop_loss),
                    "notional_value": pos.notional_value,
                    "age_minutes": ((_time.time() - pos.opened_at.timestamp()) / 60 if pos.opened_at else 0),
                    "breakeven_locked": ts.breakeven_locked if ts else False,
                    "scale_mode": sp.mode.value if sp else "",
                    "scale_phase": sp.phase.value if sp else "",
                    "dca_count": sp.adds if sp else 0,
                    "trade_url": self.settings.symbol_platform_url(pos.symbol, pos.market_type),
                }
            )

        wick_list = []
        for sym, ws in self.orders.wick_scalper.active_scalps.items():
            wick_list.append(
                {
                    "symbol": sym,
                    "scalp_side": ws.scalp_side,
                    "entry_price": ws.entry_price,
                    "amount": ws.amount,
                    "age_minutes": ws.age_minutes,
                    "max_hold_minutes": ws.max_hold_minutes,
                }
            )

        strat_list = []
        open_syms = set(self.orders.scaler.active_positions.keys())
        for key, stats in self._strategy_stats.items():
            parts = key.split(":", 1)
            sname = parts[0]
            ssym = parts[1] if len(parts) > 1 else ""
            strat_list.append(
                {
                    "name": sname,
                    "symbol": ssym,
                    "market_type": "futures",
                    "leverage": self.settings.default_leverage,
                    "is_dynamic": False,
                    "open_now": 1 if ssym in open_syms else 0,
                    "applied_count": stats.get("total") or 0,
                    "success_count": stats.get("winners") or 0,
                    "fail_count": stats.get("losers") or 0,
                }
            )
        margin_used = 0.0
        for sp in self.orders.scaler.active_positions.values():
            margin_used += sp.avg_entry_price * sp.current_size / max(sp.current_leverage, 1)
        total_balance = self.target._current_balance

        t = self.target
        best = t.best_day
        worst = t.worst_day
        daily_report = {
            "compound_report": t.compound_report(),
            "history": [r.model_dump() for r in t.history],
            "winning_days": t.winning_days,
            "losing_days": t.losing_days,
            "target_hit_days": t.target_hit_days,
            "avg_daily_pnl_pct": t.avg_daily_pnl_pct,
            "best_day": best.model_dump() if best else None,
            "worst_day": worst.model_dump() if worst else None,
        }

        payload = {
            "bot_id": self.settings.bot_id or "default",
            "bot_style": self.settings.bot_style,
            "exchange": self.settings.exchange.upper(),
            "status": {
                "running": self._running,
                "trading_mode": self.settings.trading_mode,
                "exchange_name": self.settings.exchange.upper(),
                "exchange_url": self.settings.platform_url,
                "balance": total_balance,
                "available_margin": max(0.0, total_balance - margin_used),
                "daily_pnl": self.target.todays_pnl,
                "daily_pnl_pct": self.target.todays_pnl_pct,
                "tier": self.target.tier.value,
                "tier_progress_pct": self.target.progress_pct,
                "daily_target_pct": self.target.daily_target_pct,
                "total_growth_pct": self.target.total_growth_pct,
                "total_growth_usd": total_balance - self.target._initial_capital,
                "uptime_seconds": time.time() - self._started_at.timestamp() if self._started_at else 0,
                "strategies_count": len(self._strategies),
                "profit_buffer_pct": self.target.profit_buffer_pct,
                "manual_stop_active": self.target.manual_stop,
            },
            "positions": pos_list,
            "wick_scalps": wick_list,
            "strategies": strat_list,
            "trade_log": self.orders._trade_log[-50:],
            "daily_report": daily_report,
        }

        if self._multibot:
            payload["queue_updates"] = self._hub_queue_updates
            self._hub_queue_updates = {"consumed": [], "rejected": {}}
            if self._last_bot_status:
                payload["bot_status"] = self._last_bot_status.model_dump()

        hub_url = self.settings.hub_url
        if hub_url:
            await self._post_to_hub(hub_url, payload)

    async def _post_to_hub(self, hub_url: str, payload: dict[str, Any]) -> None:
        """POST status to hub. Returns enabled flag, confirmed keys, and queue proposal."""
        if not self._hub_session:
            self._hub_session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(force_close=True),
                timeout=aiohttp.ClientTimeout(total=10),
            )
        try:
            url = f"{hub_url.rstrip('/')}/internal/report"
            async with self._hub_session.post(url, json=payload) as resp:
                if resp.status != 200:
                    logger.warning("Hub report failed: {}", resp.status)
                    return
                body = await resp.json()
                for key in body.get("confirmed_keys", []):
                    self._pending_hub_acks.pop(key, None)
                if "enabled" in body:
                    self._hub_enabled = body["enabled"]
                if "trade_queue" in body:
                    self._hub_trade_queue = TradeQueue(**body["trade_queue"])
        except Exception as e:
            logger.error("Hub report error: {}", e, exc_info=True)
        self._retry_pending_hub_trades()

    async def _fetch_intel(self) -> None:
        """GET intel, analytics, and extreme watchlist from hub. Called once per full tick."""
        hub_url = self.settings.hub_url
        if not hub_url or not self._multibot:
            return
        if not self._hub_session:
            self._hub_session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(force_close=True),
                timeout=aiohttp.ClientTimeout(total=10),
            )
        try:
            url = f"{hub_url.rstrip('/')}/internal/intel"
            async with self._hub_session.get(url) as resp:
                if resp.status != 200:
                    return
                body = await resp.json()
                if "intel" in body:
                    self._hub_intel = IntelSnapshot(**body["intel"])
                if "analytics" in body:
                    self._hub_analytics = AnalyticsSnapshot(**body["analytics"])
                if "extreme_watchlist" in body:
                    self._hub_extreme_watchlist = ExtremeWatchlist(**body["extreme_watchlist"])
                if "intel_age" in body:
                    self._hub_intel_age = body["intel_age"]
        except Exception as e:
            logger.error("Intel fetch error: {}", e)

    async def _push_trade_to_hub(self, record: TradeRecord, request_key: str = "") -> None:
        """Push a trade open/close event to the hub's DB via HTTP with idempotency key."""
        hub_url = self.settings.hub_url
        if not hub_url:
            return
        target = hub_url
        if not request_key:
            request_key = uuid.uuid4().hex
        if not self._hub_session:
            self._hub_session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(force_close=True),
                timeout=aiohttp.ClientTimeout(total=5),
            )
        payload = {
            "bot_id": self.settings.bot_id or "default",
            "action": record.action,
            "trade": record.model_dump(),
            "request_key": request_key,
        }
        self._pending_hub_acks[request_key] = payload
        try:
            url = f"{target.rstrip('/')}/internal/trade"
            async with self._hub_session.post(url, json=payload) as resp:
                if resp.status != 200:
                    logger.error("Hub trade push failed: {} (key={})", resp.status, request_key[:8])
        except Exception as e:
            logger.error("Hub trade push error: {} (key={}, will retry)", e, request_key[:8])

    # ---- Hub state recovery & in-memory stats ----

    async def _recover_state_from_hub(self) -> None:
        """On startup, fetch open trades and strategy stats from the hub."""
        hub_url = self.settings.hub_url
        if not hub_url:
            logger.info("No hub URL — starting with empty state")
            return
        target = hub_url
        bot_id = self.settings.bot_id or "default"
        if not self._hub_session:
            self._hub_session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(force_close=True),
                timeout=aiohttp.ClientTimeout(total=10),
            )
        try:
            async with self._hub_session.get(f"{target.rstrip('/')}/internal/trades/{bot_id}/open") as resp:
                if resp.status == 200:
                    open_trades = await resp.json()
                    await self._reconcile_open_trades(open_trades, target, bot_id)
                else:
                    logger.warning("Hub returned {} for open trades", resp.status)
        except Exception as e:
            logger.warning("Could not fetch open trades from hub: {}", e)

        try:
            async with self._hub_session.get(f"{target.rstrip('/')}/internal/trades/{bot_id}/stats") as resp:
                if resp.status == 200:
                    self._strategy_stats = await resp.json()
                    total = sum(s.get("total", 0) for s in self._strategy_stats.values())
                    logger.info(
                        "Loaded strategy stats from hub: {} strategies, {} trades", len(self._strategy_stats), total
                    )
        except Exception as e:
            logger.warning("Could not fetch strategy stats from hub: {}", e)

    async def _reconcile_open_trades(self, hub_open: list[dict[str, Any]], hub_target: str, bot_id: str) -> None:
        """Check each hub-reported open trade against the exchange. Recovery-close dead ones."""
        if not hub_open:
            logger.info("Hub reports 0 open trades — clean start")
            return

        exchange_positions: dict[str, Any] = {}
        if not self.settings.is_paper_local():
            try:
                positions = await self.exchange.fetch_positions()
                for p in positions:
                    if p.amount > 0:
                        exchange_positions[p.symbol] = p
            except Exception as e:
                logger.warning("Could not fetch exchange positions for reconciliation: {}", e)

        recovered = 0
        dead = 0
        for td in hub_open:
            symbol = td.get("symbol", "")
            if not symbol:
                continue
            if self.settings.is_paper_local() or symbol in exchange_positions or (not exchange_positions):
                rec = TradeRecord(**{k: v for k, v in td.items() if k in TradeRecord.model_fields})
                self._open_trades[symbol] = rec
                recovered += 1
            else:
                opened_at = td.get("opened_at", "")
                if opened_at:
                    try:
                        async with self._hub_session.post(  # type: ignore[union-attr]
                            f"{hub_target.rstrip('/')}/internal/recovery-close",
                            json={"bot_id": bot_id, "opened_at": opened_at},
                        ) as resp:
                            if resp.status == 200:
                                dead += 1
                    except Exception:
                        dead += 1
                else:
                    dead += 1

        if recovered:
            logger.info("Recovered {} open trades from hub", recovered)
        if dead:
            logger.info("Marked {} dead trades as recovery_close (no longer on exchange)", dead)

    def _get_strategy_stats(self, strategy: str, symbol: str = "") -> dict[str, Any]:
        """Look up strategy stats from in-memory cache (seeded from hub on startup)."""
        key = f"{strategy}:{symbol}" if symbol else strategy
        return self._strategy_stats.get(key, {})

    def _update_strategy_stats(self, record: TradeRecord) -> None:
        """Increment in-memory strategy stats after a trade close."""
        key = f"{record.strategy}:{record.symbol}" if record.symbol else record.strategy
        stats = self._strategy_stats.setdefault(key, {"total": 0, "winners": 0, "losers": 0, "total_pnl": 0.0})
        stats["total"] = (stats.get("total") or 0) + 1
        if record.is_winner:
            stats["winners"] = (stats.get("winners") or 0) + 1
        elif record.pnl_usd != 0:
            stats["losers"] = (stats.get("losers") or 0) + 1
        stats["total_pnl"] = (stats.get("total_pnl") or 0.0) + record.pnl_usd

    def _retry_pending_hub_trades(self) -> None:
        """Re-send any trade events that haven't been ack'd yet."""
        if not self._pending_hub_acks:
            return
        # Cap size to avoid unbounded growth if hub stops confirming (e.g. down or DB errors)
        while len(self._pending_hub_acks) > self._PENDING_HUB_ACKS_MAX:
            oldest_key = next(iter(self._pending_hub_acks))
            self._pending_hub_acks.pop(oldest_key, None)
        stale = list(self._pending_hub_acks.values())
        if len(stale) > 20:
            logger.warning("Hub ack buffer has {} unconfirmed trades", len(stale))
        for payload in stale[:5]:
            rk = payload.get("request_key", "")
            record_data = payload.get("trade", {})
            rec = TradeRecord(**{k: v for k, v in record_data.items() if k in TradeRecord.model_fields})
            task = asyncio.ensure_future(self._push_trade_to_hub(rec, request_key=rk))
            self._hub_tasks.add(task)
            task.add_done_callback(self._hub_tasks.discard)

    def _read_shared_intel(self) -> MarketCondition | None:
        """Read intel from hub report response.

        Hub provides IntelSnapshot via the /internal/report cycle.
        Returns None if intel is stale (>600s) or missing.
        """
        intel_age = self._hub_intel_age
        snap = self._hub_intel
        if intel_age > 600:
            return None
        if not snap or not snap.sources_active:
            return None

        if snap.news_items:
            self._recent_news = []
            for nd in snap.news_items:
                try:
                    pub = datetime.fromisoformat(nd["published"]) if nd.get("published") else datetime.now(UTC)
                    self._recent_news.append(
                        NewsItem(
                            headline=nd.get("headline", ""),
                            source=nd.get("source", ""),
                            url=nd.get("url", ""),
                            published=pub,
                            matched_symbols=nd.get("matched_symbols", []),
                            sentiment=nd.get("sentiment", "neutral"),
                            sentiment_score=nd.get("sentiment_score", 0.0),
                        )
                    )
                except Exception:
                    pass

        return MarketCondition(
            regime=MarketRegime(snap.regime),
            fear_greed=snap.fear_greed,
            fear_greed_bias=snap.fear_greed_bias,
            liquidation_24h=snap.liquidation_24h,
            mass_liquidation=snap.mass_liquidation,
            liquidation_bias=snap.liquidation_bias,
            macro_event_imminent=snap.macro_event_imminent,
            macro_exposure_mult=snap.macro_exposure_mult,
            macro_spike_opportunity=snap.macro_spike_opportunity,
            next_macro_event=snap.next_macro_event,
            whale_bias=snap.whale_bias,
            overleveraged_side=snap.overleveraged_side,
            tv_btc_consensus=snap.tv_btc_consensus,
            tv_eth_consensus=snap.tv_eth_consensus,
            position_size_multiplier=snap.position_size_multiplier,
            should_reduce_exposure=snap.should_reduce_exposure,
            preferred_direction=snap.preferred_direction,
        )

    def _apply_pattern_analysis(
        self,
        sig: Signal,
        candles: list[Candle],
        is_low_liq: bool,
    ) -> float:
        """Run chart pattern detection and enrich signal with smart SL/TP.

        Returns the pattern signal boost (0.0 if no pattern found).
        Mutates sig in-place (model_copy already happened upstream).
        """
        if not candles or len(candles) < 30:
            return 0.0

        side = "long" if sig.action == SignalAction.BUY else "short"
        current_price = sig.suggested_price or candles[-1].close
        fallback_pct = self.risk.default_stop_loss_pct

        try:
            smart = self.pattern_detector.analyze(
                candles=candles,
                current_price=current_price,
                side=side,
                low_liquidity=is_low_liq,
                fallback_stop_pct=fallback_pct,
            )
        except Exception as e:
            logger.warning("Pattern analysis error for {}: {}", sig.symbol, e)
            return 0.0

        if smart.initial_stop > 0 and not sig.suggested_stop_loss:
            sig.suggested_stop_loss = smart.initial_stop
        if smart.tightened_stop > 0:
            sig.tightened_stop = smart.tightened_stop
        if smart.take_profit_1 > 0 and not sig.suggested_take_profit:
            sig.suggested_take_profit = smart.take_profit_1

        boost = 0.0
        if smart.has_pattern and smart.pattern:
            boost = smart.pattern.signal_boost
            sig.strength = min(1.0, sig.strength * (1.0 + boost))
            logger.info(
                "Pattern {} for {} → SL={:.6f} (deep) / {:.6f} (tight), TP={:.4f}/{:.4f}, boost={:.2f}",
                smart.pattern.pattern_type.value,
                sig.symbol,
                smart.initial_stop,
                smart.tightened_stop,
                smart.take_profit_1,
                smart.take_profit_2,
                boost,
            )
        elif smart.has_structure:
            logger.debug(
                "Structure SL for {}: {:.6f} (deep) / {:.6f} (tight) — no pattern",
                sig.symbol,
                smart.initial_stop,
                smart.tightened_stop,
            )

        return boost

    def _adjust_for_target(self, sig: Signal, aggression: float) -> Signal:
        adjusted = sig.model_copy()
        adjusted.strength = min(1.0, sig.strength * aggression)

        if self.target.in_ride_mode and not sig.quick_trade:
            adjusted.strength *= 0.3
            logger.debug(
                "Ride mode (>{:.0f}%) — reducing signal strength for {}", self.target.ride_target_pct, sig.symbol
            )
        elif self.target.daily_profit_secured and not sig.quick_trade:
            adjusted.strength *= 0.6
            logger.debug("Daily secured — slightly reducing signal strength for {}", sig.symbol)

        return adjusted

    async def _process_signal(self, sig: Signal, low_liquidity: bool = False, pyramid: bool = False) -> None:
        mode_tag = "PYRAMID" if pyramid else ("GAMBLING" if low_liquidity else "WINNERS")
        logger.info(
            "Signal: {} {} {} (str={:.2f}, strat={}, reason={}, mode={})",
            sig.action.value,
            sig.symbol,
            sig.market_type,
            sig.strength,
            sig.strategy,
            sig.reason,
            mode_tag,
        )

        if sig.strength < 0.2 and sig.action != SignalAction.CLOSE:
            logger.debug("Signal too weak ({:.2f}), skipping", sig.strength)
            return

        is_close = sig.action == SignalAction.CLOSE
        order = await self.orders.execute_signal(
            sig,
            low_liquidity=low_liquidity,
            pyramid=pyramid,
        )
        if order:
            realized_pnl = 0.0
            if is_close:
                self._log_closed_trade(order, sig.strategy)
                realized_pnl = self._calc_realized_pnl(order)
            else:
                self._log_opened_trade(sig, order, low_liquidity=low_liquidity)
            self._active_signals.append(sig)
            self.target.record_trade(realized_pnl=realized_pnl)
            if len(self._active_signals) > 100:
                self._active_signals = self._active_signals[-100:]

    async def _handle_spike(self, spike: SpikeEvent) -> None:
        await self.notifier.alert_spike(spike.symbol, spike.change_pct, spike.direction, spike.price)

    async def _log_status(self) -> None:
        now = datetime.now(UTC)
        if self._last_status_log and (now - self._last_status_log).total_seconds() < self._status_interval:
            return
        self._last_status_log = now

        logger.info(self.target.status_report())
        logger.info(self.risk.risk_summary())

        for _sym, sp in self.orders.scaler.active_positions.items():
            logger.info("  {}", sp.status_line())

        stops = self.orders.trailing.active_stops
        if stops:
            for sym, ts in stops.items():
                be_tag = " [BE-LOCKED]" if ts.breakeven_locked else ""
                liq_tag = " [LOW-LIQ]" if ts.low_liquidity else ""
                logger.info(
                    "  Trail {}: stop={:.6f} peak={:.6f} pnl={:+.1f}% active={}{}{}",
                    sym,
                    ts.current_stop,
                    ts.peak_price,
                    ts.pnl_from_stop,
                    ts.activated,
                    be_tag,
                    liq_tag,
                )

        hedges = self.orders.hedger.active_pairs
        if hedges:
            for _sym, hp in hedges.items():
                logger.info("  {}", hp.status_line())

        wick_scalps = self.orders.wick_scalper.active_scalps
        if wick_scalps:
            for sym, ws in wick_scalps.items():
                logger.info(
                    "  Wick scalp {}: {} @ {:.6f} ({:.0f}m old)", sym, ws.scalp_side, ws.entry_price, ws.age_minutes
                )

    # -- Extreme Mover Strategy (6.5) -- #

    CORRELATION_MAP: dict[str, list[str]] = {
        "BTC/USDT": ["ETH/USDT", "SOL/USDT"],
        "ETH/USDT": ["BTC/USDT", "SOL/USDT", "LINK/USDT", "AVAX/USDT"],
        "SOL/USDT": ["BTC/USDT", "ETH/USDT"],
        "DOGE/USDT": ["SHIB/USDT", "PEPE/USDT", "FLOKI/USDT"],
        "SHIB/USDT": ["DOGE/USDT", "PEPE/USDT"],
        "PEPE/USDT": ["DOGE/USDT", "SHIB/USDT"],
        "XRP/USDT": ["ADA/USDT", "XLM/USDT"],
        "ADA/USDT": ["XRP/USDT", "DOT/USDT"],
        "AVAX/USDT": ["ETH/USDT", "SOL/USDT"],
        "LINK/USDT": ["ETH/USDT"],
    }

    def _symbol_available(self, symbol: str) -> bool:
        """Check if a symbol is tradeable on this bot's exchange."""
        if not self._available_symbols:
            return True  # no data yet, optimistic
        return symbol in self._available_symbols

    async def _evaluate_extreme_candidates(self) -> None:
        """Read extreme watchlist from hub (in-memory), approve candidates for WS subscription."""
        watchlist = self._hub_extreme_watchlist
        if not watchlist:
            return
        if not watchlist.candidates:
            if self.extreme_watcher.active_count > 0:
                await self.extreme_watcher.sync_watchlist({})
            return

        stale_cutoff = self.settings.extreme_stale_seconds
        now_ts = time.time()
        approved: dict[str, str] = {}
        existing_position_symbols: set[str] = set()

        open_symbols = set(self.orders.scaler.active_positions.keys())
        extreme_positions = sum(1 for s in self._active_signals if s.strategy.startswith("extreme_"))
        my_exchange = self.settings.exchange.upper()

        for candidate in watchlist.candidates:
            if not self._symbol_available(candidate.symbol):
                logger.debug("Extreme skip {}: not on {}", candidate.symbol, my_exchange)
                continue
            if my_exchange in candidate.unsupported_exchanges:
                logger.debug("Extreme skip {}: tagged not-for-{}", candidate.symbol, my_exchange)
                continue
            try:
                detected = datetime.fromisoformat(candidate.detected_at.replace("Z", "+00:00"))
                age = now_ts - detected.timestamp()
                if age > stale_cutoff:
                    continue
            except (ValueError, AttributeError):
                continue

            # Correlation check: if we hold a correlated asset, promote immediately
            correlated = self.CORRELATION_MAP.get(candidate.symbol, [])
            has_correlated_exposure = candidate.symbol in open_symbols or any(c in open_symbols for c in correlated)

            if candidate.symbol in open_symbols:
                existing_position_symbols.add(candidate.symbol)
                approved[candidate.symbol] = candidate.direction
                logger.info(
                    "Extreme: promoting {} to WS exit-watch (already open)",
                    candidate.symbol,
                )
                continue

            if has_correlated_exposure:
                existing_position_symbols.add(candidate.symbol)
                approved[candidate.symbol] = candidate.direction
                exposed_via = [c for c in correlated if c in open_symbols]
                logger.info(
                    "Extreme: promoting {} to WS (correlated with open: {})",
                    candidate.symbol,
                    ", ".join(exposed_via),
                )
                self._tighten_correlated_stops(candidate.symbol, candidate.direction, exposed_via)
                continue

            if extreme_positions >= self.settings.extreme_max_positions:
                continue
            if not self.target.should_trade():
                continue
            if len(self.orders.scaler.active_positions) >= self.settings.effective_max_concurrent_positions:
                continue

            approved[candidate.symbol] = candidate.direction

        await self.extreme_watcher.sync_watchlist(approved, existing_position_symbols)

        if approved:
            logger.debug(
                "Extreme eval: {} approved ({} exit-watch, {} entry-hunt)",
                len(approved),
                len(existing_position_symbols),
                len(approved) - len(existing_position_symbols),
            )

    def _tighten_correlated_stops(self, extreme_symbol: str, direction: str, exposed_symbols: list[str]) -> None:
        """When an extreme move happens in a correlated asset, tighten stops on our exposure."""
        for sym in exposed_symbols:
            ts = self.orders.trailing.get(sym)
            if not ts:
                continue
            sp = self.orders.scaler.get(sym)
            if not sp:
                continue

            pos_is_long = sp.side == "long"
            move_is_bullish = direction == "bull"
            is_same_direction = (pos_is_long and move_is_bullish) or (not pos_is_long and not move_is_bullish)

            if is_same_direction:
                logger.info(
                    "Extreme move in {} aligns with {} {} — letting trailing ride",
                    extreme_symbol,
                    sym,
                    sp.side,
                )
            else:
                entry = sp.avg_entry_price
                if entry > 0 and ts.current_stop > 0:
                    if not ts.breakeven_locked:
                        ts.current_stop = entry
                        ts.breakeven_locked = True
                    logger.warning(
                        "Extreme move in {} AGAINST {} {} — tightened stop to entry {:.6f}",
                        extreme_symbol,
                        sym,
                        sp.side,
                        entry,
                    )

    async def _check_daily_reset(self) -> None:
        now = datetime.now(UTC)
        if now.hour == 0 and now.minute < 2:
            if self.target._last_reset and self.target._last_reset.date() == now.date():
                return
            balance_map = await self.exchange.fetch_balance()
            raw_balance = balance_map.get("USDT", 0.0)
            self.target.update_balance(raw_balance)

            logger.info("=== DAILY RESET ===")
            logger.info(self.target.status_report())
            logger.info("Total growth since start: {:.1f}%", self.target.total_growth_pct)

            self.risk.reset_daily(raw_balance, profit_buffer_pct=self.target.profit_buffer_pct)
            self.target.reset_day(raw_balance)


def main() -> None:
    settings = get_settings()

    logger.remove()
    logger.add(sys.stderr, level=settings.log_level)
    logger.add("logs/bot_{time}.log", rotation="1 day", retention="30 days", level="DEBUG")

    bot = TradingBot(settings, daily_target_pct=5.0)

    bot_label = settings.bot_id or "default"
    if settings.hub_only:
        logger.info("Bot [{}]: HUB-ONLY mode — dashboard and coordination, no trading", bot_label)
    else:
        logger.info(
            "Bot [{}]: style={} — trade entries come from hub queue",
            bot_label,
            settings.bot_style,
        )

    loop = asyncio.new_event_loop()
    _background_tasks: list[asyncio.Task[None]] = []

    def _shutdown(sig_num: int, frame: object) -> None:
        logger.info("Received signal {}, shutting down...", sig_num)
        _background_tasks.append(loop.create_task(bot.stop()))

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    from web.command_server import start_command_server

    async def _run_with_cmd_server() -> None:
        runner = await start_command_server(bot, settings.dashboard_host, settings.dashboard_port)
        try:
            await bot.start()
        finally:
            await runner.cleanup()

    try:
        loop.run_until_complete(_run_with_cmd_server())
    except KeyboardInterrupt:
        loop.run_until_complete(bot.stop())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
