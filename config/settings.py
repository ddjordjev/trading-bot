from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    trading_mode: Literal["paper", "live"] = "paper"

    exchange: str = "mexc"
    mexc_api_key: str = ""
    mexc_api_secret: str = ""

    default_leverage: int = 10

    # Risk -- capital preservation first
    max_position_size_pct: float = 5.0
    max_daily_loss_pct: float = 3.0
    stop_loss_pct: float = 1.5
    take_profit_pct: float = 5.0
    max_concurrent_positions: int = 3
    min_signal_strength: float = 0.4  # ignore weak signals entirely
    consecutive_loss_cooldown: int = 3  # pause after N consecutive losses

    # Liquidity-aware scaling
    breakeven_lock_pct: float = 5.0  # move stop to entry once at this profit %
    initial_risk_amount: float = 50.0  # fixed $ amount for the first entry
    max_notional_position: float = 100_000.0  # stop adding once leveraged position hits this
    min_profit_to_add_pct: float = 1.0  # must be +1% before adding to position
    gambling_budget_pct: float = 2.0  # max % of balance for low-liq yolo bets
    min_liquidity_volume: float = 1_000_000  # 24h volume below this = "low liquidity"

    # Pyramid / DCA mode (DEFAULT for all strategies)
    default_scale_mode: str = "pyramid"  # "pyramid" (DCA in) or "winners" (add to winners)
    dca_interval_pct: float = 2.0    # add every 2% the price drops
    dca_multiplier: float = 1.5      # each DCA add is 1.5x the previous
    dca_profit_to_lever_pct: float = 1.0  # raise leverage once avg entry is +1%
    dca_partial_take_pct: float = 30.0  # take 30% off the table after lever-up

    # Hedging
    hedge_enabled: bool = True
    hedge_ratio: float = 0.20           # hedge is 20% of main position size
    hedge_min_profit_pct: float = 3.0   # main must be +3% before hedging
    hedge_stop_pct: float = 1.0         # tight stop on hedge (it's a probe)
    max_hedges: int = 2                 # max simultaneous hedges

    # Email
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    notify_email: str = ""
    notifications_enabled: str = "liquidation,stop_loss,spike_detected,daily_summary"

    # Volatility
    spike_threshold_pct: float = 3.0
    volatility_lookback_minutes: int = 5

    # News
    news_enabled: bool = False
    news_sources: str = "coindesk,cointelegraph,cryptopanic"

    # Market open windows (UTC)
    us_market_open_utc: int = 14
    us_market_close_utc: int = 21
    asia_market_open_utc: int = 1
    asia_market_close_utc: int = 8

    # Market Intelligence
    coinglass_api_key: str = ""            # optional, for CoinGlass premium endpoints
    intel_enabled: bool = True             # master switch for all external feeds
    fear_greed_poll: int = 3600            # how often to poll Fear & Greed (seconds)
    liquidation_poll: int = 300            # CoinGlass liquidation poll interval
    macro_calendar_poll: int = 1800        # ForexFactory calendar poll interval
    whale_sentiment_poll: int = 300        # CoinGlass funding/OI/L-S poll interval
    intel_symbols: str = "BTC,ETH"         # symbols to track for whale sentiment
    mass_liquidation_threshold: float = 1_000_000_000  # $1B = mass liq event

    log_level: str = "INFO"

    @property
    def notification_list(self) -> list[str]:
        return [n.strip() for n in self.notifications_enabled.split(",") if n.strip()]

    @property
    def intel_symbol_list(self) -> list[str]:
        return [s.strip().upper() for s in self.intel_symbols.split(",") if s.strip()]

    @property
    def news_source_list(self) -> list[str]:
        return [s.strip() for s in self.news_sources.split(",") if s.strip()]

    def is_paper(self) -> bool:
        return self.trading_mode == "paper"


@lru_cache
def get_settings() -> Settings:
    return Settings()
