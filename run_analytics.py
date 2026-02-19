#!/usr/bin/env python3
"""Entry point for the standalone analytics service.

Usage:
    python run_analytics.py

Runs independently of the trading bot. Reads trade history from
data/trades.db, computes strategy scores and patterns, and writes
results to data/analytics_state.json.

Refreshes every 5 minutes or when new trades are detected.
"""

import asyncio
import signal
import sys

from loguru import logger

from config.settings import get_settings
from services.analytics_service import AnalyticsService

_background_tasks: list = []


def main() -> None:
    settings = get_settings()

    logger.remove()
    logger.add(sys.stderr, level=settings.log_level)
    logger.add("logs/analytics_{time}.log", rotation="1 day", retention="14 days", level="DEBUG")

    service = AnalyticsService(refresh_interval=300)
    loop = asyncio.new_event_loop()

    def _shutdown(sig_num: int, frame: object) -> None:
        logger.info("Received signal {}, shutting down analytics...", sig_num)
        _background_tasks.append(loop.create_task(service.stop()))

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        loop.run_until_complete(service.start())
    except KeyboardInterrupt:
        loop.run_until_complete(service.stop())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
