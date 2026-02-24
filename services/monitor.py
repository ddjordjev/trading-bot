"""Monitoring service (runs in-process inside the hub).

Polls external data sources (TradingView, CoinMarketCap, CoinGecko,
Fear & Greed, liquidations, macro calendar, whale sentiment,
CryptoBubbles) and writes results to HubState (in-memory).

Adaptive intensity based on bot deployment status:
- HUNTING (idle, looking for trades): full-speed polling
- ACTIVE (some positions, still has capacity): normal polling
- DEPLOYED (fully deployed, running well): background polling
- STRESSED (positions losing): elevated polling for exit/hedge intel
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import UTC, datetime

from loguru import logger

from config.settings import Settings, get_settings
from hub.state import HubState
from intel.coingecko import CoinGeckoClient
from intel.coinmarketcap import CoinMarketCapClient
from intel.fear_greed import FearGreedClient
from intel.liquidations import LiquidationMonitor
from intel.macro_calendar import MacroCalendar
from intel.openclaw import OpenClawClient
from intel.tradingview import TradingViewClient
from intel.whale_sentiment import WhaleSentiment
from news import NewsItem, NewsMonitor
from scanner.binance_futures import BinanceFuturesScanner
from scanner.trending import TrendingScanner
from services.signal_generator import SignalGenerator
from shared.models import (
    BotDeploymentStatus,
    DeploymentLevel,
    ExtremeCandidate,
    ExtremeWatchlist,
    IntelSnapshot,
    SignalPriority,
    TradeProposal,
    TradeQueue,
    TrendingSnapshot,
    TVSymbolSnapshot,
)

# Poll interval multipliers by deployment level
INTENSITY_TABLE: dict[DeploymentLevel, dict[str, float]] = {
    #                         base_mult  tv_mult  scanner_mult  intel_mult
    DeploymentLevel.HUNTING: {"base": 1.0, "tv": 1.0, "scanner": 1.0, "intel": 1.0},
    DeploymentLevel.ACTIVE: {"base": 1.0, "tv": 1.0, "scanner": 1.5, "intel": 1.0},
    DeploymentLevel.DEPLOYED: {"base": 3.0, "tv": 5.0, "scanner": 5.0, "intel": 2.0},
    DeploymentLevel.STRESSED: {"base": 0.7, "tv": 0.5, "scanner": 2.0, "intel": 0.5},
}


class MonitorService:
    """Hub-integrated monitoring with adaptive poll rates."""

    def __init__(self, settings: Settings | None = None, state: HubState | None = None):
        self.settings = settings or get_settings()
        self.state: HubState = state or HubState()

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
        self.openclaw = OpenClawClient(
            enabled=self._as_bool(getattr(self.settings, "openclaw_enabled", False), False),
            base_url=str(getattr(self.settings, "openclaw_url", "") or ""),
            token=str(getattr(self.settings, "openclaw_token", "") or ""),
            poll_interval=self._as_int(getattr(self.settings, "openclaw_poll_interval", 120), 120, min_value=15),
            timeout_seconds=self._as_int(getattr(self.settings, "openclaw_timeout_seconds", 8), 8, min_value=2),
        )
        self.scanner = TrendingScanner(
            poll_interval=60,
            min_volume_24h=5_000_000,
            min_market_cap=50_000_000,
        )
        cex_enabled = self._as_bool(getattr(self.settings, "cex_scanner_enabled", True), True)
        binance_enabled = self._as_bool(getattr(self.settings, "binance_scanner_enabled", True), True)
        binance_poll = self._as_int(getattr(self.settings, "binance_scanner_poll_interval", 60), 60, min_value=30)
        binance_min_vol = self._as_float(
            getattr(self.settings, "binance_scanner_min_quote_volume", 5_000_000.0),
            5_000_000.0,
            min_value=0.0,
        )
        binance_top = self._as_int(getattr(self.settings, "binance_scanner_top_movers_count", 15), 15, min_value=1)
        binance_hist = self._as_int(getattr(self.settings, "binance_scanner_history_hours", 24), 24, min_value=1)
        binance_retention = self._as_int(getattr(self.settings, "binance_scanner_retention_days", 7), 7, min_value=1)
        self.binance_scanner = BinanceFuturesScanner(
            enabled=cex_enabled and binance_enabled,
            poll_interval=binance_poll,
            min_quote_volume=binance_min_vol,
            top_movers_count=binance_top,
            history_hours=binance_hist,
            retention_days=binance_retention,
        )
        self.news = NewsMonitor(self.settings)
        self._recent_news: list[NewsItem] = []

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
        self._last_symbols_refresh = 0.0
        self._last_analytics_refresh = 0.0
        self._last_ta_refresh = 0.0
        self._exchange_symbols: dict[str, set[str]] = {}
        self._candle_fetcher: object | None = None
        self._last_open_db_symbols: set[str] = set()

    @staticmethod
    def _intensity_for_level(level: DeploymentLevel) -> dict[str, float]:
        if level in INTENSITY_TABLE:
            return INTENSITY_TABLE[level]
        # IDLE/WINDING_DOWN should not crash monitor loop; use conservative defaults.
        return INTENSITY_TABLE[DeploymentLevel.HUNTING]

    @staticmethod
    def _as_bool(value: object, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            low = value.strip().lower()
            if low in {"1", "true", "yes", "on"}:
                return True
            if low in {"0", "false", "no", "off"}:
                return False
        return default

    @staticmethod
    def _as_int(value: object, default: int, *, min_value: int) -> int:
        try:
            parsed = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            parsed = default
        return max(min_value, parsed)

    @staticmethod
    def _as_float(value: object, default: float, *, min_value: float) -> float:
        try:
            parsed = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            parsed = default
        return max(min_value, parsed)

    @staticmethod
    def _pair_symbol(symbol: str) -> str:
        raw = (symbol or "").upper().split(":")[0]
        if "/" in raw:
            return raw
        if raw.endswith("USDT") and len(raw) > 4:
            return f"{raw[:-4]}/USDT"
        if raw.endswith("USD") and len(raw) > 3:
            return f"{raw[:-3]}/USD"
        return f"{raw}/USDT"

    @classmethod
    def _symbol_key(cls, symbol: str) -> str:
        return cls._pair_symbol(symbol).split("/", 1)[0]

    async def start(self) -> None:
        logger.info("=" * 50)
        logger.info("MONITOR SERVICE v1.0")
        logger.info("Adaptive intensity: ON")
        logger.info("Sources: F&G, Liquidations, Macro, Whales, TV, CMC, CoinGecko, OpenClaw, Scanner, BinanceScanner")
        logger.info("=" * 50)

        self._running = True

        if isinstance(self.state, HubState):
            try:
                from hub.candle_fetcher import CandleFetcher

                exchange_id = self.settings.exchange if hasattr(self.settings, "exchange") else "binance"
                sandbox = self.settings.trading_mode in ("paper_live",)
                self._candle_fetcher = CandleFetcher(exchange_id=exchange_id, sandbox=sandbox)
                logger.info("Candle fetcher initialized: {} (sandbox={})", exchange_id, sandbox)
            except Exception as e:
                logger.warning("Candle fetcher init failed (TA disabled): {}", e)

        await self._seed_exchange_symbols()

        await self.fear_greed.start()
        await self.liquidations.start()
        await self.macro.start()
        await self.whales.start()
        await self.tv.start()
        await self.cmc.start()
        await self.gecko.start()
        await self.openclaw.start()
        await self.scanner.start()
        await self.binance_scanner.start()
        await self.news.start()
        self.news.on_news(self._on_news)

        await self._run_loop()

    async def stop(self) -> None:
        self._running = False
        if self._candle_fetcher is not None:
            with contextlib.suppress(Exception):
                await self._candle_fetcher.close()
        await self.fear_greed.stop()
        await self.liquidations.stop()
        await self.macro.stop()
        await self.whales.stop()
        await self.tv.stop()
        await self.cmc.stop()
        await self.gecko.stop()
        await self.openclaw.stop()
        await self.scanner.stop()
        await self.binance_scanner.stop()
        await self.news.stop()
        logger.info("Monitor service stopped")

    def is_openclaw_enabled(self) -> bool:
        return bool(getattr(self.openclaw, "is_enabled", self.openclaw.enabled))

    async def set_openclaw_enabled(self, enabled: bool) -> bool:
        """Runtime toggle for OpenClaw advisory ingestion."""
        if enabled and not self.openclaw.base_url:
            logger.warning("OpenClaw enable requested but URL is empty")
            return False

        enabled_now = await self.openclaw.set_enabled(enabled)
        if enabled_now:
            # Try one immediate refresh so module state updates without waiting poll interval.
            with contextlib.suppress(Exception):
                await self.openclaw.fetch_once()
        return enabled_now

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

    # ---- Exchange symbol management (hub fetches directly) ----

    _SUPPORTED_EXCHANGES = ("binance", "mexc", "bybit")

    async def _seed_exchange_symbols(self) -> None:
        """On startup: load from DB, then refresh from exchanges."""
        try:
            from db.hub_store import HubDB

            db = HubDB()
            db.connect()
            cached = db.load_all_exchange_symbols()
            db.close()
            if cached:
                self._exchange_symbols = cached
                self.signal_gen.update_exchange_symbols(cached)
                total = sum(len(s) for s in cached.values())
                logger.info(
                    "Exchange symbols seeded from DB: {} exchanges, {} symbols",
                    len(cached),
                    total,
                )
        except Exception as e:
            logger.warning("DB seed for exchange symbols failed: {}", e)

        await self._fetch_exchange_symbols()

    async def _fetch_exchange_symbols(self) -> None:
        """Fetch symbol lists from all supported exchanges via CCXT."""
        import ccxt.async_support as ccxt

        fresh: dict[str, set[str]] = {}
        for exchange_id in self._SUPPORTED_EXCHANGES:
            cls = getattr(ccxt, exchange_id, None)
            if cls is None:
                continue
            ex = cls({"enableRateLimit": True})
            try:
                # In paper_live with Binance, gate symbols by testnet availability
                # so bots/scanner do not surface pairs not tradable on demo futures.
                if exchange_id == "binance" and self.settings.trading_mode == "paper_live":
                    with contextlib.suppress(Exception):
                        ex.set_sandbox_mode(True)
                await ex.load_markets()
                symbols = {s.split(":")[0] for s in ex.markets}
                fresh[exchange_id.upper()] = symbols
                if exchange_id == "binance" and self.settings.trading_mode == "paper_live":
                    logger.info("Fetched {} symbols from {} TESTNET", len(symbols), exchange_id.upper())
                else:
                    logger.info("Fetched {} symbols from {}", len(symbols), exchange_id.upper())
            except Exception as e:
                logger.warning("Failed to fetch symbols from {}: {}", exchange_id.upper(), e)
            finally:
                with contextlib.suppress(Exception):
                    await ex.close()

        if fresh:
            self._exchange_symbols = fresh
            self.signal_gen.update_exchange_symbols(fresh)
            all_syms = [s for syms in fresh.values() for s in syms]
            if all_syms:
                self.scanner.set_exchange_symbols(all_syms)
            self.binance_scanner.set_exchange_symbols(fresh.get("BINANCE", set()))
            self._last_symbols_refresh = time.monotonic()

            try:
                from db.hub_store import HubDB

                db = HubDB()
                db.connect()
                for ex_name, syms in fresh.items():
                    db.save_exchange_symbols(ex_name, syms)
                db.close()
            except Exception as e:
                logger.warning("Failed to persist exchange symbols to DB: {}", e)

    async def _run_loop(self) -> None:
        tick_count = 0
        while self._running:
            try:
                all_statuses = self.state.read_all_bot_statuses()
                combined = self._aggregate_bot_statuses(all_statuses)
                self._update_intensity(combined)

                multipliers = self._intensity_for_level(self._current_level)
                now = time.monotonic()

                # Refresh exchange symbols every 5 min (retry every tick while empty)
                sym_interval = 300 if self._exchange_symbols else 30
                if now - self._last_symbols_refresh >= sym_interval:
                    await self._fetch_exchange_symbols()

                # TradingView: refresh active symbols
                tv_interval = self.settings.tv_poll_interval * multipliers["tv"]
                if now - self._last_tv_refresh >= tv_interval:
                    await self._refresh_tv(combined)
                    self._last_tv_refresh = now

                # Scanner (CryptoBubbles + CMC + CoinGecko merge happens inside)
                scanner_interval = 60 * multipliers["scanner"]
                if now - self._last_scanner_refresh >= scanner_interval:
                    self._refresh_scanner_symbols()
                    self._last_scanner_refresh = now

                # Build and write intel snapshot
                snapshot = self._build_snapshot(multipliers)
                self.state.write_intel(snapshot)

                # Build extreme watchlist from scanner data
                try:
                    self._build_extreme_watchlist()
                except Exception as e:
                    logger.warning("Extreme watchlist error: {}", e)

                # Inject extreme movers into the trade queue as CRITICAL proposals
                try:
                    self._queue_extreme_proposals()
                except Exception as e:
                    logger.warning("Extreme proposal injection error: {}", e)

                # Feed analytics to signal generator (every 60s)
                if now - self._last_analytics_refresh >= 60:
                    try:
                        analytics_snap = self.state.read_analytics()
                        if analytics_snap.weights:
                            self.signal_gen.update_analytics(analytics_snap)
                    except Exception as e:
                        logger.warning("Analytics feed error: {}", e)
                    self._last_analytics_refresh = now

                # Feed rejection history so signal generator avoids re-proposing rejected combos
                try:
                    rej_records = self.state.get_rejection_history()
                    if rej_records:
                        rej_tuples = {k: (r.reason, r.timestamp, r.count) for k, r in rej_records.items()}
                        self.signal_gen.update_rejections(rej_tuples)
                    self.state.purge_old_rejections()
                except Exception as e:
                    logger.warning("Rejection feed error: {}", e)

                # Generate proposals into a staging queue, then route to per-bot queues
                try:
                    staging_queue = TradeQueue()
                    staging_queue = self.signal_gen.generate(snapshot, staging_queue)

                    # Hub-side technical analysis (every 120s when candle fetcher is available)
                    ta_interval = 120 * multipliers.get("tv", 1.0)
                    if self._candle_fetcher and now - self._last_ta_refresh >= ta_interval:
                        try:
                            ta_candidates = self._build_ta_candidates(snapshot)
                            if ta_candidates:
                                staging_queue = await self.signal_gen.generate_technical_signals(
                                    ta_candidates, self._candle_fetcher, staging_queue
                                )
                        except Exception as e:
                            logger.warning("Technical analysis error: {}", e)
                        self._last_ta_refresh = now

                    self._route_to_bots(staging_queue, all_statuses)
                except Exception as e:
                    logger.warning("Signal generation/routing error: {}", e)

                if tick_count % 10 == 0:
                    bot_queues = {s.bot_id: s for s in all_statuses}
                    bot_summary = (
                        ", ".join(f"{bid}:{s.open_positions}/{s.max_positions}" for bid, s in bot_queues.items())
                        or "no bots"
                    )
                    logger.info(
                        "Monitor [{}] | mult={:.1f}x | sources={} | movers={} | tv={} | bots: {}",
                        self._current_level.value,
                        multipliers["base"],
                        len(snapshot.sources_active),
                        len(snapshot.hot_movers),
                        len(snapshot.tv_analyses),
                        bot_summary,
                    )
                    if self.binance_scanner.enabled:
                        logger.info(
                            "BinanceScanner | latest={} hot={}",
                            len(self.binance_scanner.latest_scan),
                            len(self.binance_scanner.hot_movers),
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
            mult = self._intensity_for_level(self._current_level)["base"]
            logger.info("Monitor intensity: {} -> {} (poll mult: {:.1f}x)", old.value, self._current_level.value, mult)

    @staticmethod
    def _aggregate_bot_statuses(statuses: list[BotDeploymentStatus]) -> BotDeploymentStatus:
        """Combine per-bot statuses into one view for intensity decisions.

        Uses the most constrained level (STRESSED > DEPLOYED > ACTIVE > HUNTING)
        and sums positions across bots.
        """
        if not statuses:
            return BotDeploymentStatus()

        priority = {
            DeploymentLevel.STRESSED: 0,
            DeploymentLevel.DEPLOYED: 1,
            DeploymentLevel.ACTIVE: 2,
            DeploymentLevel.HUNTING: 3,
        }
        worst = max(statuses, key=lambda s: -priority.get(s.level, 99))
        return BotDeploymentStatus(
            bot_id="aggregated",
            level=worst.level,
            open_positions=sum(s.open_positions for s in statuses),
            max_positions=sum(s.max_positions for s in statuses),
            daily_pnl_pct=sum(s.daily_pnl_pct for s in statuses) / len(statuses),
            should_trade=any(s.should_trade for s in statuses),
            avg_position_health=min((s.avg_position_health for s in statuses), default=0.0),
            worst_position_pnl=min((s.worst_position_pnl for s in statuses), default=0.0),
        )

    def _route_to_bots(
        self,
        staging: TradeQueue,
        bot_statuses: list[BotDeploymentStatus],
    ) -> None:
        """Route proposals from staging queue to the shared hub queue.

        Drops proposals for symbols not available on any connected exchange.
        Removes exchanges where the symbol is already held by a bot — prevents
        multiple bots on the same account from piling into the same position.
        If no exchanges remain after filtering, the proposal is dropped.
        Filtering by bot style happens at read time in /internal/report.
        """
        from hub.state import HubState

        if not isinstance(self.state, HubState):
            logger.warning("_route_to_bots called without HubState (got {}) — skipping", type(self.state).__name__)
            return

        all_tradeable: set[str] = set()
        for syms in self._exchange_symbols.values():
            all_tradeable |= syms
        paper_live_mode = str(getattr(self.settings, "trading_mode", "") or "").lower() == "paper_live"
        binance_demo_symbols = self._exchange_symbols.get("BINANCE", set()) if paper_live_mode else set()

        open_db_symbols: set[str] = set()
        hub = None
        try:
            from pathlib import Path

            from db.hub_store import HubDB

            hub = HubDB(path=Path("data/hub.db"))
            hub.connect()
            open_db_symbols = hub.get_open_trade_symbols()
            self._last_open_db_symbols = set(open_db_symbols)
        except Exception as e:
            logger.warning("Skipping queue routing: failed reading open trades from hub.db: {}", e)
            return
        finally:
            with contextlib.suppress(Exception):
                if hub is not None:
                    hub.close()

        existing = self.state.read_trade_queue()
        new_count = 0
        skipped = 0
        deduped = 0
        all_proposals = staging.proposals
        for proposal in all_proposals:
            if proposal.consumed or proposal.is_expired:
                continue
            if all_tradeable and proposal.symbol not in all_tradeable:
                skipped += 1
                continue
            if paper_live_mode and binance_demo_symbols and proposal.symbol not in binance_demo_symbols:
                # In paper_live we only queue symbols tradable on Binance demo/testnet.
                skipped += 1
                continue
            if existing.has_symbol(proposal.symbol):
                deduped += 1
                continue
            if proposal.symbol in open_db_symbols:
                deduped += 1
                continue
            available = [
                ex for ex in proposal.supported_exchanges if proposal.symbol not in self.state.get_active_symbols(ex)
            ]
            if paper_live_mode and binance_demo_symbols:
                if proposal.symbol in self.state.get_active_symbols("BINANCE"):
                    deduped += 1
                    continue
                available = ["BINANCE"]
            if proposal.supported_exchanges and not available:
                deduped += 1
                continue
            if available != proposal.supported_exchanges:
                proposal.supported_exchanges = available
            before = existing.total
            existing.add(proposal)
            if existing.total > before:
                new_count += 1
        purged = existing.purge_stale()
        self.state.write_trade_queue(existing)
        if new_count or purged or skipped or deduped:
            logger.info(
                "Trade queue updated: +{} new, -{} purged, ~{} skipped (no exchange), ~{} deduped (already traded), {} total ({} pending)",
                new_count,
                purged,
                skipped,
                deduped,
                existing.total,
                existing.pending_count,
            )

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
            logger.warning("TV refresh error: {}", e)

    def _refresh_scanner_symbols(self) -> None:
        """Add symbols from CMC/CoinGecko to the TV watch list.

        Only includes symbols that exist on at least one connected exchange.
        """
        all_tradeable: set[str] = set()
        for syms in self._exchange_symbols.values():
            all_tradeable |= syms

        extra: set[str] = set()
        for coin in self.cmc.all_interesting[:10]:
            if coin.is_tradable_size:
                pair = f"{coin.symbol.upper()}/USDT"
                if not all_tradeable or pair in all_tradeable:
                    extra.add(pair)
        for coin in self.gecko.all_interesting[:10]:
            if coin.volume_24h >= 1_000_000:
                pair = f"{coin.symbol.upper()}/USDT"
                if not all_tradeable or pair in all_tradeable:
                    extra.add(pair)

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
            snap.liquidation_24h_text = liq.total_24h_text
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
        self._apply_openclaw(snapshot=snap)

        # Trending (CEX-first): Binance scanner is the base stream; legacy
        # scanner remains additive confidence/discovery.
        merged_hot: list[TrendingSnapshot] = []
        seen_hot: set[str] = set()

        for c in self.binance_scanner.hot_movers:
            key = self._symbol_key(c.symbol)
            if key in seen_hot:
                continue
            seen_hot.add(key)
            merged_hot.append(
                TrendingSnapshot(
                    symbol=self._pair_symbol(c.symbol),
                    name=c.name,
                    price=c.price,
                    market_cap=c.market_cap,
                    volume_24h=c.volume_24h,
                    change_5m=c.change_5m,
                    change_1h=c.change_1h,
                    change_24h=c.change_24h,
                    change_7d=c.change_7d,
                    momentum_score=c.momentum_score,
                    is_low_liquidity=c.is_low_liquidity,
                    source="binance_scanner",
                    cex_confidence=c.cex_confidence,
                    cex_vol_accel=c.cex_vol_accel,
                    cex_score=c.cex_score,
                    cex_funding_rate=c.cex_funding_rate,
                    cex_change_1m=c.cex_change_1m,
                    cex_change_4h=c.cex_change_4h,
                    cex_change_1d=c.cex_change_1d,
                    cex_change_1w=c.cex_change_1w,
                    cex_change_3w=c.cex_change_3w,
                    cex_change_1mo=c.cex_change_1mo,
                    cex_change_3mo=c.cex_change_3mo,
                    cex_change_1y=c.cex_change_1y,
                )
            )

        for c in self.scanner.hot_movers:
            key = self._symbol_key(c.symbol)
            if key in seen_hot:
                continue
            seen_hot.add(key)
            merged_hot.append(
                TrendingSnapshot(
                    symbol=self._pair_symbol(c.symbol),
                    name=c.name,
                    price=c.price,
                    market_cap=c.market_cap,
                    volume_24h=c.volume_24h,
                    change_5m=c.change_5m,
                    change_1h=c.change_1h,
                    change_24h=c.change_24h,
                    change_7d=c.change_7d,
                    momentum_score=c.momentum_score,
                    is_low_liquidity=c.is_low_liquidity,
                    source="cryptobubbles",
                )
            )

        snap.hot_movers = merged_hot

        snap.cmc_trending = [
            TrendingSnapshot(
                symbol=c.symbol,
                name=c.name,
                price=c.price,
                market_cap=c.market_cap,
                volume_24h=c.volume_24h,
                change_5m=float(getattr(c, "change_5m", 0.0) or 0.0),
                change_1h=float(getattr(c, "change_1h", 0.0) or 0.0),
                change_24h=float(getattr(c, "change_24h", 0.0) or 0.0),
                change_7d=float(getattr(c, "change_7d", 0.0) or 0.0),
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
                change_5m=float(getattr(c, "change_5m", 0.0) or 0.0),
                change_1h=float(getattr(c, "change_1h", 0.0) or 0.0),
                change_24h=float(getattr(c, "change_24h", 0.0) or 0.0),
                change_7d=float(getattr(c, "change_7d", 0.0) or 0.0),
                source="coingecko",
            )
            for c in self.gecko.all_interesting[:15]
        ]

        # News
        snap.news_items = [
            {
                "headline": n.headline,
                "source": n.source,
                "url": n.url,
                "published": n.published.isoformat() if n.published else "",
                "matched_symbols": n.matched_symbols,
                "sentiment": n.sentiment,
                "sentiment_score": n.sentiment_score,
            }
            for n in self._recent_news[-50:]
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
        if self.openclaw.latest:
            sources.append("openclaw")
            ts["openclaw"] = now_iso
        if self.scanner.hot_movers:
            sources.append("scanner")
            ts["scanner"] = now_iso
        if self.binance_scanner.hot_movers:
            sources.append("binance_scanner")
            ts["binance_scanner"] = now_iso
        if self._recent_news:
            sources.append("news")
            ts["news"] = now_iso
        snap.sources_active = sources
        prev = self.state.read_intel()
        merged_ts = dict(prev.source_timestamps) if prev.source_timestamps else {}
        merged_ts.update(ts)
        snap.source_timestamps = merged_ts

        return snap

    def _apply_openclaw(self, snapshot: IntelSnapshot) -> None:
        """Merge OpenClaw advisory outputs into the cached hub snapshot."""
        data = self.openclaw.latest
        if data is None:
            return

        commentary = data.regime_commentary
        alt = data.alt_data
        snapshot.openclaw_regime = commentary.regime or "unknown"
        snapshot.openclaw_regime_confidence = float(commentary.confidence or 0.0)
        snapshot.openclaw_regime_why = list(commentary.why or [])
        snapshot.openclaw_sentiment_score = int(alt.sentiment_score or 50)
        snapshot.openclaw_long_short_ratio = float(alt.long_short_ratio or 0.0)
        snapshot.openclaw_liquidations_24h_usd = float(alt.liquidations_24h_usd or 0.0)
        snapshot.openclaw_open_interest_24h_usd = float(alt.open_interest_24h_usd or 0.0)
        snapshot.openclaw_idea_briefs = [i.model_dump() for i in data.idea_briefs[:10]]
        snapshot.openclaw_failure_triage = [t.model_dump() for t in data.failure_triage[:10]]
        snapshot.openclaw_experiments = [e.model_dump() for e in data.experiments[:10]]

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

    def _build_ta_candidates(self, snap: IntelSnapshot) -> list[str]:
        """Build list of symbols for hub-side technical analysis.

        Combines major coins, trending movers, and extreme candidates.
        Limited to avoid API rate limits.
        """
        candidates: list[str] = []
        seen: set[str] = set()

        for sym in self.signal_gen._major_symbols:
            if sym not in seen:
                candidates.append(sym)
                seen.add(sym)

        for mover in snap.hot_movers[:10]:
            sym = self._pair_symbol(mover.symbol)
            if sym not in seen and not mover.is_low_liquidity:
                candidates.append(sym)
                seen.add(sym)

        for mover in snap.cmc_trending[:5]:
            sym = f"{mover.symbol.upper()}/USDT"
            if sym not in seen:
                candidates.append(sym)
                seen.add(sym)

        return candidates[:20]

    def _queue_extreme_proposals(self) -> None:
        """Convert top extreme candidates into CRITICAL proposals and add to the hub queue."""
        from hub.state import HubState

        if not isinstance(self.state, HubState):
            return
        watchlist = self.state.read_extreme_watchlist()
        if not watchlist.candidates:
            return

        open_db_symbols: set[str] = set()
        hub = None
        try:
            from pathlib import Path

            from db.hub_store import HubDB

            hub = HubDB(path=Path("data/hub.db"))
            hub.connect()
            open_db_symbols = hub.get_open_trade_symbols()
            self._last_open_db_symbols = set(open_db_symbols)
        except Exception as e:
            logger.warning("Skipping extreme queueing: failed reading open trades from hub.db: {}", e)
            return
        finally:
            with contextlib.suppress(Exception):
                if hub is not None:
                    hub.close()

        existing = self.state.read_trade_queue()
        added = 0
        for cand in watchlist.candidates[:5]:
            if existing.has_symbol(cand.symbol):
                continue
            if cand.symbol in open_db_symbols:
                continue
            available = [ex for ex in cand.supported_exchanges if cand.symbol not in self.state.get_active_symbols(ex)]
            if cand.supported_exchanges and not available:
                continue
            side = "long" if cand.direction == "bull" else "short"
            proposal = TradeProposal(
                priority=SignalPriority.CRITICAL,
                symbol=cand.symbol,
                side=side,
                strategy="extreme_mover",
                reason=cand.reason,
                strength=min(1.0, cand.momentum_score / 50.0),
                market_type="futures",
                leverage=20,
                quick_trade=True,
                max_hold_minutes=30,
                tick_urgency="scalp",
                max_age_seconds=300,
                source="extreme_watchlist",
                target_bot="extreme",
                supported_exchanges=available or cand.supported_exchanges,
            )
            existing.add(proposal)
            added += 1

        if added:
            self.state.write_trade_queue(existing)
            logger.debug("Queued {} extreme proposals", added)

    def _build_extreme_watchlist(self) -> None:
        """Filter scanner data for extreme movers and write to shared state."""
        min_hourly = self.settings.extreme_min_hourly_move_pct
        min_vol = self.settings.extreme_min_volume_24h
        max_candidates = self.settings.extreme_max_candidates

        all_coins = list(self.binance_scanner.latest_scan) + list(self.scanner.latest_scan)
        if not all_coins:
            return

        all_tradeable = set()
        for syms in self._exchange_symbols.values():
            all_tradeable |= syms

        extreme: list[ExtremeCandidate] = []
        seen_pairs: set[str] = set()
        for coin in all_coins:
            hourly_abs = abs(coin.change_1h)
            if hourly_abs < min_hourly:
                continue
            if coin.volume_24h < min_vol:
                continue

            pair = coin.trading_pair
            if pair in seen_pairs:
                continue
            # Skip coins not available on any known exchange
            if all_tradeable and pair not in all_tradeable:
                continue

            direction = "bull" if coin.change_1h > 0 else "bear"
            score = hourly_abs * (coin.volume_24h / 1e6) ** 0.5

            reasons: list[str] = []
            reasons.append(f"1h: {coin.change_1h:+.1f}%")
            if abs(coin.change_5m) > 1.0:
                reasons.append(f"5m: {coin.change_5m:+.1f}%")
            reasons.append(f"vol: ${coin.volume_24h / 1e6:.0f}M")

            supported = [ex for ex, syms in self._exchange_symbols.items() if pair in syms]

            extreme.append(
                ExtremeCandidate(
                    symbol=pair,
                    direction=direction,
                    change_1h=coin.change_1h,
                    change_5m=coin.change_5m,
                    volume_24h=coin.volume_24h,
                    momentum_score=score,
                    reason=" | ".join(reasons),
                    supported_exchanges=supported,
                )
            )
            seen_pairs.add(pair)

        extreme.sort(key=lambda c: c.momentum_score, reverse=True)
        watchlist = ExtremeWatchlist(candidates=extreme[:max_candidates])
        self.state.write_extreme_watchlist(watchlist)

        if extreme:
            logger.debug("Extreme watchlist: {} candidates (top: {})", len(extreme[:max_candidates]), extreme[0].symbol)
