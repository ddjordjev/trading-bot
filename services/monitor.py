"""Standalone monitoring service.

Runs independently of the trading bot. Polls external data sources
(TradingView, CoinMarketCap, CoinGecko, Fear & Greed, liquidations,
macro calendar, whale sentiment, CryptoBubbles) and writes results
to data/intel_state.json.

Adaptive intensity:
- Reads data/bot_status.json to know how busy the bot is
- HUNTING (idle, looking for trades): full-speed polling
- ACTIVE (some positions, still has capacity): normal polling
- DEPLOYED (fully deployed, running well): background polling
- STRESSED (positions losing): elevated polling for exit/hedge intel

When the bot is fully deployed and positions are profitable,
there's no need to hammer APIs for new opportunities.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

from loguru import logger

from config.settings import Settings, get_settings
from intel.coingecko import CoinGeckoClient
from intel.coinmarketcap import CoinMarketCapClient
from intel.fear_greed import FearGreedClient
from intel.liquidations import LiquidationMonitor
from intel.macro_calendar import MacroCalendar
from intel.tradingview import TradingViewClient
from intel.whale_sentiment import WhaleSentiment
from scanner.trending import TrendingScanner
from services.signal_generator import SignalGenerator
from shared.models import (
    BotDeploymentStatus,
    DeploymentLevel,
    IntelSnapshot,
    SignalPriority,
    TrendingSnapshot,
    TVSymbolSnapshot,
)
from shared.state import SharedState

# Poll interval multipliers by deployment level
INTENSITY_TABLE: dict[DeploymentLevel, dict[str, float]] = {
    #                         base_mult  tv_mult  scanner_mult  intel_mult
    DeploymentLevel.HUNTING: {"base": 1.0, "tv": 1.0, "scanner": 1.0, "intel": 1.0},
    DeploymentLevel.ACTIVE: {"base": 1.0, "tv": 1.0, "scanner": 1.5, "intel": 1.0},
    DeploymentLevel.DEPLOYED: {"base": 3.0, "tv": 5.0, "scanner": 5.0, "intel": 2.0},
    DeploymentLevel.STRESSED: {"base": 0.7, "tv": 0.5, "scanner": 2.0, "intel": 0.5},
}


class MonitorService:
    """Standalone monitoring process with adaptive poll rates."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.state = SharedState()

        # Clients
        self.fear_greed = FearGreedClient(poll_interval=3600)
        self.liquidations = LiquidationMonitor(
            poll_interval=300,
            api_key=self.settings.coinglass_api_key,
        )
        self.macro = MacroCalendar(poll_interval=1800)
        self.whales = WhaleSentiment(
            symbols=self.settings.intel_symbol_list,
            poll_interval=300,
            coinglass_key=self.settings.coinglass_api_key,
        )
        self.tv = TradingViewClient(
            exchange=self.settings.tv_exchange,
            intervals=self.settings.tv_interval_list,
            poll_interval=self.settings.tv_poll_interval,
        )
        self.cmc = CoinMarketCapClient(
            api_key=self.settings.cmc_api_key,
            poll_interval=self.settings.cmc_poll_interval,
        )
        self.gecko = CoinGeckoClient(
            api_key=self.settings.coingecko_api_key,
            poll_interval=self.settings.coingecko_poll_interval,
        )
        self.scanner = TrendingScanner(
            poll_interval=60,
            min_volume_24h=5_000_000,
            min_market_cap=50_000_000,
        )

        mkt = "futures" if self.settings.futures_allowed else "spot"
        self.signal_gen = SignalGenerator(
            preferred_market_type=mkt,
            major_symbols=set(self.settings.major_symbol_list),
        )

        self._running = False
        self._current_level = DeploymentLevel.HUNTING
        self._base_tick = 30  # seconds between monitor ticks
        self._tv_symbols: list[str] = ["BTC/USDT", "ETH/USDT"]
        self._last_tv_refresh = 0.0
        self._last_scanner_refresh = 0.0

    async def start(self) -> None:
        logger.info("=" * 50)
        logger.info("MONITOR SERVICE v1.0")
        logger.info("Adaptive intensity: ON")
        logger.info("Sources: F&G, Liquidations, Macro, Whales, TV, CMC, CoinGecko, Scanner")
        logger.info("=" * 50)

        self._running = True

        await self.fear_greed.start()
        await self.liquidations.start()
        await self.macro.start()
        await self.whales.start()
        await self.tv.start()
        await self.cmc.start()
        await self.gecko.start()
        await self.scanner.start()

        await self._run_loop()

    async def stop(self) -> None:
        self._running = False
        await self.fear_greed.stop()
        await self.liquidations.stop()
        await self.macro.stop()
        await self.whales.stop()
        await self.tv.stop()
        await self.cmc.stop()
        await self.gecko.stop()
        await self.scanner.stop()
        logger.info("Monitor service stopped")

    async def _run_loop(self) -> None:
        tick_count = 0
        while self._running:
            try:
                bot_status = self.state.read_bot_status()
                self._update_intensity(bot_status)

                multipliers = INTENSITY_TABLE[self._current_level]
                now = time.monotonic()

                # TradingView: refresh active symbols
                tv_interval = self.settings.tv_poll_interval * multipliers["tv"]
                if now - self._last_tv_refresh >= tv_interval:
                    await self._refresh_tv(bot_status)
                    self._last_tv_refresh = now

                # Scanner (CryptoBubbles + CMC + CoinGecko merge happens inside)
                scanner_interval = 60 * multipliers["scanner"]
                if now - self._last_scanner_refresh >= scanner_interval:
                    self._refresh_scanner_symbols()
                    self._last_scanner_refresh = now

                # Build and write intel snapshot
                snapshot = self._build_snapshot(multipliers)
                self.state.write_intel(snapshot)

                # Generate trade proposals from the intel snapshot
                try:
                    trade_queue = self.state.read_trade_queue()
                    trade_queue = self.signal_gen.generate(snapshot, trade_queue)
                    self.state.write_trade_queue(trade_queue)
                except Exception as e:
                    logger.debug("Signal generator error: {}", e)

                if tick_count % 10 == 0:
                    tq = self.state.read_trade_queue()
                    logger.info(
                        "Monitor [{}] | mult={:.1f}x | sources={} | movers={} | tv={} | queue: C={} D={} S={}",
                        self._current_level.value,
                        multipliers["base"],
                        len(snapshot.sources_active),
                        len(snapshot.hot_movers),
                        len(snapshot.tv_analyses),
                        len(tq.get_actionable(SignalPriority.CRITICAL)),
                        len(tq.get_actionable(SignalPriority.DAILY)),
                        len(tq.get_actionable(SignalPriority.SWING)),
                    )

                tick_count += 1
                sleep_time = self._base_tick * multipliers["base"]
                await asyncio.sleep(sleep_time)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Monitor tick error: {}", e)
                await asyncio.sleep(10)

    def _update_intensity(self, bot_status: BotDeploymentStatus) -> None:
        old = self._current_level
        self._current_level = bot_status.level

        if old != self._current_level:
            mult = INTENSITY_TABLE[self._current_level]["base"]
            logger.info("Monitor intensity: {} -> {} (poll mult: {:.1f}x)", old.value, self._current_level.value, mult)

    async def _refresh_tv(self, bot_status: BotDeploymentStatus) -> None:
        """Refresh TradingView analysis, adapting to deployment state."""
        symbols_to_analyze = list(self._tv_symbols)

        if self._current_level == DeploymentLevel.HUNTING:
            # Full analysis: base symbols + trending movers
            for coin in self.scanner.hot_movers[:5]:
                pair = coin.trading_pair
                if pair not in symbols_to_analyze:
                    symbols_to_analyze.append(pair)

        elif self._current_level == DeploymentLevel.DEPLOYED:
            # Only BTC/ETH for general context -- don't waste API calls
            symbols_to_analyze = ["BTC/USDT", "ETH/USDT"]

        elif self._current_level == DeploymentLevel.STRESSED:
            # Analyze everything we're holding for exit signals
            symbols_to_analyze = list(self._tv_symbols)
            for coin in self.scanner.hot_movers[:3]:
                pair = coin.trading_pair
                if pair not in symbols_to_analyze:
                    symbols_to_analyze.append(pair)

        try:
            results = await self.tv.analyze_multi(symbols_to_analyze, "1h")
            if self._current_level != DeploymentLevel.DEPLOYED:
                await self.tv.analyze_multi(symbols_to_analyze[:5], "4h")
            logger.debug("TV refreshed: {} symbols", len(results))
        except Exception as e:
            logger.debug("TV refresh error: {}", e)

    def _refresh_scanner_symbols(self) -> None:
        """Add symbols from CMC/CoinGecko to the TV watch list."""
        extra: set[str] = set()
        for coin in self.cmc.all_interesting[:10]:
            if coin.is_tradable_size:
                extra.add(f"{coin.symbol.upper()}/USDT")
        for coin in self.gecko.all_interesting[:10]:
            if coin.volume_24h >= 1_000_000:
                extra.add(f"{coin.symbol.upper()}/USDT")

        base = {"BTC/USDT", "ETH/USDT"}
        self._tv_symbols = sorted(base | extra)

    def _build_snapshot(self, multipliers: dict[str, float]) -> IntelSnapshot:
        snap = IntelSnapshot()

        # Fear & Greed
        snap.fear_greed = self.fear_greed.value
        snap.fear_greed_bias = self.fear_greed.trade_direction_bias()

        # Liquidations
        liq = self.liquidations.latest
        if liq:
            snap.liquidation_24h = liq.total_24h
            snap.mass_liquidation = liq.is_mass_liquidation
            snap.liquidation_bias = self.liquidations.reversal_bias()

        # Macro
        snap.macro_event_imminent = self.macro.has_imminent_event()
        snap.macro_exposure_mult = self.macro.exposure_multiplier()
        snap.macro_spike_opportunity = self.macro.is_spike_opportunity()
        snap.next_macro_event = self.macro.next_event_info() or ""

        # Whale sentiment
        snap.whale_bias = self.whales.contrarian_bias("BTC")
        btc = self.whales.get("BTC")
        if btc:
            if btc.is_overleveraged_longs:
                snap.overleveraged_side = "longs"
            elif btc.is_overleveraged_shorts:
                snap.overleveraged_side = "shorts"

        # TradingView
        snap.tv_btc_consensus = self.tv.consensus("BTC/USDT")
        snap.tv_eth_consensus = self.tv.consensus("ETH/USDT")

        tv_snapshots = []
        for sym, analyses in self.tv.get_all_cached().items():
            for interval, analysis in analyses.items():
                tv_snapshots.append(
                    TVSymbolSnapshot(
                        symbol=sym,
                        interval=interval,
                        rating=analysis.summary_rating.value,
                        oscillators=analysis.oscillators_rating.value,
                        moving_averages=analysis.moving_averages_rating.value,
                        confidence=analysis.confidence,
                        rsi_14=analysis.rsi_14,
                        consensus=self.tv.consensus(sym),
                        signal_boost_long=self.tv.signal_boost(sym, "long"),
                        signal_boost_short=self.tv.signal_boost(sym, "short"),
                        updated_at=analysis.fetched_at.isoformat(),
                    )
                )
        snap.tv_analyses = tv_snapshots

        # Market regime
        snap.should_reduce_exposure = (
            snap.macro_event_imminent or self.fear_greed.is_extreme_greed or snap.overleveraged_side == "longs"
        )
        snap.regime = self._derive_regime(snap)
        snap.position_size_multiplier = self._compute_size_mult(snap)
        snap.preferred_direction = self._compute_direction(snap)

        # Trending
        snap.hot_movers = [
            TrendingSnapshot(
                symbol=c.symbol,
                name=c.name,
                price=c.price,
                market_cap=c.market_cap,
                volume_24h=c.volume_24h,
                change_1h=c.change_1h,
                change_24h=c.change_24h,
                change_7d=c.change_7d,
                momentum_score=c.momentum_score,
                is_low_liquidity=c.is_low_liquidity,
                source="cryptobubbles",
            )
            for c in self.scanner.hot_movers
        ]

        snap.cmc_trending = [
            TrendingSnapshot(
                symbol=c.symbol,
                name=c.name,
                price=c.price,
                market_cap=c.market_cap,
                volume_24h=c.volume_24h,
                change_1h=c.change_1h,
                change_24h=c.change_24h,
                change_7d=c.change_7d,
                source="coinmarketcap",
            )
            for c in self.cmc.all_interesting[:15]
        ]

        snap.coingecko_trending = [
            TrendingSnapshot(
                symbol=c.symbol,
                name=c.name,
                price=c.price,
                market_cap=c.market_cap,
                volume_24h=c.volume_24h,
                change_1h=c.change_1h,
                change_24h=c.change_24h,
                change_7d=c.change_7d,
                source="coingecko",
            )
            for c in self.gecko.all_interesting[:15]
        ]

        # Metadata
        now_iso = datetime.now(UTC).isoformat()
        snap.monitor_intensity = self._current_level.value
        snap.poll_multiplier = multipliers["base"]
        sources = []
        ts: dict[str, str] = {}
        if self.fear_greed.latest:
            sources.append("fear_greed")
            ts["fear_greed"] = now_iso
        if self.liquidations.latest:
            sources.append("liquidations")
            ts["liquidations"] = now_iso
        sources.append("macro")
        ts["macro"] = now_iso
        sources.append("whales")
        ts["whales"] = now_iso
        if self.tv._cache:
            sources.append("tradingview")
            ts["tradingview"] = now_iso
        if self.cmc.trending:
            sources.append("coinmarketcap")
            ts["coinmarketcap"] = now_iso
        if self.gecko.trending:
            sources.append("coingecko")
            ts["coingecko"] = now_iso
        if self.scanner.hot_movers:
            sources.append("scanner")
            ts["scanner"] = now_iso
        snap.sources_active = sources
        prev = self.state.read_intel()
        merged_ts = dict(prev.source_timestamps) if prev.source_timestamps else {}
        merged_ts.update(ts)
        snap.source_timestamps = merged_ts

        return snap

    def _derive_regime(self, snap: IntelSnapshot) -> str:
        if self.fear_greed.is_extreme_fear and snap.mass_liquidation:
            return "capitulation"
        if (self.fear_greed.is_extreme_greed and snap.macro_event_imminent) or (
            snap.overleveraged_side == "longs" and self.fear_greed.is_greed
        ):
            return "risk_off"
        if snap.should_reduce_exposure:
            return "caution"
        if self.fear_greed.is_fear or snap.mass_liquidation:
            return "risk_on"
        return "normal"

    def _compute_size_mult(self, snap: IntelSnapshot) -> float:
        fg = self.fear_greed.position_bias()
        liq = self.liquidations.aggression_boost()
        macro = snap.macro_exposure_mult
        return min(fg * liq * macro, 1.5)

    def _compute_direction(self, snap: IntelSnapshot) -> str:
        votes = {"long": 0, "short": 0, "neutral": 0}
        votes[snap.fear_greed_bias] += 2
        votes[snap.liquidation_bias] += 2 if snap.mass_liquidation else 1
        votes[snap.whale_bias] += 1
        tv_dir = snap.tv_btc_consensus
        if tv_dir in votes:
            votes[tv_dir] += 2

        if votes["long"] > votes["short"] and votes["long"] > votes["neutral"]:
            return "long"
        if votes["short"] > votes["long"] and votes["short"] > votes["neutral"]:
            return "short"
        return "neutral"
