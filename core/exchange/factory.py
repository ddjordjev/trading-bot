from __future__ import annotations

from loguru import logger

from config.settings import Settings
from core.exchange.base import BaseExchange
from core.exchange.binance import BinanceExchange
from core.exchange.bybit import BybitExchange
from core.exchange.mexc import MexcExchange
from core.exchange.paper import PaperExchange


def create_exchange(settings: Settings) -> BaseExchange:
    """Factory that builds the right exchange client based on config.

    Trading modes:
      paper_local  — PaperExchange wrapper (local simulation, no orders hit exchange)
      paper_live   — Real orders on exchange testnet (e.g. demo.binance.com)
      live         — Real orders on production exchange

    Both paper modes use testnet API keys. Live uses production keys.
    """

    exchange_map: dict[str, tuple[type[BaseExchange], dict[str, str]]] = {
        "mexc": (
            MexcExchange,
            {
                "api_key": settings.mexc_api_key,
                "api_secret": settings.mexc_api_secret,
            },
        ),
        "binance": (
            BinanceExchange,
            {
                "api_key": settings.binance_api_key,
                "api_secret": settings.binance_api_secret,
            },
        ),
        "bybit": (
            BybitExchange,
            {
                "api_key": settings.bybit_api_key,
                "api_secret": settings.bybit_api_secret,
            },
        ),
    }

    entry = exchange_map.get(settings.exchange)
    if not entry:
        raise ValueError(f"Unsupported exchange: {settings.exchange}. Supported: {', '.join(exchange_map)}")

    cls, kwargs = entry
    sandbox = settings.is_paper() and cls.HAS_TESTNET
    real_exchange = cls(**kwargs, sandbox=sandbox)

    if sandbox:
        label = "local simulation" if settings.is_paper_local() else "testnet orders"
        logger.info("Testnet mode enabled for {} ({})", settings.exchange.upper(), label)

    wanted = set(settings.allowed_market_type_list)
    supported = set(real_exchange.SUPPORTED_MARKET_TYPES)
    unsupported = wanted - supported
    if unsupported:
        logger.warning(
            "{} does not support: {}. Restricting to: {}",
            settings.exchange.upper(),
            ", ".join(sorted(unsupported)),
            ", ".join(sorted(wanted & supported)) or "spot",
        )
        effective = sorted(wanted & supported) or ["spot"]
        settings.allowed_market_types = ",".join(effective)

    logger.info(
        "Exchange: {} | Mode: {} | Supported: {} | Allowed: {} | Testnet: {}",
        settings.exchange.upper(),
        settings.trading_mode,
        ", ".join(real_exchange.SUPPORTED_MARKET_TYPES),
        settings.allowed_market_types,
        sandbox,
    )

    if settings.is_paper_live() and not cls.HAS_TESTNET:
        logger.warning(
            "{} has no testnet — paper_live would trade real money. Falling back to paper_local.",
            settings.exchange.upper(),
        )
        settings.trading_mode = "paper_local"

    if settings.is_paper_local():
        budget = settings.session_budget if settings.session_budget > 0 else 10_000.0
        logger.info("Paper LOCAL: simulated balance ${:.2f} (no orders hit exchange)", budget)
        return PaperExchange(real_exchange, starting_balance=budget)

    if settings.is_paper_live():
        logger.info("Paper LIVE: real orders on testnet (visible on exchange demo)")
        if settings.session_budget > 0:
            logger.info("Session budget cap: ${:.2f} (bot won't use more than this)", settings.session_budget)

    return real_exchange
