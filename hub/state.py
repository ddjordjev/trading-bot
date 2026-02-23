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
    SignalPriority,
    TradeProposal,
    TradeQueue,
)

_ANALYTICS_PATH = Path("data/analytics_state.json")


class QueueOutcome:
    """Lightweight record kept for dashboard display and signal-generator cooldown."""

    __slots__ = ("action", "bot_id", "proposal_id", "reason", "strategy", "symbol", "timestamp")

    def __init__(
        self,
        proposal_id: str,
        symbol: str,
        strategy: str,
        action: str,
        bot_id: str,
        reason: str = "",
    ):
        self.proposal_id = proposal_id
        self.symbol = symbol
        self.strategy = strategy
        self.action = action
        self.bot_id = bot_id
        self.reason = reason
        self.timestamp = datetime.now(UTC)


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
        self._rejections: dict[str, RejectionRecord] = {}
        self._outcomes: list[QueueOutcome] = []
        self._outcomes_max = 100

        self._bot_statuses: dict[str, BotDeploymentStatus] = {}
        self._bot_positions: dict[str, tuple[str, set[str]]] = {}
        self._active_symbols_by_exchange: dict[str, set[str]] = {}

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

    def update_bot_positions(self, bot_id: str, exchange: str, symbols: set[str]) -> None:
        """Track which symbols a bot currently holds on a given exchange."""
        ex = exchange.upper()
        all_syms: set[str] = set()
        for bid, (bex, bsyms) in list(self._bot_positions.items()):
            if bid == bot_id:
                continue
            if bex == ex:
                all_syms |= bsyms
        self._bot_positions[bot_id] = (ex, symbols)
        all_syms |= symbols
        self._active_symbols_by_exchange[ex] = all_syms

    def get_active_symbols(self, exchange: str) -> set[str]:
        """Symbols currently held by any bot on this exchange."""
        return self._active_symbols_by_exchange.get(exchange.upper(), set())

    # ---- Trade queue ---- #

    def write_trade_queue(self, queue: TradeQueue) -> None:
        queue.updated_at = datetime.now(UTC).isoformat()
        self._trade_queue = queue

    def read_trade_queue(self) -> TradeQueue:
        return self._trade_queue

    def serve_proposal_to_bot(
        self,
        bot_style: str,
        bot_id: str,
        exchange: str,
        allowed_priorities: list[SignalPriority] | None = None,
        open_db_symbols: set[str] | None = None,
    ) -> TradeProposal | None:
        """Pick and lock the next matching proposal for a bot.

        Returns a copy of the proposal (for the bot) or None.
        The original stays in the queue with ``locked_until`` set to 300 s
        so no other bot can grab the same symbol while this one is executing.
        """
        self._trade_queue.unlock_expired()

        active = self.get_active_symbols(exchange) if exchange else set()

        picked = self._trade_queue.get_next_for_bot(
            exchange=exchange,
            bot_style=bot_style,
            allowed_priorities=allowed_priorities,
            active_symbols=active,
            open_db_symbols=open_db_symbols,
        )
        if not picked:
            return None

        self._trade_queue.lock_proposal(picked.id, seconds=300)
        logger.info(
            "Served {} {} to {} (locked 300s) | queue={} active={} db={}",
            picked.symbol,
            picked.strategy,
            bot_id,
            self._trade_queue.total,
            active,
            open_db_symbols or set(),
        )
        return picked.model_copy()

    def handle_consume(self, proposal_id: str, exchange: str, bot_id: str) -> None:
        """Bot confirmed it executed the trade — remove from queue.

        Immediately registers the symbol in ``active_symbols`` so that
        ``_route_to_bots`` and ``_queue_extreme_proposals`` won't re-add
        a proposal for this symbol before the bot's next report cycle
        propagates ``open_symbols``.
        """
        proposal = self._find_proposal(proposal_id)
        if proposal:
            self._outcomes.append(QueueOutcome(proposal_id, proposal.symbol, proposal.strategy, "consumed", bot_id))
            self._outcomes = self._outcomes[-self._outcomes_max :]

            if bot_id and exchange:
                ex = exchange.upper()
                _, existing_syms = self._bot_positions.get(bot_id, (ex, set()))
                self.update_bot_positions(bot_id, ex, existing_syms | {proposal.symbol})
                logger.info("Consumed {} by {} — symbol added to active_symbols[{}]", proposal.symbol, bot_id, ex)

            self._trade_queue.remove_proposal(proposal_id)

        self._trade_queue.updated_at = datetime.now(UTC).isoformat()

    def handle_reject(self, proposal_id: str, exchange: str, bot_id: str, reason: str = "") -> None:
        """Bot rejected the proposal — remove this exchange, record for cooldown."""
        proposal = self._find_proposal(proposal_id)
        if proposal:
            self._outcomes.append(
                QueueOutcome(proposal_id, proposal.symbol, proposal.strategy, "rejected", bot_id, reason)
            )
            self._outcomes = self._outcomes[-self._outcomes_max :]
            rkey = f"{proposal.symbol}|{proposal.strategy}"
            existing = self._rejections.get(rkey)
            if existing:
                existing.reason = reason
                existing.timestamp = datetime.now(UTC)
                existing.count += 1
            else:
                self._rejections[rkey] = RejectionRecord(reason, datetime.now(UTC))

        self._trade_queue.remove_exchange(proposal_id, exchange)
        self._trade_queue.updated_at = datetime.now(UTC).isoformat()

    def _find_proposal(self, proposal_id: str) -> TradeProposal | None:
        for p in self._trade_queue.proposals:
            if p.id == proposal_id:
                return p
        return None

    def read_recent_outcomes(self) -> list[QueueOutcome]:
        """For dashboard display — recent consumed/rejected outcomes."""
        cutoff = datetime.now(UTC) - timedelta(minutes=30)
        self._outcomes = [o for o in self._outcomes if o.timestamp > cutoff]
        return self._outcomes

    def get_rejection_history(self) -> dict[str, RejectionRecord]:
        return self._rejections

    def purge_old_rejections(self, max_age_hours: int = 24) -> None:
        cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)
        self._rejections = {k: v for k, v in self._rejections.items() if v.timestamp > cutoff}
