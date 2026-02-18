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

    log_level: str = "INFO"

    @property
    def notification_list(self) -> list[str]:
        return [n.strip() for n in self.notifications_enabled.split(",") if n.strip()]

    @property
    def news_source_list(self) -> list[str]:
        return [s.strip() for s in self.news_sources.split(",") if s.strip()]

    def is_paper(self) -> bool:
        return self.trading_mode == "paper"


@lru_cache
def get_settings() -> Settings:
    return Settings()
