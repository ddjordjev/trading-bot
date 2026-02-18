from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class DeploymentLevel(str, Enum):
    """How busy the bot is -- controls monitoring intensity."""

    HUNTING = "hunting"      # no/few positions, looking for entries → max monitoring
    ACTIVE = "active"        # some positions, still has capacity → normal monitoring
    DEPLOYED = "deployed"    # fully deployed, positions running well → low monitoring
    STRESSED = "stressed"    # positions losing, need exit/hedge intel → high monitoring


class BotDeploymentStatus(BaseModel):
    """Written by the bot every tick so the monitor knows how hard to work."""

    level: DeploymentLevel = DeploymentLevel.HUNTING
    open_positions: int = 0
    max_positions: int = 3
    capacity_pct: float = 100.0
    daily_pnl_pct: float = 0.0
    daily_tier: str = "building"
    avg_position_health: float = 0.0   # avg unrealized PnL %
    worst_position_pnl: float = 0.0
    should_trade: bool = True
    manual_stop: bool = False
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def has_capacity(self) -> bool:
        return self.open_positions < self.max_positions

    @property
    def is_idle(self) -> bool:
        return self.open_positions == 0


class TVSymbolSnapshot(BaseModel):
    """Cached TradingView analysis for one symbol."""

    symbol: str
    interval: str = "1h"
    rating: str = "NEUTRAL"
    oscillators: str = "NEUTRAL"
    moving_averages: str = "NEUTRAL"
    confidence: float = 0.0
    rsi_14: float = 0.0
    consensus: str = "neutral"
    signal_boost_long: float = 1.0
    signal_boost_short: float = 1.0
    updated_at: str = ""


class TrendingSnapshot(BaseModel):
    """A single trending coin from the scanner."""

    symbol: str
    name: str = ""
    price: float = 0.0
    market_cap: float = 0.0
    volume_24h: float = 0.0
    change_1h: float = 0.0
    change_24h: float = 0.0
    change_7d: float = 0.0
    momentum_score: float = 0.0
    is_low_liquidity: bool = False
    source: str = ""


class IntelSnapshot(BaseModel):
    """Written by the monitor -- everything the bot needs to know about the market."""

    # Market regime and conditions
    regime: str = "normal"
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
    tv_btc_consensus: str = "neutral"
    tv_eth_consensus: str = "neutral"

    # Composite
    position_size_multiplier: float = 1.0
    should_reduce_exposure: bool = False
    preferred_direction: str = "neutral"

    # TradingView cache
    tv_analyses: list[TVSymbolSnapshot] = []

    # Trending / discovery
    hot_movers: list[TrendingSnapshot] = []
    cmc_trending: list[TrendingSnapshot] = []
    coingecko_trending: list[TrendingSnapshot] = []

    # Monitoring metadata
    monitor_intensity: str = "normal"
    poll_multiplier: float = 1.0
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    sources_active: list[str] = []


class StrategyWeightEntry(BaseModel):
    strategy: str
    weight: float = 1.0
    win_rate: float = 0.0
    total_trades: int = 0
    total_pnl: float = 0.0
    streak: int = 0


class AnalyticsSnapshot(BaseModel):
    """Written by the analytics service -- strategy scores and suggestions."""

    weights: list[StrategyWeightEntry] = []
    patterns: list[dict] = []
    suggestions: list[dict] = []
    total_trades_logged: int = 0
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
