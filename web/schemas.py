from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class BotInstance(BaseModel):
    bot_id: str
    label: str
    port: int
    exchange: str = ""
    strategies: list[str] = []


class BotStatus(BaseModel):
    bot_id: str = ""
    running: bool = False
    trading_mode: str = "paper_local"
    exchange_name: str = ""
    exchange_url: str = ""
    balance: float = 0.0
    available_margin: float = 0.0
    daily_pnl: float = 0.0
    daily_pnl_pct: float = 0.0
    tier: str = "building"
    tier_progress_pct: float = 0.0
    daily_target_pct: float = 10.0
    total_growth_pct: float = 0.0
    total_growth_usd: float = 0.0
    uptime_seconds: float = 0.0
    manual_stop_active: bool = False
    strategies_count: int = 0
    dynamic_strategies_count: int = 0
    profit_buffer_pct: float = 0.0


class PositionInfo(BaseModel):
    symbol: str
    side: str
    amount: float
    entry_price: float
    current_price: float
    pnl_pct: float
    pnl_usd: float
    leverage: int = 1
    market_type: str = "spot"
    strategy: str = ""
    stop_loss: float | None = None
    take_profit: float | None = None
    exchange_stop_loss: float | None = None
    exchange_take_profit: float | None = None
    bot_stop_loss: float | None = None
    bot_take_profit: float | None = None
    effective_stop_loss: float | None = None
    effective_take_profit: float | None = None
    stop_source: str = "none"
    tp_source: str = "none"
    risk_state: str = "none"
    close_pending: bool = False
    close_reason_pending: str = ""
    notional_value: float = 0.0
    age_minutes: float = 0.0
    breakeven_locked: bool = False
    scale_mode: str = ""
    scale_phase: str = ""
    dca_count: int = 0
    trade_url: str = ""


class TradeRecord(BaseModel):
    timestamp: str
    symbol: str
    side: str
    action: str
    amount: float
    price: float
    strategy: str
    pnl: float = 0.0


class TradeQueueItem(BaseModel):
    """Active trade proposal in the queue."""

    symbol: str
    side: str
    strategy: str
    strength: float
    age_seconds: float
    reason: str = ""
    supported_exchanges: list[str] = []


class MacroEventInfo(BaseModel):
    title: str
    impact: str
    hours_until: float
    date_iso: str = ""


class IntelSnapshot(BaseModel):
    regime: str = "normal"
    fear_greed: int = 50
    fear_greed_bias: str = "neutral"
    liquidation_24h: float = 0.0
    liquidation_24h_text: str = ""
    mass_liquidation: bool = False
    liquidation_bias: str = "neutral"
    macro_event_imminent: bool = False
    macro_exposure_mult: float = 1.0
    macro_spike_opportunity: bool = False
    next_macro_event: str = ""
    macro_events: list[MacroEventInfo] = []
    whale_bias: str = "neutral"
    overleveraged_side: str = ""
    position_size_multiplier: float = 1.0
    should_reduce_exposure: bool = False
    preferred_direction: str = "neutral"
    openclaw_regime: str = "unknown"
    openclaw_regime_confidence: float = 0.0
    openclaw_regime_why: list[str] = []
    openclaw_sentiment_score: int = 50
    openclaw_long_short_ratio: float = 0.0
    openclaw_liquidations_24h_usd: float = 0.0
    openclaw_open_interest_24h_usd: float = 0.0
    openclaw_idea_briefs: list[dict[str, Any]] = []
    openclaw_failure_triage: list[dict[str, Any]] = []
    openclaw_experiments: list[dict[str, Any]] = []
    source_timestamps: dict[str, str] = {}
    sources_active: list[str] = []


class TrendingCoinInfo(BaseModel):
    symbol: str
    name: str = ""
    price: float = 0.0
    volume_24h: float = 0.0
    market_cap: float = 0.0
    change_5m: float = 0.0
    change_1h: float = 0.0
    change_24h: float = 0.0
    is_low_liquidity: bool = False
    has_dynamic_strategy: bool = False
    source: str = ""


class StrategyInfo(BaseModel):
    name: str
    symbol: str
    market_type: str
    leverage: int
    mode: str = "pyramid"
    is_dynamic: bool = False
    open_now: int = 0
    applied_count: int = 0
    success_count: int = 0
    fail_count: int = 0
    bot_id: str = ""


