"""Standalone analytics service.

Runs independently. Reads trade history from data/trades.db,
computes strategy scores, detects patterns, generates suggestions,
and writes results to data/analytics_state.json.

The bot reads analytics_state.json to get strategy weights without
needing to run the analytics engine itself.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from loguru import logger

from analytics.engine import AnalyticsEngine
from db.store import TradeDB
from shared.models import AnalyticsSnapshot, StrategyWeightEntry
from shared.state import SharedState

UNIFIED_DB = Path("data/trades_all.db")


class AnalyticsService:
    """Standalone analytics process.

    Refreshes periodically (default every 5 minutes) and after
    detecting new trades in the database.
    """

    def __init__(self, refresh_interval: int = 300):
        self.refresh_interval = refresh_interval
        self.state = SharedState()
        self.db = TradeDB(path=UNIFIED_DB)
        self.engine: AnalyticsEngine | None = None
        self._running = False
        self._last_trade_count = 0

    async def start(self) -> None:
        logger.info("=" * 50)
        logger.info("ANALYTICS SERVICE v1.0")
        logger.info("Refresh interval: {}s", self.refresh_interval)
        logger.info("=" * 50)

        self.db.connect()
        self.engine = AnalyticsEngine(self.db)
        self._last_trade_count = self.db.trade_count()
        self._running = True

        self._do_refresh()
        logger.info("Initial refresh: {} trades, {} strategies scored", self._last_trade_count, len(self.engine.scores))

        await self._run_loop()

    async def stop(self) -> None:
        self._running = False
        self.db.close()
        logger.info("Analytics service stopped")

    async def _run_loop(self) -> None:
        while self._running:
            try:
                current_count = self.db.trade_count()
                new_trades = current_count - self._last_trade_count

                if new_trades > 0:
                    logger.info("Detected {} new trade(s) (total: {}), refreshing...", new_trades, current_count)
                    self._do_refresh()
                    self._last_trade_count = current_count
                else:
                    # Periodic refresh even without new trades (scores may shift
                    # due to time-based factors like streak cooloff)
                    self._do_refresh()

                await asyncio.sleep(self.refresh_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Analytics tick error: {}", e)
                await asyncio.sleep(30)

    def _do_refresh(self) -> None:
        assert self.engine is not None
        self.engine.refresh()

        weights = []
        for name, score in self.engine.scores.items():
            weights.append(
                StrategyWeightEntry(
                    strategy=name,
                    weight=score.weight,
                    win_rate=score.win_rate,
                    total_trades=score.total_trades,
                    total_pnl=score.total_pnl,
                    streak=score.streak_current,
                )
            )

        patterns = [p.model_dump() for p in self.engine.patterns]
        suggestions = [s.model_dump() for s in self.engine.suggestions]

        snapshot = AnalyticsSnapshot(
            weights=weights,
            patterns=patterns,
            suggestions=suggestions,
            total_trades_logged=self.db.trade_count(),
        )
        self.state.write_analytics(snapshot)

        if weights:
            logger.debug(
                "Analytics written: {} strategies, {} patterns, {} suggestions",
                len(weights),
                len(patterns),
                len(suggestions),
            )
