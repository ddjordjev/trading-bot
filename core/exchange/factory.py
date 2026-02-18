from __future__ import annotations

from config.settings import Settings
from core.exchange.base import BaseExchange
from core.exchange.binance import BinanceExchange
from core.exchange.bybit import BybitExchange
from core.exchange.mexc import MexcExchange
from core.exchange.paper import PaperExchange


def create_exchange(settings: Settings) -> BaseExchange:
    """Factory that builds the right exchange client based on config."""

    exchange_map = {
        "mexc": lambda: MexcExchange(
            api_key=settings.mexc_api_key,
            api_secret=settings.mexc_api_secret,
            sandbox=False,
        ),
        "binance": lambda: BinanceExchange(
            api_key=settings.binance_api_key,
            api_secret=settings.binance_api_secret,
            sandbox=False,
        ),
        "bybit": lambda: BybitExchange(
            api_key=settings.bybit_api_key,
            api_secret=settings.bybit_api_secret,
            sandbox=False,
        ),
    }

    builder = exchange_map.get(settings.exchange)
    if not builder:
        raise ValueError(f"Unsupported exchange: {settings.exchange}. "
                         f"Supported: {', '.join(exchange_map)}")

    real_exchange = builder()

    if settings.is_paper():
        return PaperExchange(real_exchange)

    return real_exchange
