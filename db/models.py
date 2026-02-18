from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class TradeRecord(BaseModel):
    """A single completed trade with full context for pattern analysis."""

    id: int = 0
    symbol: str
    side: str
    strategy: str
    action: str  # buy/sell/close
    scale_mode: str = ""  # pyramid / winners
    entry_price: float = 0.0
    exit_price: float = 0.0
    amount: float = 0.0
    leverage: int = 1
    pnl_usd: float = 0.0
    pnl_pct: float = 0.0
    is_winner: bool = False
    hold_minutes: float = 0.0
    was_quick_trade: bool = False
    was_low_liquidity: bool = False
    dca_count: int = 0
    max_drawdown_pct: float = 0.0

    # Market context at entry
    market_regime: str = ""  # risk_on, normal, caution, risk_off, capitulation
    fear_greed: int = 50
    daily_tier: str = ""  # losing, building, strong, excellent, monster, legendary
    daily_pnl_at_entry: float = 0.0
    signal_strength: float = 0.0
    hour_utc: int = 0
    day_of_week: int = 0  # 0=Monday
    volatility_pct: float = 0.0

    opened_at: str = ""
    closed_at: str = ""


class StrategyScore(BaseModel):
    """Performance score for a strategy, used as a weight factor."""

    strategy: str
    symbol: str = ""  # empty = aggregate across all symbols
    total_trades: int = 0
    winners: int = 0
    losers: int = 0
    win_rate: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    total_pnl: float = 0.0
    profit_factor: float = 0.0  # gross_profit / gross_loss
    expectancy: float = 0.0  # avg $ per trade
    weight: float = 1.0  # composite weight factor (0.0 = disable, 2.0 = double allocation)
    streak_current: int = 0  # positive = wins, negative = losses
    streak_max_loss: int = 0
    avg_hold_minutes: float = 0.0
    best_hour_utc: int = -1
    worst_hour_utc: int = -1
    best_regime: str = ""
    worst_regime: str = ""
    last_updated: str = ""


class PatternInsight(BaseModel):
    """A detected pattern in losing (or winning) trades."""

    pattern_type: str  # time_of_day, market_regime, strategy_symbol, streak, volatility, etc.
    description: str
    severity: str = "info"  # info, warning, critical
    affected_strategy: str = ""
    affected_symbol: str = ""
    sample_size: int = 0
    confidence: float = 0.0  # 0-1
    suggestion: str = ""
    data: dict = {}


class ModificationSuggestion(BaseModel):
    """Concrete suggestion to modify a strategy or parameter."""

    strategy: str
    symbol: str = ""
    suggestion_type: str  # disable, reduce_weight, change_param, time_filter, regime_filter
    title: str
    description: str
    confidence: float = 0.0
    current_value: str = ""
    suggested_value: str = ""
    expected_improvement: str = ""
    based_on_trades: int = 0
