from __future__ import annotations

import asyncio
import signal
import sys
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from config.settings import Settings, get_settings
from core.exchange import create_exchange, BaseExchange
from core.models import Ticker, Candle, Signal, SignalAction
from core.orders import OrderManager
from core.orders.scaler import ScaleMode
from core.risk import RiskManager
from core.risk.daily_target import DailyTargetTracker
from core.risk.market_filter import MarketQualityFilter, LiquidityTier
from notifications import Notifier, NotificationType
from news import NewsMonitor, NewsItem
from strategies.base import BaseStrategy
from strategies import BUILTIN_STRATEGIES
from volatility import VolatilityDetector, SpikeEvent
from scanner import TrendingScanner, TrendingCoin


# Strategies that should use PYRAMID mode (DCA down, lever up on recovery)
PYRAMID_STRATEGIES = {"swing_opportunity"}


class TradingBot:
    """Main bot orchestrator.

    Two position-building modes run in parallel:

    WINNERS (scalps): start small -> add to winners -> trail -> break-even lock
    PYRAMID (conviction): start tiny at low leverage -> DCA down as it drops ->
      avg entry improves -> price recovers -> raise leverage -> take partial
      profit (pull capital out) -> lock break-even -> ride the rest

    MEXC-specific:
    - Low-liq coins: only gambling-sized bets, self-managed stops
    - Lock break-even at +5%, then trail
    - After a good day, allow tiny yolo bets on trending shitcoins
    """

    def __init__(self, settings: Optional[Settings] = None, daily_target_pct: float = 10.0):
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
        self.scanner = TrendingScanner(
            poll_interval=60,
            min_volume_24h=5_000_000,
            min_market_cap=50_000_000,
            min_hourly_move_pct=2.0,
            min_daily_move_pct=5.0,
        )

        self._strategies: list[BaseStrategy] = []
        self._dynamic_strategies: dict[str, BaseStrategy] = {}
        self._active_signals: list[Signal] = []
        self._recent_news: list[NewsItem] = []
        self._running = False
        self._tick_interval = 60
        self._status_interval = 300
        self._last_status_log: Optional[datetime] = None

    # -- Strategy Management --

    def add_strategy(self, name: str, symbol: str, market_type: str = "spot",
                     leverage: int = 0, **params: object) -> None:
        lev = leverage or self.settings.default_leverage
        cls = BUILTIN_STRATEGIES.get(name)
        if not cls:
            raise ValueError(f"Unknown strategy: {name}. Available: {list(BUILTIN_STRATEGIES.keys())}")
        strategy = cls(symbol=symbol, market_type=market_type, leverage=lev, **params)
        self._strategies.append(strategy)
        mode = "PYRAMID" if name in PYRAMID_STRATEGIES else "WINNERS"
        logger.info("Added strategy '{}' for {} ({}, {}x, mode={})", name, symbol, market_type, lev, mode)

    def add_custom_strategy(self, strategy: BaseStrategy) -> None:
        self._strategies.append(strategy)
        logger.info("Added custom strategy '{}' for {}", strategy.name, strategy.symbol)

    # -- Main Loop --

    async def start(self) -> None:
        logger.info("=" * 60)
        logger.info("TRADING BOT v0.3.0")
        logger.info("Mode: {}", self.settings.trading_mode.upper())
        logger.info("Exchange: {}", self.settings.exchange)
        logger.info("Daily target: {:.0f}% (compounding)", self.target.daily_target_pct)
        logger.info("Strategies: {}", len(self._strategies))
        logger.info("Leverage: {}x default", self.settings.default_leverage)
        logger.info("PYRAMID mode: DCA down -> lever up -> partial take -> ride")
        logger.info("WINNERS mode: start small -> add to winners -> trail")
        logger.info("Gambling budget: {}% for low-liq coins", self.settings.gambling_budget_pct)
        logger.info("=" * 60)

        await self.exchange.connect()
        await self.notifier.start()
        await self.news.start()
        await self.scanner.start()
        self.news.on_news(self._on_news)
        self.scanner.on_trending(self._on_trending)

        balance_map = await self.exchange.fetch_balance()
        balance = balance_map.get("USDT", 0.0)
        self.risk.reset_daily(balance)
        self.target.reset_day(balance)

        projected = self.target.projected_balance
        logger.info("Starting balance: {:.2f} USDT", balance)
        logger.info("Projections if target hit daily -> 1w: {:.0f} | 1mo: {:.0f} | 3mo: {:.0f}",
                     projected["1_week"], projected["1_month"], projected["3_months"])

        self._running = True
        await self._run_loop()

    async def stop(self) -> None:
        logger.info("Shutting down...")
        logger.info("Final status: {}", self.target.status_report())
        self._running = False
        await self.scanner.stop()
        await self.news.stop()
        await self.notifier.stop()
        await self.exchange.disconnect()
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
        balance = balance_map.get("USDT", 0.0)
        self.target.update_balance(balance)

        # 1. Check trailing stops and liquidation
        closed = await self.orders.check_stops()
        for order in closed:
            await self.notifier.alert_liquidation(order.symbol, 0, balance)

        # 2. Scale into positions (both WINNERS adds and PYRAMID DCA-downs)
        scale_orders = await self.orders.try_scale_in()
        if scale_orders:
            logger.info("Scaled into {} position(s) this tick", len(scale_orders))

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

        # 5. Close expired quick trades
        await self.orders.close_expired_quick_trades(self._active_signals)

        # 6. Trading gates
        allow_new_entries = self.target.should_trade()
        aggression = self.target.aggression_multiplier()
        allow_gambling = self.target.target_reached and self.target.todays_pnl_pct > 0

        # 7. Run all strategies (collect candles for hedge analysis)
        candles_map: dict[str, list] = {}
        all_strategies = list(self._strategies) + list(self._dynamic_strategies.values())
        for strategy in all_strategies:
            try:
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
                    continue

                if sig.action == SignalAction.CLOSE:
                    await self._process_signal(sig)
                    continue

                is_swing = sig.strategy == "swing_opportunity"
                use_pyramid = sig.strategy in PYRAMID_STRATEGIES

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

                tradeable, reason = self.market_filter.is_tradeable(candles, ticker)
                if not tradeable:
                    logger.info("Skipping {} -- {}", strategy.symbol, reason)
                    continue

                sig = self._adjust_for_target(sig, aggression)
                await self._process_signal(sig, pyramid=use_pyramid)

            except Exception as e:
                logger.error("Strategy '{}' error for {}: {}", strategy.name, strategy.symbol, e)

        # 8. Hedge check: open counter-positions on reversal signals
        if self.settings.hedge_enabled:
            try:
                hedges = await self.orders.try_hedge(candles_map)
                if hedges:
                    logger.info("Opened {} hedge position(s)", len(hedges))
            except Exception as e:
                logger.error("Hedge tick error: {}", e)

        # 9. Status + daily reset
        await self._log_status()
        await self._check_daily_reset()

    def _adjust_for_target(self, sig: Signal, aggression: float) -> Signal:
        adjusted = sig.model_copy()
        adjusted.strength = min(1.0, sig.strength * aggression)

        if self.target.target_reached and not sig.quick_trade:
            adjusted.strength *= 0.3
            logger.debug("Target reached - reducing signal strength for {}", sig.symbol)

        return adjusted

    async def _process_signal(self, sig: Signal, low_liquidity: bool = False,
                              pyramid: bool = False) -> None:
        mode_tag = "PYRAMID" if pyramid else ("GAMBLING" if low_liquidity else "WINNERS")
        logger.info("Signal: {} {} {} (str={:.2f}, strat={}, reason={}, mode={})",
                     sig.action.value, sig.symbol, sig.market_type,
                     sig.strength, sig.strategy, sig.reason, mode_tag)

        if sig.strength < 0.2 and sig.action != SignalAction.CLOSE:
            logger.debug("Signal too weak ({:.2f}), skipping", sig.strength)
            return

        order = await self.orders.execute_signal(
            sig, low_liquidity=low_liquidity, pyramid=pyramid,
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
        try:
            available = set(await self.exchange.get_available_symbols())
        except Exception:
            pass

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
                logger.info("Trending {} is LOW-LIQ (vol:{:.0f}M, cap:{:.0f}M) -- gambling only",
                            pair, coin.volume_24h / 1e6, coin.market_cap / 1e6)

            from strategies.compound_momentum import CompoundMomentumStrategy
            strategy = CompoundMomentumStrategy(
                symbol=pair, market_type="futures",
                leverage=self.settings.default_leverage,
                spike_pct=1.0, spike_max_hold=10,
            )
            self._dynamic_strategies[pair] = strategy
            direction = "BULL" if coin.momentum_score > 0 else "BEAR"
            liq_tag = " [LOW-LIQ]" if coin.is_low_liquidity else ""
            logger.info("Dynamic strategy added: {} [{}]{} (1h:{:+.1f}% 24h:{:+.1f}%)",
                        pair, direction, liq_tag, coin.change_1h, coin.change_24h)

    async def _on_news(self, item: NewsItem) -> None:
        self._recent_news.append(item)
        if len(self._recent_news) > 200:
            self._recent_news = self._recent_news[-200:]

        if item.matched_symbols and abs(item.sentiment_score) > 0.3:
            logger.info("News [{}]: {} (symbols: {}, sentiment: {})",
                        item.source, item.headline, item.matched_symbols, item.sentiment)
            await self.notifier.alert_news(item.headline, item.matched_symbols, item.source)

    async def _log_status(self) -> None:
        now = datetime.now(timezone.utc)
        if self._last_status_log and (now - self._last_status_log).seconds < self._status_interval:
            return
        self._last_status_log = now

        logger.info(self.target.status_report())
        logger.info(self.risk.risk_summary())
        logger.info(self.scanner.scan_summary())
        logger.info("Active strategies: {} static + {} dynamic",
                     len(self._strategies), len(self._dynamic_strategies))

        for sym, sp in self.orders.scaler.active_positions.items():
            logger.info("  {}", sp.status_line())

        stops = self.orders.trailing.active_stops
        if stops:
            for sym, ts in stops.items():
                be_tag = " [BE-LOCKED]" if ts.breakeven_locked else ""
                liq_tag = " [LOW-LIQ]" if ts.low_liquidity else ""
                logger.info("  Trail {}: stop={:.6f} peak={:.6f} pnl={:+.1f}% active={}{}{}",
                            sym, ts.current_stop, ts.peak_price, ts.pnl_from_stop,
                            ts.activated, be_tag, liq_tag)

        hedges = self.orders.hedger.active_pairs
        if hedges:
            for sym, hp in hedges.items():
                logger.info("  {}", hp.status_line())

    async def _check_daily_reset(self) -> None:
        now = datetime.now(timezone.utc)
        if now.hour == 0 and now.minute < 2:
            balance_map = await self.exchange.fetch_balance()
            balance = balance_map.get("USDT", 0.0)
            self.target.update_balance(balance)
            positions = await self.exchange.fetch_positions()

            logger.info("=== DAILY SUMMARY ===")
            logger.info(self.target.status_report())
            logger.info("Total growth since start: {:.1f}%", self.target.total_growth_pct)

            compound_report = self.target.compound_report()
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

    # --- PRIMARY: Scalping / hit-and-run (WINNERS mode) ---
    bot.add_strategy("compound_momentum", "BTC/USDT", market_type="futures")
    bot.add_strategy("compound_momentum", "ETH/USDT", market_type="futures")

    # --- Market open volatility scalps (WINNERS mode) ---
    bot.add_strategy("market_open_volatility", "BTC/USDT", market_type="futures")
    bot.add_strategy("market_open_volatility", "ETH/USDT", market_type="futures")

    # --- RARE: Swing opportunity (PYRAMID mode -- DCA down, lever up, ride) ---
    bot.add_strategy("swing_opportunity", "BTC/USDT", market_type="futures")
    bot.add_strategy("swing_opportunity", "ETH/USDT", market_type="futures")

    # --- Scanner adds dynamic strategies for trending coins automatically ---
    # bot.add_strategy("compound_momentum", "SOL/USDT", market_type="futures")

    loop = asyncio.new_event_loop()

    def _shutdown(sig: int, frame: object) -> None:
        logger.info("Received signal {}, shutting down...", sig)
        loop.create_task(bot.stop())

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        loop.run_until_complete(bot.start())
    except KeyboardInterrupt:
        loop.run_until_complete(bot.stop())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
