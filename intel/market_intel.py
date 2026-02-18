from __future__ import annotations

from enum import Enum
from typing import Optional

from loguru import logger
from pydantic import BaseModel

from intel.fear_greed import FearGreedClient
from intel.liquidations import LiquidationMonitor
from intel.macro_calendar import MacroCalendar
from intel.whale_sentiment import WhaleSentiment


class MarketRegime(str, Enum):
    RISK_ON = "risk_on"       # fear + liquidations clearing + good conditions
    NORMAL = "normal"         # nothing extreme
    CAUTION = "caution"       # greed or pre-event or overleveraged
    RISK_OFF = "risk_off"     # extreme greed + FOMC imminent + overleveraged
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

    # Composite multipliers for the bot
    position_size_multiplier: float = 1.0  # applied to all new entries
    should_reduce_exposure: bool = False
    preferred_direction: str = "neutral"  # "long", "short", "neutral"

    def summary_lines(self) -> list[str]:
        lines = [
            f"Regime: {self.regime.value}",
            f"F&G: {self.fear_greed} ({self.fear_greed_bias})",
            f"Liq: ${self.liquidation_24h/1e6:.0f}M {'** MASS LIQ **' if self.mass_liquidation else ''}",
            f"Macro: {'IMMINENT' if self.macro_event_imminent else 'clear'} "
            f"(expo={self.macro_exposure_mult:.0%})"
            f"{' | ' + self.next_macro_event if self.next_macro_event else ''}",
            f"Whale: {self.whale_bias}"
            f"{' | overleveraged ' + self.overleveraged_side if self.overleveraged_side else ''}",
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

    The bot reads MarketCondition each tick to adjust:
    - Position sizing (multiplier)
    - Trade direction (long/short/neutral bias)
    - Whether to reduce exposure (tighten stops, skip entries)
    - Whether it's a spike opportunity (macro event volatility)
    """

    def __init__(self, coinglass_key: str = "",
                 symbols: list[str] = None):
        self.fear_greed = FearGreedClient(poll_interval=3600)
        self.liquidations = LiquidationMonitor(poll_interval=300, api_key=coinglass_key)
        self.macro = MacroCalendar(poll_interval=1800)
        self.whales = WhaleSentiment(
            symbols=symbols or ["BTC", "ETH"],
            poll_interval=300,
            coinglass_key=coinglass_key,
        )
        self._condition = MarketCondition()

    async def start(self) -> None:
        await self.fear_greed.start()
        await self.liquidations.start()
        await self.macro.start()
        await self.whales.start()
        logger.info("MarketIntel started -- all external feeds active")

    async def stop(self) -> None:
        await self.fear_greed.stop()
        await self.liquidations.stop()
        await self.macro.stop()
        await self.whales.stop()

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

        # -- Composite position size multiplier --
        c.position_size_multiplier = min(
            fg_mult * liq_mult * macro_mult,
            1.5,  # cap at 1.5x even if everything is screaming "buy"
        )

        # -- Should reduce exposure --
        c.should_reduce_exposure = (
            c.macro_event_imminent or
            self.fear_greed.is_extreme_greed or
            (btc is not None and btc.is_overleveraged_longs)
        )

        # -- Preferred direction --
        votes = {"long": 0, "short": 0, "neutral": 0}
        votes[c.fear_greed_bias] += 2
        votes[c.liquidation_bias] += 2 if c.mass_liquidation else 1
        votes[c.whale_bias] += 1

        if votes["long"] > votes["short"] and votes["long"] > votes["neutral"]:
            c.preferred_direction = "long"
        elif votes["short"] > votes["long"] and votes["short"] > votes["neutral"]:
            c.preferred_direction = "short"
        else:
            c.preferred_direction = "neutral"

        # -- Market regime --
        if self.fear_greed.is_extreme_fear and c.mass_liquidation:
            c.regime = MarketRegime.CAPITULATION
        elif (self.fear_greed.is_extreme_greed and c.macro_event_imminent) or \
             (c.overleveraged_side == "longs" and self.fear_greed.is_greed):
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

    def full_summary(self) -> str:
        lines = [
            "=== MARKET INTELLIGENCE ===",
            self.fear_greed.summary(),
            self.liquidations.summary(),
            self.macro.summary(),
            self.whales.summary(),
            f"Regime: {self._condition.regime.value} | "
            f"Size: {self._condition.position_size_multiplier:.1f}x | "
            f"Direction: {self._condition.preferred_direction}",
        ]
        return "\n  ".join(lines)
