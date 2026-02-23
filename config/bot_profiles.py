"""Bot profile definitions for dynamic container management.

Each profile maps to a Docker container configuration. The hub
(bot-hub) can spin up / tear down profiles at runtime via the
Docker socket. The hub itself is infrastructure-only (no trading).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class BotProfile:
    id: str
    display_name: str
    description: str
    style: str  # momentum / meanrev / swing
    strategies: list[str] = field(default_factory=list)
    allowed_priorities: list[str] = field(default_factory=lambda: ["critical", "daily"])
    env_overrides: dict[str, str] = field(default_factory=dict)
    is_default: bool = False
    is_hub: bool = False


ALL_PROFILES: list[BotProfile] = [
    BotProfile(
        id="hub",
        display_name="Hub",
        description="Dashboard and coordination — no trading",
        style="momentum",
        strategies=[],
        is_default=True,
        is_hub=True,
    ),
    BotProfile(
        id="extreme",
        display_name="Extreme Mover",
        description="Hunts extreme price movers with high leverage and aggressive entries",
        style="momentum",
        strategies=["compound_momentum", "market_open_volatility"],
        allowed_priorities=["critical"],
        env_overrides={
            "DEFAULT_LEVERAGE": "20",
            "MAX_CONCURRENT_POSITIONS": "10",
            "TICK_INTERVAL_IDLE": "30",
            "TICK_INTERVAL_ACTIVE": "30",
            "EXTREME_ENABLED": "true",
            "EXTREME_MAX_POSITIONS": "10",
            "EXTREME_POSITION_SIZE_PCT": "10.0",
            "EXTREME_MIN_HOURLY_MOVE_PCT": "3.0",
            "EXTREME_TRAIL_PCT": "0.3",
            "EXTREME_LOSER_TIMEOUT_MINUTES": "2",
            "EXTREME_EVAL_INTERVAL": "15",
            "INITIAL_RISK_AMOUNT": "100",
            "TAKE_PROFIT_PCT": "8",
        },
        is_default=True,
    ),
    BotProfile(
        id="hedger",
        display_name="Hedge Heavy",
        description="Momentum + Mean reversion with aggressive hedging ratios",
        style="momentum",
        strategies=["compound_momentum", "mean_reversion"],
        env_overrides={
            "HEDGE_ENABLED": "true",
            "HEDGE_RATIO": "0.40",
            "HEDGE_MIN_PROFIT_PCT": "2.0",
            "MAX_HEDGES": "4",
        },
        is_default=True,
    ),
    BotProfile(
        id="momentum",
        display_name="Momentum",
        description="Trend-following with compounding momentum and market-open volatility",
        style="momentum",
        strategies=["compound_momentum", "market_open_volatility"],
        is_default=True,
    ),
    BotProfile(
        id="indicators",
        display_name="Technical Indicators",
        description="Classic RSI and MACD crossover signals",
        style="momentum",
        strategies=["rsi", "macd"],
        is_default=True,
    ),
    BotProfile(
        id="meanrev",
        display_name="Mean Reversion",
        description="Bollinger Band breakouts and mean reversion on extended moves",
        style="meanrev",
        strategies=["bollinger", "mean_reversion"],
        env_overrides={
            "TICK_INTERVAL_IDLE": "120",
            "TICK_INTERVAL_ACTIVE": "60",
        },
        is_default=False,
    ),
    BotProfile(
        id="swing",
        display_name="Swing / Grid",
        description="Multi-day swing trades and grid trading with fixed intervals",
        style="swing",
        strategies=["swing_opportunity", "grid"],
        allowed_priorities=["daily", "swing"],
        env_overrides={
            "TICK_INTERVAL_IDLE": "600",
            "TICK_INTERVAL_ACTIVE": "300",
        },
        is_default=False,
    ),
    BotProfile(
        id="scalper",
        display_name="Scalper",
        description="Quick in-and-out scalps with tight stops and fast tick intervals",
        style="momentum",
        strategies=["compound_momentum"],
        allowed_priorities=["critical"],
        env_overrides={
            "TICK_INTERVAL_ACTIVE": "15",
            "STOP_LOSS_PCT": "0.8",
            "TAKE_PROFIT_PCT": "2.0",
            "INITIAL_RISK_AMOUNT": "30",
        },
        is_default=False,
    ),
    BotProfile(
        id="fullstack",
        display_name="Full Stack",
        description="All 8 strategies on all major symbols for maximum coverage",
        style="momentum",
        strategies=[
            "compound_momentum",
            "market_open_volatility",
            "swing_opportunity",
            "rsi",
            "macd",
            "bollinger",
            "mean_reversion",
            "grid",
        ],
        allowed_priorities=["critical", "daily", "swing"],
        env_overrides={
            "MAX_CONCURRENT_POSITIONS": "10",
        },
        is_default=True,
    ),
    BotProfile(
        id="conservative",
        display_name="Conservative",
        description="Low leverage with RSI + Bollinger and tight risk limits",
        style="meanrev",
        strategies=["rsi", "bollinger"],
        env_overrides={
            "DEFAULT_LEVERAGE": "3",
            "MAX_POSITION_SIZE_PCT": "3",
            "STOP_LOSS_PCT": "1.0",
            "INITIAL_RISK_AMOUNT": "20",
        },
        is_default=False,
    ),
    BotProfile(
        id="aggressive",
        display_name="Aggressive",
        description="High leverage momentum + RSI for maximum upside",
        style="momentum",
        strategies=["compound_momentum", "rsi"],
        env_overrides={
            "DEFAULT_LEVERAGE": "20",
            "MAX_POSITION_SIZE_PCT": "10",
            "TAKE_PROFIT_PCT": "10",
            "INITIAL_RISK_AMOUNT": "100",
            "MAX_CONCURRENT_POSITIONS": "8",
        },
        is_default=False,
    ),
]

PROFILES_BY_ID: dict[str, BotProfile] = {p.id: p for p in ALL_PROFILES}
