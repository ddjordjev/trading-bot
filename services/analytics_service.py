"""Analytics service (runs in-process inside the hub).

Reads trade history from hub.db, computes strategy scores, detects
patterns, generates suggestions, and persists results via HubState.

On startup the previously persisted snapshot is already loaded by HubState,
so scores/patterns/suggestions are available immediately.  A full recompute
only happens when new trades are detected or on a periodic cadence (default
every 30 minutes) to pick up time-sensitive shifts like streak cooloff.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from loguru import logger

from analytics.engine import AnalyticsEngine
from db.store import TradeDB
from hub.state import HubState
from shared.models import AnalyticsSnapshot, StrategyWeightEntry

HUB_DB = Path("data/hub.db")

_FULL_REFRESH_INTERVAL = 1800  # 30 min — periodic recompute even without new trades


class AnalyticsService:
    """Incremental analytics process.

    - On boot: persisted snapshot is already in HubState (loaded from disk).
      If new trades arrived while the hub was down, recompute immediately.
      Otherwise skip the expensive full refresh.
    - On each tick: check trade count.  Only recompute when new trades exist.
    - Every 30 min: force a recompute regardless (streak cooloff, time shifts).
    """

    def __init__(self, refresh_interval: int = 300, state: HubState | None = None):
        self.refresh_interval = refresh_interval
        self.state: HubState = state or HubState()
        self.db = TradeDB(path=HUB_DB)
        self.engine: AnalyticsEngine | None = None
        self._running = False
        self._last_trade_count = 0
        self._last_full_refresh: float = 0.0

    async def start(self) -> None:
        logger.info("=" * 50)
        logger.info("ANALYTICS SERVICE v2.0 (incremental)")
        logger.info("Tick interval: {}s | Full refresh: {}s", self.refresh_interval, _FULL_REFRESH_INTERVAL)
        logger.info("=" * 50)

        self.db.connect()
        self.engine = AnalyticsEngine(self.db)
        self._last_trade_count = self.db.trade_count()
        self._running = True

        existing = self.state.read_analytics()
        if existing.weights and existing.total_trades_logged == self._last_trade_count:
            logger.info(
                "Analytics loaded from disk: {} strategies, {} patterns — no new trades, skipping recompute",
                len(existing.weights),
                len(existing.patterns),
            )
        else:
            new = self._last_trade_count - existing.total_trades_logged
            logger.info(
                "Analytics: {} new trade(s) since last persist (disk had {}), recomputing...",
                max(new, 0),
                existing.total_trades_logged,
            )
            self._do_refresh()

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
                now = time.monotonic()
                force_periodic = (now - self._last_full_refresh) >= _FULL_REFRESH_INTERVAL

                if new_trades > 0:
                    logger.info("Detected {} new trade(s) (total: {}), refreshing...", new_trades, current_count)
                    self._do_refresh()
                    self._last_trade_count = current_count
                elif force_periodic:
                    logger.debug("Periodic analytics refresh (no new trades)")
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
        self._last_full_refresh = time.monotonic()

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
