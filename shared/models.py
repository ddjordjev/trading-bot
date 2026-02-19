from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class DeploymentLevel(str, Enum):
    """How busy the bot is -- controls monitoring intensity."""

    HUNTING = "hunting"  # no/few positions, looking for entries → max monitoring
    ACTIVE = "active"  # some positions, still has capacity → normal monitoring
    DEPLOYED = "deployed"  # fully deployed, positions running well → low monitoring
    STRESSED = "stressed"  # positions losing, need exit/hedge intel → high monitoring


# ---------------------------------------------------------------------------
# Trade Priority Queue — monitor/intel proposes trades, bot consumes them
# ---------------------------------------------------------------------------


class SignalPriority(str, Enum):
    CRITICAL = "critical"  # act within seconds — spikes, liq cascades, wick scalps
    DAILY = "daily"  # valid hours — momentum entries, trending setups
    SWING = "swing"  # limit order plan — valid days, with full entry/exit plan


class EntryPlan(BaseModel):
    """Execution blueprint for SWING proposals.

    The bot uses this to place limit orders and manage the position
    lifecycle (DCA levels, leverage ramps, partial takes).
    """

    entry_price: float = 0.0
    entry_zone_low: float = 0.0
    entry_zone_high: float = 0.0
    stop_loss: float = 0.0
    take_profit_targets: list[float] = []
    dca_levels: list[float] = []
    initial_leverage: int = 1
    max_leverage: int = 10
    scale_in_pct: float = 2.0
    notes: str = ""


class TradeProposal(BaseModel):
    """A proposed trade produced by the monitor/intel layer."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    priority: SignalPriority
    symbol: str
    side: str = "long"
    strategy: str = ""
    reason: str = ""
    strength: float = 0.5
    market_type: str = "futures"
    leverage: int = 10
    quick_trade: bool = False
    max_hold_minutes: int = 0
    tick_urgency: str = "active"  # scalp | active | swing

    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    valid_until: str = ""
    max_age_seconds: int = 0

    entry_plan: EntryPlan | None = None

    consumed: bool = False
    consumed_at: str = ""
    rejected: bool = False
    reject_reason: str = ""

    source: str = ""

    @property
    def is_expired(self) -> bool:
        now = datetime.now(UTC)
        if self.valid_until:
            try:
                deadline = datetime.fromisoformat(self.valid_until.replace("Z", "+00:00"))
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=UTC)
                if now > deadline:
                    return True
            except (ValueError, TypeError):
                pass
        if self.max_age_seconds > 0:
            try:
                created = datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
                if (now - created).total_seconds() > self.max_age_seconds:
                    return True
            except (ValueError, TypeError):
                pass
        return False

    @property
    def age_seconds(self) -> float:
        try:
            created = datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            return (datetime.now(UTC) - created).total_seconds()
        except (ValueError, TypeError):
            return 0.0


class TradeQueue(BaseModel):
    """Priority queue of trade proposals from monitor/intel to the bot.

    Written by the monitor service to data/trade_queue.json.
    Read (and consumed) by the bot each tick.
    """

    critical: list[TradeProposal] = []
    daily: list[TradeProposal] = []
    swing: list[TradeProposal] = []
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    @property
    def total(self) -> int:
        return len(self.critical) + len(self.daily) + len(self.swing)

    @property
    def pending_count(self) -> int:
        return sum(
            1 for p in self.critical + self.daily + self.swing if not p.consumed and not p.rejected and not p.is_expired
        )

    def add(self, proposal: TradeProposal) -> None:
        bucket = self._bucket(proposal.priority)
        if any(p.id == proposal.id for p in bucket):
            return
        bucket.append(proposal)

    def get_actionable(self, priority: SignalPriority) -> list[TradeProposal]:
        return [p for p in self._bucket(priority) if not p.consumed and not p.rejected and not p.is_expired]

    def mark_consumed(self, proposal_id: str) -> None:
        for p in self.critical + self.daily + self.swing:
            if p.id == proposal_id:
                p.consumed = True
                p.consumed_at = datetime.now(UTC).isoformat()
                return

    def mark_rejected(self, proposal_id: str, reason: str = "") -> None:
        for p in self.critical + self.daily + self.swing:
            if p.id == proposal_id:
                p.rejected = True
                p.reject_reason = reason
                return

    def purge_stale(self, max_consumed_age: int = 3600, max_expired_age: int = 600) -> int:
        """Remove consumed/expired proposals older than thresholds."""
        removed = 0
        _now = datetime.now(UTC)
        for attr in ("critical", "daily", "swing"):
            bucket: list[TradeProposal] = getattr(self, attr)
            keep = []
            for p in bucket:
                age = p.age_seconds
                if (p.consumed and age > max_consumed_age) or (p.is_expired and age > max_expired_age):
                    removed += 1
                else:
                    keep.append(p)
            setattr(self, attr, keep)
        return removed

    def _bucket(self, priority: SignalPriority) -> list[TradeProposal]:
        result: list[TradeProposal] = getattr(self, priority.value)
        return result


class BotDeploymentStatus(BaseModel):
    """Written by the bot every tick so the monitor knows how hard to work."""

    level: DeploymentLevel = DeploymentLevel.HUNTING
    open_positions: int = 0
    max_positions: int = 3
    capacity_pct: float = 100.0
    daily_pnl_pct: float = 0.0
    daily_tier: str = "building"
    avg_position_health: float = 0.0  # avg unrealized PnL %
    worst_position_pnl: float = 0.0
    should_trade: bool = True
    manual_stop: bool = False
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

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
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    source_timestamps: dict[str, str] = {}
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
    patterns: list[dict[str, Any]] = []
    suggestions: list[dict[str, Any]] = []
    total_trades_logged: int = 0
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
