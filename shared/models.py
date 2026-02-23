from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class DeploymentLevel(str, Enum):
    """How busy the bot is -- controls monitoring intensity."""

    IDLE = "idle"  # disabled via hub config — running but not trading
    WINDING_DOWN = "winding_down"  # closing positions before going idle
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

    locked_until: str = ""

    source: str = ""
    target_bot: str = ""
    supported_exchanges: list[str] = []
    consumed: bool = False

    @property
    def is_locked(self) -> bool:
        if not self.locked_until:
            return False
        try:
            deadline = datetime.fromisoformat(self.locked_until.replace("Z", "+00:00"))
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=UTC)
            return datetime.now(UTC) < deadline
        except (ValueError, TypeError):
            return False

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
    """Flat priority queue of trade proposals.

    Single list sorted by priority (CRITICAL > DAILY > SWING) then age.
    Proposals are either available or locked (``locked_until`` set).
    On consume/reject the bot's exchange is removed; when no exchanges
    remain the proposal is deleted.
    """

    proposals: list[TradeProposal] = []
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    _PRIORITY_ORDER: dict[SignalPriority, int] = {
        SignalPriority.CRITICAL: 0,
        SignalPriority.DAILY: 1,
        SignalPriority.SWING: 2,
    }

    @property
    def total(self) -> int:
        return len(self.proposals)

    @property
    def pending_count(self) -> int:
        return sum(1 for p in self.proposals if not p.consumed and not p.is_locked and not p.is_expired)

    def add(self, proposal: TradeProposal) -> None:
        if any(p.id == proposal.id for p in self.proposals):
            return
        self.proposals.append(proposal)

    def has_symbol(self, symbol: str) -> bool:
        """True if the symbol has any pending or locked proposal."""
        return any(p.symbol == symbol and not p.is_expired for p in self.proposals)

    def get_actionable(self, priority: SignalPriority | None = None) -> list[TradeProposal]:
        """Return available (non-locked, non-expired) proposals, optionally filtered by priority."""
        return [
            p
            for p in self.proposals
            if not p.is_locked and not p.is_expired and (priority is None or p.priority == priority)
        ]

    def get_next_for_bot(
        self,
        exchange: str,
        bot_style: str = "",
        allowed_priorities: list[SignalPriority] | None = None,
        active_symbols: set[str] | None = None,
        open_db_symbols: set[str] | None = None,
    ) -> TradeProposal | None:
        """Return the highest-priority available proposal for this bot, or None."""
        ex_upper = exchange.upper()
        prio_order = [SignalPriority.CRITICAL, SignalPriority.DAILY, SignalPriority.SWING]

        locked_symbols = {p.symbol for p in self.proposals if p.is_locked}

        for prio in prio_order:
            if allowed_priorities and prio not in allowed_priorities:
                continue
            for p in self.proposals:
                if p.priority != prio:
                    continue
                if p.is_locked or p.is_expired:
                    continue
                if ex_upper not in p.supported_exchanges:
                    continue
                if p.symbol in locked_symbols:
                    continue
                if active_symbols and p.symbol in active_symbols:
                    continue
                if open_db_symbols and p.symbol in open_db_symbols:
                    continue
                targets = {t.strip() for t in (p.target_bot or "").split(",") if t.strip()}
                if targets and bot_style not in targets:
                    continue
                return p
        return None

    def lock_proposal(self, proposal_id: str, seconds: int = 60) -> None:
        deadline = datetime.now(UTC) + timedelta(seconds=seconds)
        for p in self.proposals:
            if p.id == proposal_id:
                p.locked_until = deadline.isoformat()
                return

    def unlock_expired(self) -> int:
        """Clear locks whose deadline has passed."""
        cleared = 0
        for p in self.proposals:
            if p.locked_until and not p.is_locked:
                p.locked_until = ""
                cleared += 1
        return cleared

    def remove_exchange(self, proposal_id: str, exchange: str) -> bool:
        """Remove *exchange* from a proposal's supported list.

        Clears the lock.  Returns True if the proposal was removed entirely
        (no supported exchanges left).
        """
        ex_upper = exchange.upper()
        for i, p in enumerate(self.proposals):
            if p.id == proposal_id:
                p.supported_exchanges = [e for e in p.supported_exchanges if e != ex_upper]
                p.locked_until = ""
                if not p.supported_exchanges:
                    self.proposals.pop(i)
                    return True
                return False
        return False

    def remove_proposal(self, proposal_id: str) -> TradeProposal | None:
        for i, p in enumerate(self.proposals):
            if p.id == proposal_id:
                return self.proposals.pop(i)
        return None

    def purge_stale(self, max_expired_age: int = 600) -> int:
        """Remove expired proposals older than *max_expired_age* seconds."""
        before = len(self.proposals)
        self.proposals = [p for p in self.proposals if not (p.is_expired and p.age_seconds > max_expired_age)]
        return before - len(self.proposals)


class BotDeploymentStatus(BaseModel):
    """Written by the bot every tick so the monitor knows how hard to work."""

    bot_id: str = ""
    bot_style: str = ""  # momentum / meanrev / swing
    exchange: str = ""  # exchange this bot trades on (e.g. MEXC, BINANCE)
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

    # News
    news_items: list[dict[str, Any]] = []

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


class BotDashboardSnapshot(BaseModel):
    """Written by each bot on every tick -- read by central dashboard."""

    bot_id: str = ""
    bot_style: str = ""
    exchange: str = ""
    status: dict[str, Any] = {}
    positions: list[dict[str, Any]] = []
    wick_scalps: list[dict[str, Any]] = []
    strategies: list[dict[str, Any]] = []
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class AnalyticsSnapshot(BaseModel):
    """Written by the analytics service -- strategy scores and suggestions."""

    weights: list[StrategyWeightEntry] = []
    patterns: list[dict[str, Any]] = []
    suggestions: list[dict[str, Any]] = []
    total_trades_logged: int = 0
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


# ---------------------------------------------------------------------------
#  Extreme mover watchlist (monitor -> bot via shared state)
# ---------------------------------------------------------------------------


class ExtremeCandidate(BaseModel):
    """A single extreme mover identified by the monitor."""

    symbol: str
    direction: str = "bull"  # "bull" or "bear"
    change_1h: float = 0.0
    change_5m: float = 0.0
    volume_24h: float = 0.0
    momentum_score: float = 0.0
    reason: str = ""
    supported_exchanges: list[str] = []  # exchanges where this symbol was found
    detected_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class ExtremeWatchlist(BaseModel):
    """Written by monitor, read by trading bots."""

    candidates: list[ExtremeCandidate] = []
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
