from __future__ import annotations

from loguru import logger

from config.settings import Settings
from core.exchange.base import BaseExchange
from core.exchange.binance import BinanceExchange
from core.exchange.bybit import BybitExchange


def create_exchange(settings: Settings) -> BaseExchange:
    """Factory that builds the right exchange client based on config.

    Behavior is exchange-target driven:
      EXCHANGE=binance/bybit           -> production endpoints
      EXCHANGE=binance_testnet/_demo   -> sandbox endpoints
      EXCHANGE=bybit_testnet           -> sandbox endpoints
    """

    # Centralized startup guard to prevent mixing test/prod credentials/URLs.
    settings.validate_startup_mode_guard()

    exchange_map: dict[str, tuple[type[BaseExchange], dict[str, str]]] = {
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

    entry = exchange_map.get(settings.exchange_base)
    if not entry:
        raise ValueError(f"Unsupported exchange: {settings.exchange}. Supported: {', '.join(exchange_map)}")

    cls, kwargs = entry
    sandbox = bool(settings.exchange_is_sandbox and cls.HAS_TESTNET)
    real_exchange = cls(**kwargs, sandbox=sandbox)

    if sandbox:
        logger.info("Sandbox mode enabled for {} (testnet/demo orders)", settings.exchange_base.upper())

    wanted = set(settings.allowed_market_type_list)
    supported = set(real_exchange.SUPPORTED_MARKET_TYPES)
    unsupported = wanted - supported
    if unsupported:
        logger.warning(
            "{} does not support: {}. Restricting to: {}",
            settings.exchange_base.upper(),
            ", ".join(sorted(unsupported)),
            ", ".join(sorted(wanted & supported)) or "spot",
        )
        effective = sorted(wanted & supported) or ["spot"]
        settings.allowed_market_types = ",".join(effective)

    logger.info(
        "Exchange: {} | Sandbox: {} | Supported: {} | Allowed: {}",
        settings.exchange_base.upper(),
        bool(settings.exchange_is_sandbox),
        ", ".join(real_exchange.SUPPORTED_MARKET_TYPES),
        settings.allowed_market_types,
    )

    if settings.exchange_is_sandbox and not cls.HAS_TESTNET:
        raise ValueError(f"{settings.exchange_base.upper()} has no testnet support but sandbox target was requested.")

    return real_exchange
