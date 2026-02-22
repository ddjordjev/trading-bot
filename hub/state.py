"""In-memory state for the hub process.

MonitorService, AnalyticsService, and the FastAPI endpoints all share
a single HubState instance.  This is the sole state backend — there is
no file-based alternative.

Analytics (strategy scores, patterns, suggestions) are persisted to disk
so they survive restarts.  Everything else is ephemeral.

Thread-safety: everything runs in one asyncio event loop, so plain
dicts/lists are safe.  If we ever add threads, swap to asyncio.Lock.
"""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from loguru import logger

from shared.models import (
    AnalyticsSnapshot,
    BotDeploymentStatus,
    ExtremeWatchlist,
    IntelSnapshot,
    TradeProposal,
    TradeQueue,
)

_ANALYTICS_PATH = Path("data/analytics_state.json")


class RejectionRecord:
    __slots__ = ("count", "reason", "timestamp")

    def __init__(self, reason: str, timestamp: datetime, count: int = 1):
        self.reason = reason
        self.timestamp = timestamp
        self.count = count


class HubState:
    """In-memory state backend for the hub.

    Analytics snapshot is the one exception — it's persisted to disk on
    every write and loaded on init so strategy scores, patterns, and
    suggestions survive hub restarts.
    """

    def __init__(self, data_dir: Path | None = None) -> None:
        self._intel: IntelSnapshot = IntelSnapshot()
        self._extreme_watchlist: ExtremeWatchlist = ExtremeWatchlist()

        self._trade_queue: TradeQueue = TradeQueue()
        self._rejections: dict[str, RejectionRecord] = {}  # "symbol|strategy" → record
        self._dispatched: list[TradeProposal] = []  # recently dispatched for dashboard
        self._dispatched_max = 50

        self._bot_statuses: dict[str, BotDeploymentStatus] = {}

        self._analytics_path = (data_dir / "analytics_state.json") if data_dir else _ANALYTICS_PATH
        self._analytics: AnalyticsSnapshot = self._load_analytics()

    # ---- Intel (written by monitor, read by endpoints / bots) ---- #

    def write_intel(self, intel: IntelSnapshot) -> None:
        intel.updated_at = datetime.now(UTC).isoformat()
        self._intel = intel

    def read_intel(self) -> IntelSnapshot:
        return self._intel

    def intel_age_seconds(self) -> float:
        if not self._intel.updated_at:
            return 999999
        try:
            ts = self._intel.updated_at.replace("Z", "+00:00")
            updated = datetime.fromisoformat(ts)
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=UTC)
            return (datetime.now(UTC) - updated).total_seconds()
        except Exception:
            return 999999

    # ---- Analytics (written by analytics svc, read by endpoints / bots) ---- #
    # Persisted to disk so scores/patterns/suggestions survive restarts.

    def write_analytics(self, analytics: AnalyticsSnapshot) -> None:
        analytics.updated_at = datetime.now(UTC).isoformat()
        self._analytics = analytics
        self._save_analytics(analytics)

    def read_analytics(self) -> AnalyticsSnapshot:
        return self._analytics

    def _load_analytics(self) -> AnalyticsSnapshot:
        try:
            raw = self._analytics_path.read_text()
            snap = AnalyticsSnapshot.model_validate_json(raw)
            logger.info(
                "Loaded analytics from disk: {} strategies, {} patterns, {} suggestions",
                len(snap.weights),
                len(snap.patterns),
                len(snap.suggestions),
            )
            return snap
        except FileNotFoundError:
            return AnalyticsSnapshot()
        except Exception as e:
            logger.warning("Failed to load analytics from {}: {}", self._analytics_path, e)
            return AnalyticsSnapshot()

    def _save_analytics(self, analytics: AnalyticsSnapshot) -> None:
        try:
            self._analytics_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self._analytics_path.parent), suffix=".tmp")
            closed = False
            try:
                data = analytics.model_dump_json(indent=2)
                os.write(fd, data.encode())
                os.fsync(fd)
                os.close(fd)
                closed = True
                os.replace(tmp, str(self._analytics_path))
            except BaseException:
                if not closed:
                    os.close(fd)
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
        except Exception as e:
            logger.warning("Failed to persist analytics to {}: {}", self._analytics_path, e)

    # ---- Extreme watchlist (written by monitor, read by bots) ---- #

    def write_extreme_watchlist(self, watchlist: ExtremeWatchlist) -> None:
        watchlist.updated_at = datetime.now(UTC).isoformat()
        self._extreme_watchlist = watchlist

    def read_extreme_watchlist(self) -> ExtremeWatchlist:
        return self._extreme_watchlist

    # ---- Bot status (written via /internal/report, read by monitor) ---- #

    def write_bot_status(self, status: BotDeploymentStatus) -> None:
        status.updated_at = datetime.now(UTC).isoformat()
        if status.bot_id:
            self._bot_statuses[status.bot_id] = status

    def read_bot_status(self) -> BotDeploymentStatus:
        if self._bot_statuses:
            return next(iter(self._bot_statuses.values()))
        return BotDeploymentStatus()

    def read_all_bot_statuses(self) -> list[BotDeploymentStatus]:
        return list(self._bot_statuses.values())

    # ---- Trade queue (written by monitor/signal_gen, read by bots) ---- #

    def write_trade_queue(self, queue: TradeQueue) -> None:
        queue.updated_at = datetime.now(UTC).isoformat()
        self._trade_queue = queue

    def read_trade_queue(self) -> TradeQueue:
        return self._trade_queue

    def apply_trade_queue_updates(
        self,
        consumed_ids: list[str],
        rejected: dict[str, str],
    ) -> None:
        if not consumed_ids and not rejected:
            return
        q = self._trade_queue
        for pid in consumed_ids:
            q.mark_consumed(pid)
        for pid, reason in rejected.items():
            q.mark_rejected(pid, reason)
        q.updated_at = datetime.now(UTC).isoformat()

    def apply_bot_queue_updates(
        self,
        bot_id: str,
        consumed_ids: list[str],
        rejected: dict[str, str],
    ) -> None:
        """Process consumed/rejected reports from a bot.

        Updates the dispatched list (dashboard) and records rejections
        so the signal generator can cool down on those symbols.
        """
        if not consumed_ids and not rejected:
            return

        for dp in self._dispatched:
            if dp.id in consumed_ids:
                dp.consumed = True
            elif dp.id in rejected:
                dp.rejected = True
                dp.reject_reason = rejected[dp.id]

        for pid, reason in rejected.items():
            proposal = self._find_dispatched_by_id(pid)
            if not proposal:
                continue
            rkey = f"{proposal.symbol}|{proposal.strategy}"
            existing = self._rejections.get(rkey)
            if existing:
                existing.reason = reason
                existing.timestamp = datetime.now(UTC)
                existing.count += 1
            else:
                self._rejections[rkey] = RejectionRecord(reason, datetime.now(UTC))

    def _find_dispatched_by_id(self, pid: str) -> TradeProposal | None:
        for p in self._dispatched:
            if p.id == pid:
                return p
        return None

    def read_queue_for_bot_style(self, bot_style: str, exchange: str = "") -> TradeQueue:
        """Pop the top matching proposal for this bot style + exchange.

        Each bot gets exactly 1 proposal per request (the highest-priority
        one). The rest stay in the queue for other bots.  Priority order:
        critical → daily → swing.

        If *exchange* is provided, only proposals whose supported_exchanges
        include that exchange are considered.  Proposals meant for other
        exchanges are silently skipped (left in the queue for the right bot).
        """
        result = TradeQueue()
        picked = None
        picked_bucket = None
        picked_idx = None
        ex_upper = exchange.upper() if exchange else ""

        for bucket_name in ("critical", "daily", "swing"):
            src: list = getattr(self._trade_queue, bucket_name)
            for i, p in enumerate(src):
                if p.consumed or p.rejected or p.is_expired:
                    continue
                if ex_upper and p.supported_exchanges and ex_upper not in p.supported_exchanges:
                    continue
                targets = {t.strip() for t in (p.target_bot or "").split(",") if t.strip()}
                if not targets or bot_style in targets:
                    picked = p
                    picked_bucket = bucket_name
                    picked_idx = i
                    break
            if picked:
                break

        if picked and picked_bucket is not None and picked_idx is not None:
            getattr(self._trade_queue, picked_bucket).pop(picked_idx)
            getattr(result, picked_bucket).append(picked)
            self._dispatched = [picked, *self._dispatched][: self._dispatched_max]

        result.updated_at = self._trade_queue.updated_at
        return result

    def read_dispatched_proposals(self) -> list[TradeProposal]:
        """Recent proposals dispatched to bots — for dashboard display."""
        cutoff = datetime.now(UTC) - timedelta(minutes=30)
        self._dispatched = [
            p for p in self._dispatched if p.created_at and datetime.fromisoformat(p.created_at) > cutoff
        ]
        return self._dispatched

    def get_rejection_history(self) -> dict[str, RejectionRecord]:
        """Return rejection records for signal generator to consult."""
        return self._rejections

    def purge_old_rejections(self, max_age_hours: int = 24) -> None:
        cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)
        self._rejections = {k: v for k, v in self._rejections.items() if v.timestamp > cutoff}
