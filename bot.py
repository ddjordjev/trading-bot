from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from datetime import UTC, datetime

from loguru import logger

from analytics import AnalyticsEngine
from config.settings import Settings, get_settings
from core.exchange import BaseExchange, create_exchange
from core.market_schedule import get_market_schedule
from core.models import Signal, SignalAction
from core.models.order import Order, OrderSide
from core.orders import OrderManager
from core.risk import RiskManager
from core.risk.daily_target import DailyTargetTracker, DailyTier
from core.risk.market_filter import LiquidityTier, MarketQualityFilter
from db import TradeDB
from db.models import TradeRecord
from intel import MarketCondition, MarketIntel
from news import NewsItem, NewsMonitor
from notifications import NotificationType, Notifier
from scanner import TrendingCoin, TrendingScanner
from shared.models import (
    BotDeploymentStatus,
    DeploymentLevel,
    SignalPriority,
    TradeProposal,
)
from shared.state import SharedState
from strategies import BUILTIN_STRATEGIES
from strategies.base import BaseStrategy
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
        self.news = NewsMonitor(self.settings)
        self.target = DailyTargetTracker(daily_target_pct=daily_target_pct, compound=True)
        self.market_filter = MarketQualityFilter(
            min_liquidity_volume=self.settings.min_liquidity_volume,
        )
        self.intel: MarketIntel | None = None
        if self.settings.intel_enabled:
            self.intel = MarketIntel(
                coinglass_key=self.settings.coinglass_api_key,
                symbols=self.settings.intel_symbol_list,
                tv_exchange=self.settings.tv_exchange,
                cmc_api_key=self.settings.cmc_api_key,
                coingecko_api_key=self.settings.coingecko_api_key,
            )

        self.scanner = TrendingScanner(
            poll_interval=60,
            min_volume_24h=5_000_000,
            min_market_cap=50_000_000,
            min_hourly_move_pct=2.0,
            min_daily_move_pct=5.0,
            intel=self.intel,
        )

        self.trade_db = TradeDB()
        self.trade_db.connect()
        self.analytics = AnalyticsEngine(self.trade_db)
        self.shared = SharedState()

        self._strategies: list[BaseStrategy] = []
        self._dynamic_strategies: dict[str, BaseStrategy] = {}
        self._active_signals: list[Signal] = []
        self._recent_news: list[NewsItem] = []
        self._whale_alerted: set[str] = set()  # symbols already alerted for whale position
        self._running = False
        self._tick_interval = 60
        self._status_interval = 300
        self._last_status_log: datetime | None = None
        self._started_at: datetime | None = None
        self._warmup_minutes = 3  # no queue processing for first N minutes

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
        logger.info("TRADING BOT v0.6.0")
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
        logger.info("Intel: {}", "ENABLED" if self.intel else "disabled")
        logger.info("Analytics DB: {} trades logged", self.trade_db.trade_count())
        logger.info("=" * 60)

        self.analytics.refresh()
        if self.analytics.scores:
            logger.info(self.analytics.summary())

        schedule = get_market_schedule()
        schedule.configure(fmp_api_key=self.settings.fmp_api_key)
        await schedule.refresh_holidays()
        logger.info("Market schedule: {}", schedule.summary())

        await self.exchange.connect()
        await self.notifier.start()
        await self.news.start()
        await self.scanner.start()
        if self.intel:
            await self.intel.start()
        self.news.on_news(self._on_news)
        self.scanner.on_trending(self._on_trending)

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
        await self._run_loop()

    async def stop(self) -> None:
        logger.info("Shutting down...")
        logger.info("Final status: {}", self.target.status_report())
        self._running = False
        if self.intel:
            await self.intel.stop()
        await self.scanner.stop()
        await self.news.stop()
        await self.notifier.stop()
        await self.exchange.disconnect()
        self.trade_db.close()
        logger.info("Bot stopped")

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self._tick()
                await asyncio.sleep(self._tick_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Error in main loop: {}", e)
                await asyncio.sleep(10)

    async def _tick(self) -> None:

        balance_map = await self.exchange.fetch_balance()
        balance = self.settings.cap_balance(balance_map.get("USDT", 0.0))
        self.target.update_balance(balance)

        logger.debug(
            "=== TICK === bal=${:.2f} pnl={:+.2f}% tier={} aggr={:.2f} strats={} dynamic={} trade={}",
            balance,
            self.target.todays_pnl_pct,
            self.target.tier.value,
            self.target.aggression_multiplier(),
            len(self._strategies),
            len(self._dynamic_strategies),
            self.target.should_trade(),
        )

        # 0. Manual override check
        if self.target.manual_close_all:
            logger.critical("CLOSE_ALL detected -- closing all positions NOW")
            await self._close_all_positions("Manual CLOSE_ALL file")
            self.target.clear_close_all()
            return

        # 1. Check trailing stops and liquidation
        closed = await self.orders.check_stops()
        for order in closed:
            await self.notifier.alert_liquidation(order.symbol, 0, balance)
            self._log_closed_trade(order, "stop")

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
        partials = await self.orders.try_partial_take()
        if partials:
            logger.info("Partial profit taken on {} position(s)", len(partials))

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
        await self.orders.close_expired_quick_trades(self._active_signals)

        await asyncio.sleep(0)

        # 8. Process trade queue (CRITICAL -> DAILY -> SWING)
        await self._process_trade_queue()

        # 9. Assess market intelligence
        # Prefer shared state from monitor service; fall back to in-process
        intel_condition = self._read_shared_intel()
        if intel_condition is None and self.intel:
            intel_condition = self.intel.assess()
            active_symbols = list(self.orders.scaler.active_positions.keys())
            if active_symbols:
                try:
                    await self.intel.tradingview.analyze_multi(active_symbols, "1h")
                except Exception as e:
                    logger.debug("TV multi-analyze error: {}", e)

        # 10. Legendary day check: at 100%+ decide whether to close or ride
        if self.target.tier.value == "legendary":
            reversal_risk = intel_condition is not None and intel_condition.should_reduce_exposure
            should_close, reason = self.target.should_close_all(reversal_risk)
            if should_close:
                logger.critical("LEGENDARY DAY + REVERSAL RISK -- closing all: {}", reason)
                await self._close_all_positions(reason)
                await self.notifier.send(
                    NotificationType.DAILY_SUMMARY,
                    f"LEGENDARY DAY CLOSED: {self.target.todays_pnl_pct:+.1f}%",
                    reason,
                )
                return

            if not self.target.legendary_email_sent:
                intel_text = self.intel.full_summary() if self.intel else "Intel disabled"
                ride_reason = self.target.legendary_ride_reason(intel_text)
                logger.info("LEGENDARY DAY -- letting it ride, emailing owner")
                await self.notifier.send(
                    NotificationType.DAILY_SUMMARY,
                    f"LEGENDARY DAY RIDING: {self.target.todays_pnl_pct:+.1f}% -- positions open",
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

        # 12. Run all strategies (collect candles for hedge analysis)
        candles_map: dict[str, list] = {}
        all_strategies = list(self._strategies) + list(self._dynamic_strategies.values())
        positions = await self.exchange.fetch_positions()
        pos_map = {p.symbol: p for p in positions if p.amount > 0}
        for strategy in all_strategies:
            try:
                # Sync position state so strategies survive restarts
                pos = pos_map.get(strategy.symbol)
                if pos:
                    side = "long" if pos.side == OrderSide.BUY else "short"
                    strategy.set_position_state(True, side)
                else:
                    strategy.set_position_state(False)

                candles = await self.exchange.fetch_candles(strategy.symbol, "1m", limit=200)
                ticker = await self.exchange.fetch_ticker(strategy.symbol)
                candles_map[strategy.symbol] = candles

                for c in candles:
                    strategy.feed_candle(c)

                spike = self.volatility.update(ticker)
                if spike:
                    await self._handle_spike(spike)

                sig = strategy.analyze(candles, ticker)
                if not sig:
                    logger.debug("Strategy '{}' on {} — no signal this tick", strategy.name, strategy.symbol)
                    continue

                logger.debug(
                    "Signal: {} {} {} str={:.2f} strat={} reason={} quick={} mkt={}",
                    sig.action.value,
                    sig.symbol,
                    sig.market_type,
                    sig.strength,
                    sig.strategy,
                    sig.reason[:50],
                    sig.quick_trade,
                    sig.market_type,
                )

                if sig.action == SignalAction.CLOSE:
                    await self._process_signal(sig)
                    continue

                if not self.settings.is_market_type_allowed(sig.market_type):
                    logger.debug("Skipping {} signal — market type '{}' not allowed", strategy.symbol, sig.market_type)
                    continue

                is_swing = sig.strategy == "swing_opportunity"
                use_pyramid = sig.strategy not in SCALP_ONLY_STRATEGIES

                liq = self.market_filter.assess_liquidity(candles, ticker)
                is_low_liq = liq.tier in (LiquidityTier.LOW, LiquidityTier.DEAD)

                if is_low_liq and not allow_gambling:
                    logger.info("Skipping {} -- low liquidity and not in gambling mode", strategy.symbol)
                    continue

                if is_low_liq and allow_gambling:
                    logger.info("GAMBLING BET on {} (low-liq, already had a good day)", strategy.symbol)
                    await self._process_signal(sig, low_liquidity=True)
                    continue

                if not allow_new_entries and not is_swing:
                    logger.debug("Skipping entry signal -- target/risk says sit out")
                    continue

                # Intel: direction filter -- don't go against strong crowd consensus
                if intel_condition and sig.action in (SignalAction.BUY, SignalAction.SELL):
                    sig = self._apply_intel_to_signal(sig, intel_condition)
                    if sig.strength <= 0:
                        continue

                tradeable, reason = self.market_filter.is_tradeable(candles, ticker)
                if not tradeable:
                    logger.info("Skipping {} -- {}", strategy.symbol, reason)
                    continue

                # Intel: boost signals during capitulation / mass liquidation reversal
                if intel_condition and intel_condition.macro_spike_opportunity:
                    logger.info("MACRO SPIKE OPPORTUNITY -- boosting signal for {}", sig.symbol)
                    sig = sig.model_copy()
                    sig.quick_trade = True
                    sig.max_hold_minutes = 15

                # TradingView alignment: boost/penalize based on TV technical analysis
                side = "long" if sig.action == SignalAction.BUY else "short"
                tv_boost = self._get_tv_boost(sig.symbol, side)
                if tv_boost != 1.0:
                    sig = sig.model_copy()
                    sig.strength *= tv_boost
                    logger.debug(
                        "TV boost for {} {}: {:.2f}x -> strength={:.2f}", sig.symbol, side, tv_boost, sig.strength
                    )

                # Analytics weight: reduce signal strength for underperforming strategies
                strat_weight = self._read_shared_analytics_weight(sig.strategy)
                if strat_weight < 1.0:
                    sig = sig.model_copy()
                    sig.strength *= strat_weight
                    if sig.strength <= 0:
                        logger.info(
                            "Analytics: {} weight {:.2f} killed signal for {}", sig.strategy, strat_weight, sig.symbol
                        )
                        continue

                sig = self._adjust_for_target(sig, aggression)

                logger.debug(
                    "Final signal: {} {} str={:.3f} (tv={:.2f} analytics={:.2f} aggr={:.2f}) pyramid={} | executing",
                    sig.action.value,
                    sig.symbol,
                    sig.strength,
                    tv_boost,
                    strat_weight,
                    aggression,
                    use_pyramid,
                )
                await self._process_signal(sig, pyramid=use_pyramid)

            except Exception as e:
                logger.error("Strategy '{}' error for {}: {}", strategy.name, strategy.symbol, e)

        # 13. Hedge check: open counter-positions on reversal signals
        if self.settings.hedge_enabled:
            try:
                hedges = await self.orders.try_hedge(candles_map)
                if hedges:
                    logger.info("Opened {} hedge position(s)", len(hedges))
            except Exception as e:
                logger.error("Hedge tick error: {}", e)

        # 14. Write deployment status for monitor service
        try:
            await self._write_deployment_status()
        except Exception as e:
            logger.debug("Failed to write deployment status: {}", e)

        # 15. Status + daily reset
        await self._log_status()
        await self._check_daily_reset()

    # -- Trade Queue Consumer (advisory, never forced) -- #

    MAX_QUEUE_EXECUTIONS_PER_TICK = 1

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

        try:
            queue = self.shared.read_trade_queue()
        except Exception:
            return

        if queue.pending_count == 0:
            return

        positions = await self.exchange.fetch_positions()
        active_count = sum(1 for p in positions if p.amount > 0)
        max_pos = self.settings.max_concurrent_positions
        free_slots = max(0, max_pos - active_count)

        tier = self.target.tier
        allow_new = self.target.should_trade()
        aggression = self.target.aggression_multiplier()
        pnl_pct = self.target.todays_pnl_pct

        avg_health = 0.0
        if active_count > 0:
            pnls = [p.pnl_pct for p in positions if p.amount > 0]
            avg_health = sum(pnls) / len(pnls)
        positions_secured = active_count == 0 or avg_health >= 0

        executed = 0
        tick_limit = self.MAX_QUEUE_EXECUTIONS_PER_TICK
        modified = False

        # --- CRITICAL: time-sensitive, but still respect capacity + tick cap ---
        for proposal in queue.get_actionable(SignalPriority.CRITICAL):
            if executed >= tick_limit:
                break
            if not self.settings.is_market_type_allowed(proposal.market_type):
                queue.mark_rejected(proposal.id, f"market type '{proposal.market_type}' not allowed")
                modified = True
                continue
            if free_slots <= 0:
                queue.mark_rejected(proposal.id, "no free slots")
                modified = True
                continue
            if not allow_new:
                queue.mark_rejected(proposal.id, f"tier={tier.value} — not trading")
                modified = True
                continue
            if proposal.strength * aggression < 0.2:
                queue.mark_rejected(proposal.id, "strength too low after aggression")
                modified = True
                continue

            ok = await self._execute_proposal(proposal, aggression)
            if ok:
                queue.mark_consumed(proposal.id)
                free_slots -= 1
                executed += 1
            else:
                queue.mark_rejected(proposal.id, "execution failed")
            modified = True

        # --- DAILY: only when idle with spare capacity and budget ---
        idle_enough = free_slots >= 2 or (free_slots >= 1 and active_count == 0)
        budget_ok = tier in (DailyTier.BUILDING, DailyTier.LOSING) or (tier == DailyTier.STRONG and positions_secured)

        if allow_new and idle_enough and budget_ok:
            for proposal in queue.get_actionable(SignalPriority.DAILY):
                if executed >= tick_limit:
                    break
                if not self.settings.is_market_type_allowed(proposal.market_type):
                    queue.mark_rejected(proposal.id, f"market type '{proposal.market_type}' not allowed")
                    modified = True
                    continue
                if free_slots <= 0:
                    break
                if proposal.strength * aggression < 0.3:
                    queue.mark_rejected(proposal.id, "daily: strength too low")
                    modified = True
                    continue

                ok = await self._execute_proposal(proposal, aggression)
                if ok:
                    queue.mark_consumed(proposal.id)
                    free_slots -= 1
                    executed += 1
                else:
                    queue.mark_rejected(proposal.id, "execution failed")
                modified = True

        # --- SWING: only when truly idle, positions secured, day is stable ---
        genuinely_idle = active_count == 0 or (free_slots >= 2 and positions_secured and pnl_pct >= 0)

        if allow_new and genuinely_idle:
            for proposal in queue.get_actionable(SignalPriority.SWING):
                if executed >= tick_limit:
                    break
                if not self.settings.is_market_type_allowed(proposal.market_type):
                    queue.mark_rejected(proposal.id, f"market type '{proposal.market_type}' not allowed")
                    modified = True
                    continue
                if free_slots <= 0:
                    break
                if proposal.strength * aggression < 0.3:
                    continue

                ok = await self._execute_swing_proposal(proposal, aggression)
                if ok:
                    queue.mark_consumed(proposal.id)
                    free_slots -= 1
                    executed += 1
                else:
                    queue.mark_rejected(proposal.id, "swing execution failed")
                modified = True

        if modified:
            self.shared.write_trade_queue(queue)

        if executed > 0:
            logger.info(
                "Queue: executed {} proposal(s) this tick (remaining: C={} D={} S={})",
                executed,
                len(queue.get_actionable(SignalPriority.CRITICAL)),
                len(queue.get_actionable(SignalPriority.DAILY)),
                len(queue.get_actionable(SignalPriority.SWING)),
            )

    async def _execute_proposal(self, proposal: TradeProposal, aggression: float) -> bool:
        """Convert a queue proposal into a trading signal and execute it."""
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

    def _apply_intel_to_signal(self, sig: Signal, condition: MarketCondition) -> Signal:
        """Adjust signal based on external market intelligence."""
        adjusted = sig.model_copy()

        preferred = condition.preferred_direction
        if preferred == "neutral":
            return adjusted

        is_long = sig.action == SignalAction.BUY
        is_short = sig.action == SignalAction.SELL

        # Going against mass liquidation reversal bias = bad idea
        if condition.mass_liquidation and ((preferred == "long" and is_short) or (preferred == "short" and is_long)):
            logger.info(
                "Intel BLOCKED {} {} -- mass liq bias is {} (reversal zone)", sig.action.value, sig.symbol, preferred
            )
            adjusted.strength = 0
            return adjusted

        # Going against extreme fear/greed = reduce strength but don't block
        if (condition.fear_greed <= 25 and is_short) or (condition.fear_greed >= 75 and is_long):
            adjusted.strength *= 0.5
            logger.info(
                "Intel REDUCED {} {} -- F&G={} (contrarian says {})",
                sig.action.value,
                sig.symbol,
                condition.fear_greed,
                preferred,
            )

        # Going against whale positioning = slight caution
        if condition.overleveraged_side:
            if condition.overleveraged_side == "longs" and is_long:
                adjusted.strength *= 0.7
                logger.info("Intel CAUTION {} long -- longs overleveraged (contrarian says short)", sig.symbol)
            elif condition.overleveraged_side == "shorts" and is_short:
                adjusted.strength *= 0.7
                logger.info("Intel CAUTION {} short -- shorts overleveraged (contrarian says long)", sig.symbol)

        # Aligned with intel = slight boost
        if (preferred == "long" and is_long) or (preferred == "short" and is_short):
            adjusted.strength = min(1.0, adjusted.strength * 1.15)

        return adjusted

    def _log_closed_trade(self, order: Order, close_reason: str = "") -> None:
        """Log a completed trade to the analytics database."""
        try:
            now = datetime.now(UTC)
            sp = self.orders.scaler.get(order.symbol)
            intel_cond = self.intel.condition if self.intel else None

            exit_price = order.average_price or order.price or 0
            entry_price = sp.avg_entry_price if sp and sp.avg_entry_price > 0 else exit_price
            amount = order.filled or order.amount
            leverage = order.leverage or (sp.current_leverage if sp else 1)
            side_str = order.side.value if hasattr(order.side, "value") else str(order.side)

            pnl_usd = 0.0
            pnl_pct = 0.0
            if entry_price > 0 and exit_price > 0:
                if side_str in ("buy", "long"):
                    pnl_usd = (exit_price - entry_price) * amount * leverage
                    pnl_pct = (exit_price - entry_price) / entry_price * 100
                else:
                    pnl_usd = (entry_price - exit_price) * amount * leverage
                    pnl_pct = (entry_price - exit_price) / entry_price * 100

            record = TradeRecord(
                symbol=order.symbol,
                side=side_str,
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
                dca_count=sp.adds if sp else 0,
                was_quick_trade=False,
                was_low_liquidity=sp.low_liquidity if sp else False,
                market_regime=intel_cond.regime.value if intel_cond else "",
                fear_greed=intel_cond.fear_greed if intel_cond else 50,
                daily_tier=self.target.tier.value,
                daily_pnl_at_entry=self.target.todays_pnl_pct,
                signal_strength=0,
                hour_utc=now.hour,
                day_of_week=now.weekday(),
                closed_at=now.isoformat(),
            )
            self.trade_db.log_trade(record)
            self._whale_alerted.discard(order.symbol)
        except Exception as e:
            logger.debug("Failed to log trade: {}", e)

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
                await self.orders.execute_signal(signal)
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
                profit_usd = sp.current_size * (price - sp.avg_entry_price) * sp.current_leverage
                if sp.side == "short":
                    profit_usd = sp.current_size * (sp.avg_entry_price - price) * sp.current_leverage

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
        capacity = 1.0 - (len(active) / max(self.settings.max_concurrent_positions, 1))
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
            level=level,
            open_positions=len(active),
            max_positions=self.settings.max_concurrent_positions,
            capacity_pct=capacity * 100,
            daily_pnl_pct=self.target.todays_pnl_pct,
            daily_tier=self.target.tier.value,
            avg_position_health=avg_health,
            worst_position_pnl=worst,
            should_trade=self.target.should_trade(),
            manual_stop=self.target.manual_stop,
        )
        self.shared.write_bot_status(status)

    def _read_shared_intel(self) -> MarketCondition | None:
        """Read intel from shared state file (written by monitor service).

        If the monitor is running, we use its data instead of running our
        own intel clients. Falls back to in-process intel if stale or missing.
        """
        intel_age = self.shared.intel_age_seconds()
        if intel_age > 600:  # stale after 10 minutes
            return None  # fall back to in-process

        snap = self.shared.read_intel()
        if not snap.sources_active:
            return None

        return MarketCondition(
            regime=snap.regime,
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

    def _read_shared_analytics_weight(self, strategy: str) -> float:
        """Read strategy weight from shared analytics state.

        If the analytics service is running, use its weights.
        Falls back to in-process analytics engine.
        """
        snap = self.shared.read_analytics()
        if not snap.weights:
            return self.analytics.get_weight(strategy)

        for w in snap.weights:
            if w.strategy == strategy:
                return w.weight
        return 1.0

    def _get_tv_boost(self, symbol: str, side: str) -> float:
        """Get TradingView signal boost, preferring shared state."""
        snap = self.shared.read_intel()
        for tv in snap.tv_analyses:
            if tv.symbol == symbol and tv.interval == "1h":
                return tv.signal_boost_long if side == "long" else tv.signal_boost_short
        if self.intel:
            return self.intel.tv_signal_boost(symbol, side)
        return 1.0

    def _adjust_for_target(self, sig: Signal, aggression: float) -> Signal:
        adjusted = sig.model_copy()
        adjusted.strength = min(1.0, sig.strength * aggression)

        if self.target.target_reached and not sig.quick_trade:
            adjusted.strength *= 0.3
            logger.debug("Target reached - reducing signal strength for {}", sig.symbol)

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

        order = await self.orders.execute_signal(
            sig,
            low_liquidity=low_liquidity,
            pyramid=pyramid,
        )
        if order:
            self._active_signals.append(sig)
            self.target.record_trade()
            if len(self._active_signals) > 100:
                self._active_signals = self._active_signals[-100:]

    async def _handle_spike(self, spike: SpikeEvent) -> None:
        await self.notifier.alert_spike(spike.symbol, spike.change_pct, spike.direction, spike.price)

        news_item = self.news.correlate_spike(spike.symbol, self._recent_news)
        if news_item:
            spike.confirmed_by_news = True
            spike.news_headline = news_item.headline
            logger.info("Spike on {} confirmed by news: {}", spike.symbol, news_item.headline)

    async def _on_trending(self, movers: list[TrendingCoin]) -> None:
        available = set()
        with contextlib.suppress(Exception):
            available = set(await self.exchange.get_available_symbols())

        current_symbols = {m.trading_pair for m in movers}
        for sym in list(self._dynamic_strategies.keys()):
            if sym not in current_symbols:
                del self._dynamic_strategies[sym]
                logger.info("Removed dynamic strategy for {} (no longer trending)", sym)

        for coin in movers:
            pair = coin.trading_pair
            if pair in self._dynamic_strategies:
                continue
            if available and pair not in available:
                continue
            if any(s.symbol == pair for s in self._strategies):
                continue

            if coin.is_low_liquidity:
                logger.info(
                    "Trending {} is LOW-LIQ (vol:{:.0f}M, cap:{:.0f}M) -- gambling only",
                    pair,
                    coin.volume_24h / 1e6,
                    coin.market_cap / 1e6,
                )

            from strategies.compound_momentum import CompoundMomentumStrategy

            mkt = "futures" if self.settings.futures_allowed else "spot"
            lev = self.settings.default_leverage if mkt == "futures" else 1
            strategy = CompoundMomentumStrategy(
                symbol=pair,
                market_type=mkt,
                leverage=lev,
                spike_pct=1.0,
                spike_max_hold=10,
            )
            self._dynamic_strategies[pair] = strategy
            direction = "BULL" if coin.momentum_score > 0 else "BEAR"
            liq_tag = " [LOW-LIQ]" if coin.is_low_liquidity else ""
            logger.info(
                "Dynamic strategy added: {} [{}]{} (1h:{:+.1f}% 24h:{:+.1f}%)",
                pair,
                direction,
                liq_tag,
                coin.change_1h,
                coin.change_24h,
            )

    async def _on_news(self, item: NewsItem) -> None:
        self._recent_news.append(item)
        if len(self._recent_news) > 200:
            self._recent_news = self._recent_news[-200:]

        if item.matched_symbols and abs(item.sentiment_score) > 0.3:
            logger.info(
                "News [{}]: {} (symbols: {}, sentiment: {})",
                item.source,
                item.headline,
                item.matched_symbols,
                item.sentiment,
            )
            await self.notifier.alert_news(item.headline, item.matched_symbols, item.source)

    async def _log_status(self) -> None:
        now = datetime.now(UTC)
        if self._last_status_log and (now - self._last_status_log).total_seconds() < self._status_interval:
            return
        self._last_status_log = now

        logger.info(self.target.status_report())
        logger.info(self.risk.risk_summary())
        logger.info(self.scanner.scan_summary())
        if self.intel:
            logger.info(self.intel.full_summary())
        logger.info("Active strategies: {} static + {} dynamic", len(self._strategies), len(self._dynamic_strategies))

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

    async def _check_daily_reset(self) -> None:
        now = datetime.now(UTC)
        if now.hour == 0 and now.minute < 2:
            balance_map = await self.exchange.fetch_balance()
            balance = self.settings.cap_balance(balance_map.get("USDT", 0.0))
            self.target.update_balance(balance)
            positions = await self.exchange.fetch_positions()

            logger.info("=== DAILY SUMMARY ===")
            logger.info(self.target.status_report())
            logger.info("Total growth since start: {:.1f}%", self.target.total_growth_pct)

            self.analytics.refresh()
            compound_report = self.target.compound_report()
            compound_report += "\n\n" + self.analytics.summary()
            if self.intel:
                compound_report += "\n\n" + self.intel.full_summary()
            logger.info("\n{}", compound_report)

            await self.notifier.send_daily_summary(
                balance=balance,
                pnl=self.target.todays_pnl,
                pnl_pct=self.target.todays_pnl_pct,
                trades=self.target._todays_trades,
                open_positions=len(positions),
                compound_report=compound_report,
                target_hit=self.target.target_reached,
            )
            self.risk.reset_daily(balance)
            self.target.reset_day(balance)


def main() -> None:
    settings = get_settings()

    logger.remove()
    logger.add(sys.stderr, level=settings.log_level)
    logger.add("logs/bot_{time}.log", rotation="1 day", retention="30 days", level="DEBUG")

    bot = TradingBot(settings, daily_target_pct=10.0)

    mkt = "futures" if settings.futures_allowed else "spot"
    bot.add_strategy("compound_momentum", "BTC/USDT", market_type=mkt)
    bot.add_strategy("compound_momentum", "ETH/USDT", market_type=mkt)
    bot.add_strategy("market_open_volatility", "BTC/USDT", market_type=mkt)
    bot.add_strategy("market_open_volatility", "ETH/USDT", market_type=mkt)
    bot.add_strategy("swing_opportunity", "BTC/USDT", market_type=mkt)
    bot.add_strategy("swing_opportunity", "ETH/USDT", market_type=mkt)

    loop = asyncio.new_event_loop()
    _background_tasks: list[asyncio.Task[None]] = []

    def _shutdown(sig_num: int, frame: object) -> None:
        logger.info("Received signal {}, shutting down...", sig_num)
        _background_tasks.append(loop.create_task(bot.stop()))

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    if settings.dashboard_enabled:
        import uvicorn

        from web.server import app, set_bot, setup_log_capture

        setup_log_capture()
        set_bot(bot)
        config = uvicorn.Config(
            app,
            host=settings.dashboard_host,
            port=settings.dashboard_port,
            log_level="warning",
            loop="none",
        )
        server = uvicorn.Server(config)

        async def _run_all() -> None:
            bot_task = asyncio.create_task(bot.start())
            web_task = asyncio.create_task(server.serve())
            logger.info("Dashboard: http://{}:{}", settings.dashboard_host, settings.dashboard_port)
            _done, pending = await asyncio.wait(
                [bot_task, web_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()

        try:
            loop.run_until_complete(_run_all())
        except KeyboardInterrupt:
            loop.run_until_complete(bot.stop())
        finally:
            loop.close()
    else:
        try:
            loop.run_until_complete(bot.start())
        except KeyboardInterrupt:
            loop.run_until_complete(bot.stop())
        finally:
            loop.close()


if __name__ == "__main__":
    main()
