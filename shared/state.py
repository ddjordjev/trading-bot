from __future__ import annotations

import contextlib
import fcntl
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from loguru import logger
from pydantic import BaseModel

from shared.models import (
    AnalyticsSnapshot,
    BotDeploymentStatus,
    IntelSnapshot,
    TradeQueue,
)

T = TypeVar("T", bound=BaseModel)

DATA_DIR = Path("data")


class SharedState:
    """Atomic read/write of JSON state files for inter-process communication.

    All writes use write-to-temp-then-rename for crash safety.
    Reads return the model or a default if the file is missing/corrupt.
    """

    def __init__(self, data_dir: Path = DATA_DIR):
        self._data_dir = data_dir
        self._data_dir.mkdir(parents=True, exist_ok=True)

    # ---- Generic helpers ---- #

    def _write(self, path: Path, model: BaseModel) -> None:
        """Atomic write: temp file -> fsync -> rename."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        closed = False
        try:
            data = model.model_dump_json(indent=2)
            os.write(fd, data.encode())
            os.fsync(fd)
            os.close(fd)
            closed = True
            os.replace(tmp, str(path))
        except Exception:
            if not closed:
                with contextlib.suppress(OSError):
                    os.close(fd)
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def _read(self, path: Path, model_cls: type[T]) -> T | None:
        try:
            raw = path.read_text()
            return model_cls.model_validate_json(raw)
        except FileNotFoundError:
            return None
        except Exception as e:
            logger.debug("Failed to read {}: {}", path, e)
            return None

    # ---- Bot status (written by bot, read by monitor) ---- #

    def write_bot_status(self, status: BotDeploymentStatus) -> None:
        status.updated_at = datetime.now(UTC).isoformat()
        self._write(self._data_dir / "bot_status.json", status)

    def read_bot_status(self) -> BotDeploymentStatus:
        s = self._read(self._data_dir / "bot_status.json", BotDeploymentStatus)
        return s or BotDeploymentStatus()

    # ---- Intel state (written by monitor, read by bot) ---- #

    def write_intel(self, intel: IntelSnapshot) -> None:
        intel.updated_at = datetime.now(UTC).isoformat()
        self._write(self._data_dir / "intel_state.json", intel)

    def read_intel(self) -> IntelSnapshot:
        s = self._read(self._data_dir / "intel_state.json", IntelSnapshot)
        return s or IntelSnapshot()

    def intel_age_seconds(self) -> float:
        """How stale the intel data is."""
        path = self._data_dir / "intel_state.json"
        if not path.exists():
            return 999999
        raw = self._read(path, IntelSnapshot)
        if raw is None:
            return 999999
        if not raw.updated_at:
            return 999999
        try:
            ts = raw.updated_at.replace("Z", "+00:00")
            updated = datetime.fromisoformat(ts)
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=UTC)
            return (datetime.now(UTC) - updated).total_seconds()
        except Exception:
            return 999999

    # ---- Analytics state (written by analytics service, read by bot) ---- #

    def write_analytics(self, analytics: AnalyticsSnapshot) -> None:
        analytics.updated_at = datetime.now(UTC).isoformat()
        self._write(self._data_dir / "analytics_state.json", analytics)

    def read_analytics(self) -> AnalyticsSnapshot:
        s = self._read(self._data_dir / "analytics_state.json", AnalyticsSnapshot)
        return s or AnalyticsSnapshot()

    # ---- Trade queue (written by monitor, read+updated by bot) ---- #
    # Uses a file lock to prevent lost-update race between bot and monitor.

    def _queue_lock_path(self) -> Path:
        return self._data_dir / "trade_queue.lock"

    def write_trade_queue(self, queue: TradeQueue) -> None:
        queue.updated_at = datetime.now(UTC).isoformat()
        lock_path = self._queue_lock_path()
        lock_path.touch(exist_ok=True)
        with open(lock_path) as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                self._write(self._data_dir / "trade_queue.json", queue)
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)

    def read_trade_queue(self) -> TradeQueue:
        lock_path = self._queue_lock_path()
        lock_path.touch(exist_ok=True)
        with open(lock_path) as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_SH)
            try:
                q = self._read(self._data_dir / "trade_queue.json", TradeQueue)
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        return q or TradeQueue()
