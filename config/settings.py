from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    trading_mode: Literal["paper_local", "paper_live", "live"] = "paper_local"

    exchange: str = "mexc"

    # MEXC (no testnet — same keys for paper & live)
    mexc_api_key: str = ""
    mexc_api_secret: str = ""

    # Binance — separate prod and testnet keys
    binance_prod_api_key: str = ""
    binance_prod_api_secret: str = ""
    binance_test_api_key: str = ""
    binance_test_api_secret: str = ""

    # Bybit — separate prod and testnet keys
    bybit_prod_api_key: str = ""
    bybit_prod_api_secret: str = ""
    bybit_test_api_key: str = ""
    bybit_test_api_secret: str = ""

    # What market types the user wants to use. The factory auto-restricts
    # to what the exchange actually supports (e.g. MEXC = spot only).
    allowed_market_types: str = "spot,futures"

    # 0 = no cap (use full exchange balance)
    session_budget: float = 100.0

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
    notifications_enabled: str = "liquidation,stop_loss,spike_detected,daily_summary"

    # Volatility
    spike_threshold_pct: float = 3.0
    volatility_lookback_minutes: int = 5

    # News
    news_enabled: bool = False
    news_sources: str = "coindesk,cointelegraph,cryptopanic"

    # Market Intelligence
    coinglass_api_key: str = ""  # optional, for CoinGlass premium endpoints
    fmp_api_key: str = ""  # Financial Modeling Prep (market holidays)
    intel_enabled: bool = True  # master switch for all external feeds
    fear_greed_poll: int = 3600  # how often to poll Fear & Greed (seconds)
    liquidation_poll: int = 300  # CoinGlass liquidation poll interval
    macro_calendar_poll: int = 1800  # ForexFactory calendar poll interval
    whale_sentiment_poll: int = 300  # CoinGlass funding/OI/L-S poll interval
    intel_symbols: str = "BTC,ETH"  # symbols to track for whale sentiment
    mass_liquidation_threshold: float = 1_000_000_000  # $1B = mass liq event

    # TradingView
    tv_exchange: str = "MEXC"  # exchange name for TradingView scanner
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

    # Glassnode (requires free API key from glassnode.com)
    glassnode_api_key: str = ""
    glassnode_poll_interval: int = 900  # seconds between on-chain refreshes

    # Dashboard
    dashboard_enabled: bool = True
    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 9035
    dashboard_token: str = ""  # set a secret token for remote access

    # Exchange platform URL (for quick-link from the dashboard).
    # Leave empty for auto-detection based on exchange + trading_mode.
    exchange_platform_url: str = ""

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
        """True for both paper modes (local sim and testnet). Used for API key
        selection (test keys) and safety checks."""
        return self.trading_mode in ("paper_local", "paper_live")

    def is_paper_local(self) -> bool:
        """Local simulation via PaperExchange — no orders hit any exchange."""
        return self.trading_mode == "paper_local"

    def is_paper_live(self) -> bool:
        """Real orders on exchange testnet (e.g. demo.binance.com)."""
        return self.trading_mode == "paper_live"

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
        "mexc": {
            "paper_spot": "https://www.mexc.com/exchange",
            "paper_futures": "https://www.mexc.com/exchange",
            "live_spot": "https://www.mexc.com/exchange",
            "live_futures": "https://www.mexc.com/exchange",
        },
    }

    @property
    def platform_url(self) -> str:
        """Base URL for the exchange trading UI."""
        if self.exchange_platform_url:
            return self.exchange_platform_url
        mode = "paper" if self.is_paper() else "live"
        mkt = "futures" if self.futures_allowed else "spot"
        urls = self._PLATFORM_URLS.get(self.exchange, {})
        return urls.get(f"{mode}_{mkt}", "")

    def symbol_platform_url(self, symbol: str, market_type: str = "") -> str:
        """Direct link to a specific trading pair on the exchange."""
        base = self.platform_url
        if not base:
            return ""
        clean = symbol.replace("/", "")
        mkt = market_type or ("futures" if self.futures_allowed else "spot")
        if self.exchange == "binance":
            if mkt == "futures":
                return f"{base}/{clean}"
            return f"{base}/{clean.replace('USDT', '_USDT')}"
        if self.exchange == "bybit":
            return f"{base}/{clean}"
        if self.exchange == "mexc":
            return f"{base}/{clean.replace('USDT', '_USDT')}"
        return base

    # ---- API key resolution (prod vs test) ----

    @property
    def binance_api_key(self) -> str:
        return self.binance_test_api_key if self.is_paper() else self.binance_prod_api_key

    @property
    def binance_api_secret(self) -> str:
        return self.binance_test_api_secret if self.is_paper() else self.binance_prod_api_secret

    @property
    def bybit_api_key(self) -> str:
        return self.bybit_test_api_key if self.is_paper() else self.bybit_prod_api_key

    @property
    def bybit_api_secret(self) -> str:
        return self.bybit_test_api_secret if self.is_paper() else self.bybit_prod_api_secret


@lru_cache
def get_settings() -> Settings:
    return Settings()
