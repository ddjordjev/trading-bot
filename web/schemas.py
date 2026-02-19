from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class BotStatus(BaseModel):
    running: bool = False
    trading_mode: str = "paper"
    exchange_name: str = ""
    exchange_url: str = ""
    balance: float = 0.0
    daily_pnl: float = 0.0
    daily_pnl_pct: float = 0.0
    tier: str = "building"
    tier_progress_pct: float = 0.0
    daily_target_pct: float = 10.0
    total_growth_pct: float = 0.0
    uptime_seconds: float = 0.0
    manual_stop_active: bool = False
    strategies_count: int = 0
    dynamic_strategies_count: int = 0


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
    stop_loss: Optional[float] = None
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


class IntelSnapshot(BaseModel):
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
    position_size_multiplier: float = 1.0
    should_reduce_exposure: bool = False
    preferred_direction: str = "neutral"


class TrendingCoinInfo(BaseModel):
    symbol: str
    name: str = ""
    price: float = 0.0
    volume_24h: float = 0.0
    market_cap: float = 0.0
    change_1h: float = 0.0
    change_24h: float = 0.0
    is_low_liquidity: bool = False
    has_dynamic_strategy: bool = False


class StrategyInfo(BaseModel):
    name: str
    symbol: str
    market_type: str
    leverage: int
    mode: str = "pyramid"
    is_dynamic: bool = False


class ModuleStatus(BaseModel):
    name: str
    enabled: bool
    display_name: str
    description: str = ""
    stats: dict = {}


class DailyReportData(BaseModel):
    compound_report: str = ""
    history: list[dict] = []
    winning_days: int = 0
    losing_days: int = 0
    target_hit_days: int = 0
    avg_daily_pnl_pct: float = 0.0
    best_day: Optional[dict] = None
    worst_day: Optional[dict] = None
    projected: dict = {}


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
    intel: Optional[IntelSnapshot] = None
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


class AnalyticsSnapshot(BaseModel):
    strategy_scores: list[StrategyScoreInfo] = []
    patterns: list[PatternInsightInfo] = []
    suggestions: list[ModificationSuggestionInfo] = []
    total_trades_logged: int = 0
    hourly_performance: list[dict] = []
    regime_performance: list[dict] = []


class ActionResponse(BaseModel):
    success: bool
    message: str
