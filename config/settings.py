from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    trading_mode: str = "live"
    # Execution behavior comes from EXCHANGE target; this label is metadata.

    # Multi-bot: each instance gets a unique ID and strategy filter.
    # When BOT_ID is set, state goes to data/{bot_id}/ instead of data/.
    # BOT_STRATEGIES is a comma-separated list; empty = all strategies.
    bot_id: str = ""
    bot_strategies: str = ""
    bot_style: str = "momentum"  # momentum / meanrev / swing — determines queue routing
    hub_only: bool = False  # True = dashboard/coordination only, no trading

    exchange: str = "binance_testnet"

    # Exchange credentials.
    # Keep one variable name across environments; env override files provide
    # mode-specific values (paper/testnet vs live/prod).
    binance_api_key: str = ""
    binance_api_secret: str = ""
    bybit_api_key: str = ""
    bybit_api_secret: str = ""

    # What market types the user wants to use. The factory auto-restricts
    # to what the exchange actually supports.
    allowed_market_types: str = "spot,futures"

    # 0 = no cap (use full exchange balance)
    session_budget: float = 100.0

    default_leverage: int = 10
    # Cross mode is intentionally disabled for now; all futures trades use isolated.
    futures_margin_mode: Literal["isolated"] = "isolated"
    # Adaptive leverage envelope for extreme signals on small accounts.
    # account_cap = max(extreme_leverage_min_cap, floor(balance / extreme_leverage_balance_step_usdt))
    extreme_leverage_balance_step_usdt: float = 10.0
    extreme_leverage_min_cap: int = 2

    # Risk -- capital preservation first
    max_position_size_pct: float = 5.0
    max_daily_loss_pct: float = 3.0
    stop_loss_pct: float = 1.5
    take_profit_pct: float = 5.0
    max_concurrent_positions: int = 5
    min_signal_strength: float = 0.4  # ignore weak signals entirely
    consecutive_loss_cooldown: int = 3  # pause after N consecutive losses
    risk_env_multiplier: float = 1.0  # environment-level risk scaler (base risk * multiplier)
    max_total_exposure_mult: float = 1.5
    min_tradeable_equity_usdt: float = 50.0  # pause new entries below this equity
    low_balance_recheck_seconds: int = 300  # when paused, re-check balance every 5 min
    insufficient_balance_burst_threshold: int = 8  # repeated insufficient-balance errors before auto-disable
    insufficient_balance_burst_window_seconds: int = 900  # burst window for auto-disable logic
    insufficient_balance_auto_disable: bool = True  # hub-disable bot on repeated insufficient-balance errors

    @property
    def effective_max_position_size_pct(self) -> float:
        return max(0.01, float(self.max_position_size_pct) * max(0.01, float(self.risk_env_multiplier)))

    @property
    def effective_max_daily_loss_pct(self) -> float:
        return max(0.01, float(self.max_daily_loss_pct) * max(0.01, float(self.risk_env_multiplier)))

    @property
    def effective_max_concurrent_positions(self) -> int:
        return max(1, round(float(self.max_concurrent_positions) * max(0.01, float(self.risk_env_multiplier))))

    @property
    def effective_min_signal_strength(self) -> float:
        # Higher env multiplier relaxes signal gate proportionally.
        return max(0.01, min(1.0, float(self.min_signal_strength) / max(0.01, float(self.risk_env_multiplier))))

    @property
    def effective_consecutive_loss_cooldown(self) -> int:
        return max(1, round(float(self.consecutive_loss_cooldown) / max(0.01, float(self.risk_env_multiplier))))

    # Tick intervals (seconds) — adaptive, fastest active tier wins
    tick_interval_scalp: int = 1  # quick_trade / wick scalp positions (aggressive monitoring)
    tick_interval_active: int = 60  # standard positions (DCA, pyramid)
    tick_interval_swing: int = 300  # only swing positions open
    tick_interval_idle: int = 60  # no open positions, just scanning

    # Liquidity-aware scaling
    breakeven_lock_pct: float = 5.0  # move stop to entry once at this profit %
    pullback_trail_buffer_pct: float = 4.0  # non-aggressive trailing buffer below/above price (3-5% zone)
    initial_risk_amount: float = 50.0  # fixed $ amount for the first entry
    max_notional_position: float = 100_000.0  # stop adding once leveraged position hits this
    min_profit_to_add_pct: float = 1.0  # must be +1% before adding to position
    gambling_budget_pct: float = 2.0  # max % of balance for low-liq yolo bets
    min_liquidity_volume: float = 1_000_000  # 24h volume below this = "low liquidity"

    short_term_max_hold_minutes: int = 60  # auto-cut non-pyramid losers after this

    # Extreme mover strategy (6.5)
    extreme_enabled: bool = True
    extreme_min_hourly_move_pct: float = 5.0
    extreme_min_volume_24h: float = 10_000_000.0
    extreme_max_candidates: int = 10
    extreme_max_positions: int = 3
    extreme_position_size_pct: float = 3.0  # % of balance per extreme trade
    extreme_initial_stop_pct: float = 1.5
    extreme_trail_pct: float = 0.5
    extreme_loser_timeout_minutes: int = 1  # kill flat/losing positions after this
    extreme_eval_interval: int = 30  # seconds between watchlist re-evaluation
    extreme_stale_seconds: int = 300  # drop candidates older than this
    extreme_price_buffer_size: int = 30  # ticks to buffer per symbol for pattern detection

    # Major coins always get static strategies + regular queue proposals.
    # Separate from trending/scanner which only adds volatile movers.
    major_symbols: str = "BTC/USDT,ETH/USDT,SOL/USDT,XRP/USDT,DOGE/USDT,ADA/USDT,AVAX/USDT,LINK/USDT"

    # Pyramid / DCA mode (DEFAULT for all strategies)
    default_scale_mode: str = "pyramid"  # "pyramid" (DCA in) or "winners" (add to winners)
    dca_interval_pct: float = 2.0  # add every 2% the price drops
    dca_multiplier: float = 1.5  # each DCA add is 1.5x the previous
    dca_profit_to_lever_pct: float = 1.0  # raise leverage once avg entry is +1%
    dca_partial_take_pct: float = 30.0  # take 30% off the table after lever-up

    # Hedging
    hedge_enabled: bool = True
    hedge_ratio: float = 0.20  # hedge is 20% of main position size
    hedge_min_profit_pct: float = 3.0  # main must be +3% before hedging
    hedge_stop_pct: float = 1.0  # tight stop on hedge (it's a probe)
    max_hedges: int = 2  # max simultaneous hedges

    # Email
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    notify_email: str = ""
    notifications_enabled: str = "liquidation,daily_summary"
    send_daily_report: bool = False

    # Volatility
    spike_threshold_pct: float = 3.0
    volatility_lookback_minutes: int = 5

    # News
    news_enabled: bool = True
    news_sources: str = "coindesk,cointelegraph,cryptopanic"

    # Market Intelligence
    coinglass_api_key: str = ""  # optional, for CoinGlass premium endpoints
    fmp_api_key: str = ""  # Financial Modeling Prep (market holidays)
    intel_enabled: bool = True  # master switch for all external feeds
    openclaw_enabled: bool = True  # advisory-only external intelligence source
    openclaw_url: str = "http://host.docker.internal:18080/intel"  # OpenClaw intelligence JSON endpoint
    openclaw_token: str = ""  # optional bearer token for OpenClaw endpoint
    openclaw_poll_interval: int = Field(default=120, ge=15, le=3600)  # seconds between OpenClaw pulls
    openclaw_timeout_seconds: int = Field(default=8, ge=2, le=60)  # per-request timeout for OpenClaw fetches
    openclaw_daily_review_enabled: bool = True  # enable end-of-day optimization advisory run
    openclaw_daily_review_interval_hours: int = Field(default=24, ge=1, le=72)  # cadence for daily review
    openclaw_daily_review_force_paid: bool = True  # allow bridge paid lane for daily review
    fear_greed_poll: int = 3600  # how often to poll Fear & Greed (seconds)
    liquidation_poll: int = 300  # CoinGlass liquidation poll interval
    macro_calendar_poll: int = 1800  # ForexFactory calendar poll interval
    whale_sentiment_poll: int = 300  # CoinGlass funding/OI/L-S poll interval
    intel_symbols: str = "BTC,ETH"  # symbols to track for whale sentiment
    mass_liquidation_threshold: float = 1_000_000_000  # $1B = mass liq event

    # TradingView
    tv_exchange: str = "BINANCE"  # exchange name for TradingView scanner
    tv_intervals: str = "1h,4h,1D"  # timeframes to analyze
    tv_poll_interval: int = 120  # seconds between TV refreshes

    # CoinMarketCap
    cmc_api_key: str = ""  # optional, for CMC pro API (higher rate limits)
    cmc_poll_interval: int = 300  # seconds between CMC refreshes

    # CoinGecko
    coingecko_api_key: str = ""  # optional, for CoinGecko pro API
    coingecko_poll_interval: int = 300  # seconds between CoinGecko refreshes

    # DeFiLlama (free, no key needed)
    defillama_enabled: bool = True
    defillama_poll_interval: int = 600  # seconds between TVL refreshes

    # Santiment (free tier, optional key for higher rate limits)
    santiment_api_key: str = ""
    santiment_poll_interval: int = 600  # seconds between social data refreshes

    # CEX scanners (exchange-native trend feeds)
    cex_scanner_enabled: bool = True
    binance_scanner_enabled: bool = True
    binance_scanner_poll_interval: int = 60
    binance_scanner_min_quote_volume: float = 5_000_000.0
    binance_scanner_top_movers_count: int = 15
    binance_scanner_history_hours: int = 24
    binance_scanner_retention_days: int = 3

    # Dashboard (hub only)
    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 9035
    dashboard_token: str = ""  # set a secret token for remote access
    grafana_port: int = 3001

    # Hub persistence backend (PostgreSQL only)
    hub_db_backend: Literal["postgres"] = "postgres"
    hub_postgres_dsn: str = "postgresql://tradeborg:tradeborg@bot-hub-postgres:5432/trading_db"
    exchange_equity_snapshot_interval_seconds: int = 60  # hub DB snapshot cadence per exchange

    # Hub URL — bots POST status snapshots here
    hub_url: str = ""

    # Default-enabled profiles are runtime-configured via env override files.
    # Use the same key name across environments and set different values per file.
    bot_default_enabled_ids: str = ""

    # Exchange platform URL (for quick-link from the dashboard).
    # Leave empty for auto-detection based on exchange target + market type.
    exchange_platform_url: str = ""

    log_level: str = "INFO"

    @property
    def notification_list(self) -> list[str]:
        return [n.strip() for n in self.notifications_enabled.split(",") if n.strip()]

    @property
    def bot_strategy_list(self) -> list[str]:
        raw = getattr(self, "bot_strategies", "") or ""
        return [s.strip() for s in raw.split(",") if s.strip()]

    @property
    def data_dir(self) -> str:
        bid = getattr(self, "bot_id", "") or ""
        return f"data/{bid}" if bid else "data"

    @property
    def major_symbol_list(self) -> list[str]:
        raw = getattr(self, "major_symbols", "") or ""
        return [s.strip() for s in raw.split(",") if s.strip()]

    @property
    def intel_symbol_list(self) -> list[str]:
        return [s.strip().upper() for s in self.intel_symbols.split(",") if s.strip()]

    @property
    def openclaw_configured(self) -> bool:
        return bool((self.openclaw_url or "").strip())

    @property
    def news_source_list(self) -> list[str]:
        return [s.strip() for s in self.news_sources.split(",") if s.strip()]

    @property
    def tv_interval_list(self) -> list[str]:
        return [s.strip() for s in self.tv_intervals.split(",") if s.strip()]

    @property
    def allowed_market_type_list(self) -> list[str]:
        return [t.strip().lower() for t in self.allowed_market_types.split(",") if t.strip()]

    @property
    def spot_allowed(self) -> bool:
        return "spot" in self.allowed_market_type_list

    @property
    def futures_allowed(self) -> bool:
        return "futures" in self.allowed_market_type_list

    def is_market_type_allowed(self, market_type: str) -> bool:
        return market_type.lower() in self.allowed_market_type_list

    def is_paper(self) -> bool:
        """Compatibility shim; sandbox mode is considered paper."""
        return bool(self.exchange_is_sandbox)

    def is_paper_local(self) -> bool:
        """Legacy shim kept for test compatibility."""
        return False

    def is_paper_live(self) -> bool:
        """Legacy shim kept for test compatibility."""
        return bool(self.exchange_is_sandbox)

    def cap_balance(self, raw_balance: float) -> float:
        """Apply session_budget cap. Returns raw_balance if no cap is set."""
        if self.session_budget > 0:
            return min(raw_balance, self.session_budget)
        return raw_balance

    # ---- exchange platform URLs (auto-resolve if not set) ----

    _PLATFORM_URLS: dict[str, dict[str, str]] = {
        "binance": {
            "paper_futures": "https://demo.binance.com/en/futures",
            "paper_spot": "https://demo.binance.com/en/trade",
            "live_futures": "https://www.binance.com/en/futures",
            "live_spot": "https://www.binance.com/en/trade",
        },
        "bybit": {
            "paper_futures": "https://testnet.bybit.com/trade/usdt",
            "paper_spot": "https://testnet.bybit.com/trade/spot",
            "live_futures": "https://www.bybit.com/trade/usdt",
            "live_spot": "https://www.bybit.com/trade/spot",
        },
    }

    @property
    def platform_url(self) -> str:
        """Base URL for the exchange trading UI."""
        if self.exchange_platform_url:
            return self.exchange_platform_url
        mode = "paper" if self.exchange_is_sandbox else "live"
        mkt = "futures" if self.futures_allowed else "spot"
        urls = self._PLATFORM_URLS.get(self.exchange_base, {})
        return urls.get(f"{mode}_{mkt}", "")

    def symbol_platform_url(self, symbol: str, market_type: str = "") -> str:
        """Direct link to a specific trading pair on the exchange."""
        base = self.platform_url
        if not base:
            return ""
        clean = symbol.replace("/", "")
        mkt = market_type or ("futures" if self.futures_allowed else "spot")
        if self.exchange_base == "binance":
            if mkt == "futures":
                return f"{base}/{clean}"
            return f"{base}/{clean.replace('USDT', '_USDT')}"
        if self.exchange_base == "bybit":
            return f"{base}/{clean}"
        return base

    @property
    def exchange_base(self) -> str:
        """Canonical exchange id used by adapters and hub contracts."""
        raw = str(self.exchange or "").strip().lower()
        if raw in {"binance", "binance_testnet", "binance_demo"}:
            return "binance"
        if raw in {"bybit", "bybit_testnet"}:
            return "bybit"
        return raw

    @property
    def exchange_is_sandbox(self) -> bool:
        """Whether configured exchange target is a testnet/demo endpoint."""
        raw = str(self.exchange or "").strip().lower()
        return raw in {"binance_testnet", "binance_demo", "bybit_testnet"}

    @staticmethod
    def _parse_csv_ids(raw: str) -> set[str]:
        return {part.strip().lower() for part in str(raw or "").split(",") if part.strip()}

    @property
    def default_enabled_bot_ids(self) -> set[str]:
        """Runtime-configured default enabled bot IDs."""
        return self._parse_csv_ids(self.bot_default_enabled_ids)

    # ---- API keys ----

    @staticmethod
    def _url_looks_testnet(url: str) -> bool:
        u = (url or "").strip().lower()
        return any(
            hint in u
            for hint in (
                "testnet",
                "demo.binance.com",
                "demo-fapi.binance.com",
            )
        )

    @staticmethod
    def _url_looks_production(url: str) -> bool:
        u = (url or "").strip().lower()
        return any(
            hint in u
            for hint in (
                "www.binance.com",
                "www.bybit.com",
            )
        )

    def validate_startup_mode_guard(self) -> None:
        """Validate startup config without mode-name branching."""
        exchange = self.exchange_base

        if exchange in {"binance", "bybit"}:
            if exchange == "binance" and (not self.binance_api_key or not self.binance_api_secret):
                raise ValueError("Binance requires BINANCE_API_KEY and BINANCE_API_SECRET.")
            if exchange == "bybit" and (not self.bybit_api_key or not self.bybit_api_secret):
                raise ValueError("Bybit requires BYBIT_API_KEY and BYBIT_API_SECRET.")

        custom_url = str(self.exchange_platform_url or "").strip()
        if custom_url:
            if self.exchange_is_sandbox and self._url_looks_production(custom_url):
                raise ValueError("Sandbox mode cannot use production EXCHANGE_PLATFORM_URL.")
            if (not self.exchange_is_sandbox) and self._url_looks_testnet(custom_url):
                raise ValueError("Production mode cannot use testnet/demo EXCHANGE_PLATFORM_URL.")


@lru_cache
def get_settings() -> Settings:
    return Settings()
