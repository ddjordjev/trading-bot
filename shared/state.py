from __future__ import annotations

import contextlib
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
        intel = self.read_intel()
        if not intel.updated_at:
            return 999999
        try:
            updated = datetime.fromisoformat(intel.updated_at)
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

    def write_trade_queue(self, queue: TradeQueue) -> None:
        queue.updated_at = datetime.now(UTC).isoformat()
        self._write(self._data_dir / "trade_queue.json", queue)

    def read_trade_queue(self) -> TradeQueue:
        q = self._read(self._data_dir / "trade_queue.json", TradeQueue)
        return q or TradeQueue()