class ModuleStatus(BaseModel):
    name: str
    enabled: bool
    display_name: str
    description: str = ""
    stats: dict[str, Any] = {}


class DailyReportData(BaseModel):
    compound_report: str = ""
    history: list[dict[str, Any]] = []
    winning_days: int = 0
    losing_days: int = 0
    target_hit_days: int = 0
    avg_daily_pnl_pct: float = 0.0
    best_day: dict[str, Any] | None = None
    worst_day: dict[str, Any] | None = None
    projected: dict[str, Any] = {}


class WickScalpInfo(BaseModel):
    symbol: str
    scalp_side: str
    entry_price: float
    amount: float
    age_minutes: float
    max_hold_minutes: int = 5


class LogEntry(BaseModel):
    ts: str = ""
    level: str = "INFO"
    msg: str = ""
    module: str = ""


class FullSnapshot(BaseModel):
    status: BotStatus
    positions: list[PositionInfo] = []
    intel: IntelSnapshot | None = None
    wick_scalps: list[WickScalpInfo] = []
    logs: list[LogEntry] = []


class StrategyScoreInfo(BaseModel):
    strategy: str
    total_trades: int = 0
    winners: int = 0
    losers: int = 0
    win_rate: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    total_pnl: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    weight: float = 1.0
    streak_current: int = 0
    streak_max_loss: int = 0
    avg_hold_minutes: float = 0.0
    best_hour_utc: int = -1
    worst_hour_utc: int = -1
    best_regime: str = ""
    worst_regime: str = ""


class PatternInsightInfo(BaseModel):
    pattern_type: str
    description: str
    severity: str = "info"
    affected_strategy: str = ""
    affected_symbol: str = ""
    sample_size: int = 0
    confidence: float = 0.0
    suggestion: str = ""


class ModificationSuggestionInfo(BaseModel):
    id: int = 0
    source: str = "analytics"
    status: str = "new"
    strategy: str
    symbol: str = ""
    suggestion_type: str
    title: str
    description: str
    confidence: float = 0.0
    current_value: str = ""
    suggested_value: str = ""
    expected_improvement: str = ""
    based_on_trades: int = 0
    notes: str = ""
    updated_at: str = ""


class SuggestionStatusBody(BaseModel):
    status: str
    notes: str = ""


class LivePositionInfo(BaseModel):
    symbol: str
    side: str
    strategy: str
    entry_price: float
    current_price: float
    pnl_pct: float
    pnl_usd: float
    notional: float
    leverage: int
    age_minutes: float
    dca_count: int


class AnalyticsSnapshot(BaseModel):
    strategy_scores: list[StrategyScoreInfo] = []
    patterns: list[PatternInsightInfo] = []
    suggestions: list[ModificationSuggestionInfo] = []
    total_trades_logged: int = 0
    hourly_performance: list[dict[str, Any]] = []
    regime_performance: list[dict[str, Any]] = []
    live_positions: list[LivePositionInfo] = []


class NewsItemInfo(BaseModel):
    headline: str
    source: str
    url: str = ""
    published: str = ""
    matched_symbols: list[str] = []
    sentiment: str = "neutral"
    sentiment_score: float = 0.0


class ActionResponse(BaseModel):
    success: bool
    message: str


class PositionCloseBody(BaseModel):
    symbol: str
    bot_id: str = ""


class PositionClaimBody(BaseModel):
    symbol: str
    bot_id: str
    strategy: str = "manual_claim"


class PositionTakeProfitBody(BaseModel):
    symbol: str
    pct: float = 50.0
    bot_id: str = ""


class PositionTightenStopBody(BaseModel):
    symbol: str
    pct: float = 2.0
    bot_id: str = ""


class BotActionBody(BaseModel):
    bot_id: str = ""


class BotProfileInfo(BaseModel):
    id: str
    display_name: str
    description: str
    style: str
    strategies: list[str] = []
    env_overrides: dict[str, str] = {}
    is_hub: bool = False
    enabled: bool = False
    container_status: str = "idle"  # "running" | "idle" | "winding_down"
    balance: float | None = None
    daily_pnl: float | None = None
    wins: int = 0
    losses: int = 0
    open_positions: int = 0
