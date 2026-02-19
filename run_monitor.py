#!/usr/bin/env python3
"""Entry point for the standalone monitoring service.

Usage:
    python run_monitor.py

Runs independently of the trading bot. Polls external data sources
and writes results to data/intel_state.json. Reads bot deployment
status from data/bot_status.json to adjust polling intensity:

    HUNTING  → full speed (bot is idle, looking for trades)
    ACTIVE   → normal speed (some positions, capacity remains)
    DEPLOYED → background (fully deployed, positions running well)
    STRESSED → elevated (positions losing, need exit/hedge intel)
"""

import asyncio
import signal
import sys

from loguru import logger

from config.settings import get_settings
from services.monitor import MonitorService

_background_tasks: list = []


def main() -> None:
    settings = get_settings()

    logger.remove()
    logger.add(sys.stderr, level=settings.log_level)
    logger.add("logs/monitor_{time}.log", rotation="1 day", retention="14 days", level="DEBUG")

    service = MonitorService(settings)
    loop = asyncio.new_event_loop()

    def _shutdown(sig_num: int, frame: object) -> None:
        logger.info("Received signal {}, shutting down monitor...", sig_num)
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
