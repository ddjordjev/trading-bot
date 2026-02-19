from __future__ import annotations

from enum import Enum

from loguru import logger
from pydantic import BaseModel

from intel.coingecko import CoinGeckoClient
from intel.coinmarketcap import CoinMarketCapClient
from intel.defillama import DeFiLlamaClient
from intel.fear_greed import FearGreedClient
from intel.glassnode import GlassnodeClient
from intel.liquidations import LiquidationMonitor
from intel.macro_calendar import MacroCalendar
from intel.santiment import SantimentClient
from intel.tradingview import TradingViewClient
from intel.whale_sentiment import WhaleSentiment


class MarketRegime(str, Enum):
    RISK_ON = "risk_on"  # fear + liquidations clearing + good conditions
    NORMAL = "normal"  # nothing extreme
    CAUTION = "caution"  # greed or pre-event or overleveraged
    RISK_OFF = "risk_off"  # extreme greed + FOMC imminent + overleveraged
    CAPITULATION = "capitulation"  # extreme fear + mass liquidation = BUY opportunity


class MarketCondition(BaseModel):
    """Combined assessment of all external intelligence sources."""

    regime: MarketRegime = MarketRegime.NORMAL
    fear_greed: int = 50
    fear_greed_bias: str = "neutral"
    liquidation_24h: float = 0.0
    mass_liquidation: bool = False
    liquidation_bias: str = "neutral"
    macro_event_imminent: bool = False
    macro_exposure_mult: float = 1.0
    macro_spike_opportunity: bool = False
    next_macro_event: str = ""
    whale_bias: str = "neutral"
    overleveraged_side: str = ""

    # TradingView consensus
    tv_btc_consensus: str = "neutral"
    tv_eth_consensus: str = "neutral"

    # DeFiLlama
    tvl_trend: str = "stable"

    # Santiment
    social_sentiment: str = "neutral"
    social_spike: bool = False

    # Glassnode
    on_chain_bias: str = "neutral"
    distribution_phase: bool = False
    accumulation_phase: bool = False

    # OI details
    btc_oi_total_usd: float = 0.0
    btc_oi_change_1h_pct: float = 0.0
    top_trader_long_ratio: float = 0.5

    # Composite multipliers for the bot
    position_size_multiplier: float = 1.0  # applied to all new entries
    should_reduce_exposure: bool = False
    preferred_direction: str = "neutral"  # "long", "short", "neutral"

    def summary_lines(self) -> list[str]:
        lines = [
            f"Regime: {self.regime.value}",
            f"F&G: {self.fear_greed} ({self.fear_greed_bias})",
            f"Liq: ${self.liquidation_24h / 1e6:.0f}M {'** MASS LIQ **' if self.mass_liquidation else ''}",
            f"Macro: {'IMMINENT' if self.macro_event_imminent else 'clear'} "
            f"(expo={self.macro_exposure_mult:.0%})"
            f"{' | ' + self.next_macro_event if self.next_macro_event else ''}",
            f"Whale: {self.whale_bias}"
            f"{' | overleveraged ' + self.overleveraged_side if self.overleveraged_side else ''}",
            f"TV: BTC={self.tv_btc_consensus} ETH={self.tv_eth_consensus}",
            f"=> size: {self.position_size_multiplier:.1f}x | "
            f"direction: {self.preferred_direction} | "
            f"reduce: {self.should_reduce_exposure}",
        ]
        return lines


