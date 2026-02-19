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

    - Validates ALLOWED_MARKET_TYPES against exchange capabilities.
    - Enables testnet/sandbox automatically when in paper mode on
      exchanges that have a testnet (Binance, Bybit).
    - MEXC has no testnet; paper mode uses real market data with
      simulated order execution via PaperExchange.
    """

    use_paper = settings.is_paper()

    exchange_map: dict[str, tuple[type[BaseExchange], dict]] = {
        "mexc": (MexcExchange, {
            "api_key": settings.mexc_api_key,
            "api_secret": settings.mexc_api_secret,
        }),
        "binance": (BinanceExchange, {
            "api_key": settings.binance_api_key,
            "api_secret": settings.binance_api_secret,
        }),
        "bybit": (BybitExchange, {
            "api_key": settings.bybit_api_key,
            "api_secret": settings.bybit_api_secret,
        }),
    }

    entry = exchange_map.get(settings.exchange)
    if not entry:
        raise ValueError(f"Unsupported exchange: {settings.exchange}. "
                         f"Supported: {', '.join(exchange_map)}")

    cls, kwargs = entry
    sandbox = use_paper and cls.HAS_TESTNET
    real_exchange = cls(**kwargs, sandbox=sandbox)

    if sandbox:
        logger.info("Testnet mode enabled for {} (paper trading with testnet keys)",
                     settings.exchange.upper())

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

    logger.info("Exchange: {} | Supported: {} | Allowed: {} | Testnet: {}",
                settings.exchange.upper(),
                ", ".join(real_exchange.SUPPORTED_MARKET_TYPES),
                settings.allowed_market_types,
                sandbox)

    if use_paper:
        budget = settings.session_budget if settings.session_budget > 0 else 10_000.0
        logger.info("Paper session budget: ${:.2f}", budget)
        return PaperExchange(real_exchange, starting_balance=budget)

    return real_exchange
