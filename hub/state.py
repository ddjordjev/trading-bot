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
import uuid
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
        self._bot_queues: dict[str, TradeQueue] = {}
        self._rejections: dict[str, RejectionRecord] = {}  # "symbol|strategy" → record

        self._bot_statuses: dict[str, BotDeploymentStatus] = {}
        self._exchange_symbols: dict[str, dict] = {}

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

    # ---- Exchange symbols (written via /internal/report, read by monitor) ---- #

    def write_exchange_symbols(self, bot_id: str, exchange: str, symbols: list[str]) -> None:
        self._exchange_symbols[bot_id] = {
            "exchange": exchange.upper(),
            "symbols": symbols,
        }

    def read_all_exchange_symbols(self) -> dict[str, set[str]]:
        result: dict[str, set[str]] = {}
        for _bot_id, data in self._exchange_symbols.items():
            exchange = data.get("exchange", "").upper()
            symbols = set(data.get("symbols", []))
            if exchange and symbols:
                if exchange in result:
                    result[exchange] |= symbols
                else:
                    result[exchange] = symbols
        return result

    # ---- Trade queue (written by monitor/signal_gen, read by bots) ---- #

    def write_trade_queue(self, queue: TradeQueue) -> None:
        queue.updated_at = datetime.now(UTC).isoformat()
        self._trade_queue = queue

    def read_trade_queue(self) -> TradeQueue:
        return self._trade_queue

    def write_bot_trade_queue(self, bot_id: str, queue: TradeQueue) -> None:
        queue.updated_at = datetime.now(UTC).isoformat()
        self._bot_queues[bot_id] = queue

    def read_bot_trade_queue(self, bot_id: str) -> TradeQueue:
        return self._bot_queues.get(bot_id, TradeQueue())

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
        """Process consumption and rejection reports from a bot.

        Proposals were already popped from the shared queue on read, so
        consumed_ids just need cleanup on the per-bot queue.  Rejections
        are recorded so the signal generator can avoid re-proposing the
        same symbol+strategy in a tight loop.

        If a rejected proposal targeted multiple bot styles, a fresh copy
        is re-inserted into the shared queue for the remaining styles
        (excluding the one that just rejected it).
        """
        if not consumed_ids and not rejected:
            return
        q = self._bot_queues.get(bot_id)
        if q is not None:
            for pid in consumed_ids:
                q.mark_consumed(pid)
            for pid, reason in rejected.items():
                q.mark_rejected(pid, reason)
            q.updated_at = datetime.now(UTC).isoformat()

        bot_status = self._bot_statuses.get(bot_id)
        rejecting_style = bot_status.bot_style if bot_status else ""

        for pid, reason in rejected.items():
            proposal = self._find_proposal_by_id(pid, q)
            if not proposal:
                continue

            # Record rejection for signal generator cooldowns
            rkey = f"{proposal.symbol}|{proposal.strategy}"
            existing = self._rejections.get(rkey)
            if existing:
                existing.reason = reason
                existing.timestamp = datetime.now(UTC)
                existing.count += 1
            else:
                self._rejections[rkey] = RejectionRecord(reason, datetime.now(UTC))

            # Re-insert for remaining styles if the proposal targeted multiple
            if rejecting_style and proposal.target_bot:
                styles = {t.strip() for t in proposal.target_bot.split(",") if t.strip()}
                remaining = styles - {rejecting_style}
                if remaining:
                    requeued = proposal.model_copy(
                        update={
                            "id": uuid.uuid4().hex[:12],
                            "target_bot": ",".join(sorted(remaining)),
                            "consumed": False,
                            "rejected": False,
                            "reject_reason": "",
                        }
                    )
                    self._trade_queue.add(requeued)
                    logger.info(
                        "Re-queued {} {} for remaining styles [{}] after {} rejected it",
                        requeued.symbol,
                        requeued.strategy,
                        requeued.target_bot,
                        rejecting_style,
                    )

    def _find_proposal_by_id(self, pid: str, bot_queue: TradeQueue | None = None) -> TradeProposal | None:
        """Look up a proposal by ID across all queues (shared + per-bot)."""
        for q in [self._trade_queue] + ([bot_queue] if bot_queue else []) + list(self._bot_queues.values()):
            for bucket in ("critical", "daily", "swing"):
                for p in getattr(q, bucket):
                    if p.id == pid:
                        return p
        return None

    def read_queue_for_bot_style(self, bot_style: str) -> TradeQueue:
        """Pop matching proposals from the shared queue for this bot style.

        Proposals are REMOVED from the shared queue on read — once a bot
        receives them, they're gone.  The bot will either execute or reject.
        If rejected, the signal generator can propose again later (as a new
        proposal) if conditions still warrant it.
        """
        popped = TradeQueue()
        for bucket_name in ("critical", "daily", "swing"):
            src: list = getattr(self._trade_queue, bucket_name)
            dest: list = getattr(popped, bucket_name)
            keep: list = []
            for p in src:
                if p.consumed or p.rejected or p.is_expired:
                    continue
                targets = {t.strip() for t in (p.target_bot or "").split(",") if t.strip()}
                if not targets or bot_style in targets:
                    dest.append(p)
                else:
                    keep.append(p)
            setattr(self._trade_queue, bucket_name, keep)
        popped.updated_at = self._trade_queue.updated_at
        self._trade_queue.updated_at = datetime.now(UTC).isoformat()
        return popped

    def get_rejection_history(self) -> dict[str, RejectionRecord]:
        """Return rejection records for signal generator to consult."""
        return self._rejections

    def purge_old_rejections(self, max_age_hours: int = 24) -> None:
        cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)
        self._rejections = {k: v for k, v in self._rejections.items() if v.timestamp > cutoff}