class MarketIntel:
    """Orchestrates all external intelligence and produces a unified MarketCondition.

    Combines:
    - Fear & Greed Index (alternative.me)
    - Liquidation data (CoinGlass)
    - Macro calendar (ForexFactory)
    - Whale sentiment (CoinGlass funding/OI/L-S ratios)
    - TradingView technical analysis (scanner API)
    - CoinMarketCap trending/gainers (web API or pro API)
    - CoinGecko trending/market data (free or pro API)

    The bot reads MarketCondition each tick to adjust:
    - Position sizing (multiplier)
    - Trade direction (long/short/neutral bias)
    - Whether to reduce exposure (tighten stops, skip entries)
    - Whether it's a spike opportunity (macro event volatility)
    """

    def __init__(
        self,
        coinglass_key: str = "",
        symbols: list[str] | None = None,
        tv_exchange: str = "MEXC",
        cmc_api_key: str = "",
        coingecko_api_key: str = "",
        santiment_api_key: str = "",
        glassnode_api_key: str = "",
        defillama_enabled: bool = True,
    ):
        self.fear_greed = FearGreedClient(poll_interval=3600)
        self.liquidations = LiquidationMonitor(poll_interval=300, api_key=coinglass_key)
        self.macro = MacroCalendar(poll_interval=1800)
        self.whales = WhaleSentiment(
            symbols=symbols or ["BTC", "ETH"],
            poll_interval=300,
            coinglass_key=coinglass_key,
        )
        self.tradingview = TradingViewClient(
            exchange=tv_exchange,
            intervals=["1h", "4h", "1D"],
            poll_interval=120,
        )
        self.coinmarketcap = CoinMarketCapClient(
            api_key=cmc_api_key,
            poll_interval=300,
        )
        self.coingecko = CoinGeckoClient(
            api_key=coingecko_api_key,
            poll_interval=300,
        )
        self.defillama = DeFiLlamaClient(poll_interval=600)
        self.santiment = SantimentClient(api_key=santiment_api_key, poll_interval=600)
        self.glassnode = GlassnodeClient(api_key=glassnode_api_key, poll_interval=900)
        self._defillama_enabled = defillama_enabled
        self._condition = MarketCondition()

    async def start(self) -> None:
        await self.fear_greed.start()
        await self.liquidations.start()
        await self.macro.start()
        await self.whales.start()
        await self.tradingview.start()
        await self.coinmarketcap.start()
        await self.coingecko.start()
        if self._defillama_enabled:
            await self.defillama.start()
        await self.santiment.start()
        await self.glassnode.start()
        logger.info(
            "MarketIntel started -- all external feeds active "
            "(F&G, Liq, Macro, Whales, TV, CMC, CoinGecko, DeFiLlama, Santiment, Glassnode)"
        )

    async def stop(self) -> None:
        await self.fear_greed.stop()
        await self.liquidations.stop()
        await self.macro.stop()
        await self.whales.stop()
        await self.tradingview.stop()
        await self.coinmarketcap.stop()
        await self.coingecko.stop()
        await self.defillama.stop()
        await self.santiment.stop()
        await self.glassnode.stop()

    def assess(self) -> MarketCondition:
        """Combine all signals into a single MarketCondition."""

        c = MarketCondition()

        # Fear & Greed
        c.fear_greed = self.fear_greed.value
        c.fear_greed_bias = self.fear_greed.trade_direction_bias()
        fg_mult = self.fear_greed.position_bias()

        # Liquidations
        liq = self.liquidations.latest
        if liq:
            c.liquidation_24h = liq.total_24h
            c.mass_liquidation = liq.is_mass_liquidation
            c.liquidation_bias = self.liquidations.reversal_bias()
        liq_mult = self.liquidations.aggression_boost()

        # Macro calendar
        c.macro_event_imminent = self.macro.has_imminent_event()
        c.macro_exposure_mult = self.macro.exposure_multiplier()
        c.macro_spike_opportunity = self.macro.is_spike_opportunity()
        next_ev = self.macro.next_event_info()
        c.next_macro_event = next_ev or ""
        macro_mult = c.macro_exposure_mult

        # Whale sentiment
        c.whale_bias = self.whales.contrarian_bias("BTC")
        btc = self.whales.get("BTC")
        if btc:
            if btc.is_overleveraged_longs:
                c.overleveraged_side = "longs"
            elif btc.is_overleveraged_shorts:
                c.overleveraged_side = "shorts"

        # TradingView consensus (BTC and ETH as market leaders)
        c.tv_btc_consensus = self.tradingview.consensus("BTC/USDT")
        c.tv_eth_consensus = self.tradingview.consensus("ETH/USDT")

        # DeFiLlama TVL flows
        c.tvl_trend = self.defillama.tvl_trend
        defi_mult = self.defillama.position_bias()

        # Santiment social sentiment
        c.social_sentiment = self.santiment.sentiment_signal("BTC")
        c.social_spike = self.santiment.is_social_spike("BTC")
        santi_mult = self.santiment.position_bias()

        # Glassnode on-chain
        c.on_chain_bias = self.glassnode.on_chain_bias("BTC")
        c.distribution_phase = self.glassnode.is_distribution_phase()
        c.accumulation_phase = self.glassnode.is_accumulation_phase()
        glass_mult = self.glassnode.position_bias()

        # CoinGlass OI details
        btc_whale = self.whales.get("BTC")
        if btc_whale and btc_whale.oi_snapshot:
            c.btc_oi_total_usd = btc_whale.oi_snapshot.total_oi_usd
            c.btc_oi_change_1h_pct = btc_whale.oi_snapshot.oi_change_1h_pct
            c.top_trader_long_ratio = btc_whale.oi_snapshot.top_trader_long_ratio

        # -- Composite position size multiplier --
        raw_mult = fg_mult * liq_mult * macro_mult * defi_mult * santi_mult * glass_mult
        c.position_size_multiplier = min(raw_mult, 1.5)

        logger.debug(
            "Intel multipliers: F&G={:.2f} Liq={:.2f} Macro={:.2f} "
            "DeFi={:.2f} Santi={:.2f} Glass={:.2f} => raw={:.3f} capped={:.2f}",
            fg_mult,
            liq_mult,
            macro_mult,
            defi_mult,
            santi_mult,
            glass_mult,
            raw_mult,
            c.position_size_multiplier,
        )

        # -- Should reduce exposure --
        reduce_reasons = []
        if c.macro_event_imminent:
            reduce_reasons.append("macro_imminent")
        if self.fear_greed.is_extreme_greed:
            reduce_reasons.append(f"extreme_greed({c.fear_greed})")
        if btc is not None and btc.is_overleveraged_longs:
            reduce_reasons.append("overleveraged_longs")
        if c.distribution_phase:
            reduce_reasons.append("distribution_phase")
        c.should_reduce_exposure = len(reduce_reasons) > 0

        if reduce_reasons:
            logger.debug("Intel: REDUCE EXPOSURE — {}", ", ".join(reduce_reasons))

        # -- Preferred direction --
        votes = {"long": 0, "short": 0, "neutral": 0}
        votes[c.fear_greed_bias] += 2
        votes[c.liquidation_bias] += 2 if c.mass_liquidation else 1
        votes[c.whale_bias] += 1

        tv_dir = c.tv_btc_consensus
        if tv_dir in votes:
            votes[tv_dir] += 2

        if c.on_chain_bias in votes:
            votes[c.on_chain_bias] += 1

        logger.debug(
            "Intel direction votes: L={} S={} N={} | sources: fg={} liq={} whale={} tv={} chain={}",
            votes["long"],
            votes["short"],
            votes["neutral"],
            c.fear_greed_bias,
            c.liquidation_bias,
            c.whale_bias,
            tv_dir,
            c.on_chain_bias,
        )

        if votes["long"] > votes["short"] and votes["long"] > votes["neutral"]:
            c.preferred_direction = "long"
        elif votes["short"] > votes["long"] and votes["short"] > votes["neutral"]:
            c.preferred_direction = "short"
        else:
            c.preferred_direction = "neutral"

        # -- Market regime --
        if self.fear_greed.is_extreme_fear and c.mass_liquidation:
            c.regime = MarketRegime.CAPITULATION
        elif (self.fear_greed.is_extreme_greed and c.macro_event_imminent) or (
            c.overleveraged_side == "longs" and self.fear_greed.is_greed
        ):
            c.regime = MarketRegime.RISK_OFF
        elif c.should_reduce_exposure:
            c.regime = MarketRegime.CAUTION
        elif self.fear_greed.is_fear or c.mass_liquidation:
            c.regime = MarketRegime.RISK_ON
        else:
            c.regime = MarketRegime.NORMAL

        self._condition = c
        return c

    @property
    def condition(self) -> MarketCondition:
        return self._condition

    async def analyze_symbol(self, symbol: str) -> float | None:
        """Run TradingView analysis for a symbol.

        Returns signal_boost multiplier (0.7-1.3) or None if no data.
        """
        analysis = await self.tradingview.analyze(symbol, "1h")
        if not analysis:
            return None
        await self.tradingview.analyze(symbol, "4h")
        return None  # boost is fetched via tradingview.signal_boost() per signal

    def tv_signal_boost(self, symbol: str, side: str) -> float:
        """Get TradingView signal alignment boost for a proposed trade."""
        return self.tradingview.signal_boost(symbol, side)

    def get_discovery_symbols(self) -> list[str]:
        """Aggregate interesting symbols from CMC and CoinGecko for the scanner."""
        symbols: set[str] = set()

        for cmc_coin in self.coinmarketcap.all_interesting:
            if cmc_coin.is_tradable_size:
                symbols.add(cmc_coin.symbol.upper())

        for gecko_coin in self.coingecko.all_interesting:
            if gecko_coin.volume_24h >= 1_000_000:
                symbols.add(gecko_coin.symbol.upper())

        return sorted(symbols)

    def full_summary(self) -> str:
        lines = [
            "=== MARKET INTELLIGENCE ===",
            self.fear_greed.summary(),
            self.liquidations.summary(),
            self.macro.summary(),
            self.whales.summary(),
            self.tradingview.summary(),
            self.coinmarketcap.summary(),
            self.coingecko.summary(),
            self.defillama.summary(),
            self.santiment.summary(),
            self.glassnode.summary(),
            f"Regime: {self._condition.regime.value} | "
            f"Size: {self._condition.position_size_multiplier:.1f}x | "
            f"Direction: {self._condition.preferred_direction}",
        ]
        return "\n  ".join(lines)
