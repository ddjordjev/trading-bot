from __future__ import annotations

from config.settings import Settings
from core.exchange.base import BaseExchange
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
    }

    builder = exchange_map.get(settings.exchange)
    if not builder:
        raise ValueError(f"Unsupported exchange: {settings.exchange}")

    real_exchange = builder()

    if settings.is_paper():
        return PaperExchange(real_exchange)

    return real_exchange
