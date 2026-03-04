from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from loguru import logger

from db.hub_store import HubDB
from db.pg_compat import PgConnCompat

_POSTGRES_SCHEMA_FILE = Path("db/migrations/postgres/001_init.sql")


class PostgresHubDB(HubDB):
    """Postgres-backed HubDB adapter with sqlite-compatible execution API."""

    def __init__(self, dsn: str):
        super().__init__(path=Path("data/hub.db"))
        self._dsn = dsn

    def connect(self) -> None:
        self._conn = PgConnCompat(self._dsn)  # type: ignore[assignment]
        self._apply_schema()
        logger.info("PostgresHubDB connected")

    def _apply_schema(self) -> None:
        if not _POSTGRES_SCHEMA_FILE.exists():
            raise RuntimeError(f"missing postgres schema file: {_POSTGRES_SCHEMA_FILE}")
        script = _POSTGRES_SCHEMA_FILE.read_text()
        assert self._conn
        self._conn.executescript(script)

    def _ensure_bot_id_column(self) -> None:
        return

    def _ensure_request_key_column(self) -> None:
        return

    def _ensure_recovery_close_column(self) -> None:
        return

    def _execute_write_with_lock_retry(
        self,
        sql: str,
        params: tuple[Any, ...],
        *,
        retries: int = 3,
        base_sleep_seconds: float = 0.05,
    ) -> Any:
        assert self._conn
        last_exc: sqlite3.OperationalError | None = None
        for attempt in range(retries):
            try:
                cursor = self._conn.execute(sql, params)
                self._conn.commit()
                return cursor
            except sqlite3.OperationalError as exc:
                last_exc = exc
                msg = str(exc).lower()
                retryable = (
                    "could not serialize access" in msg
                    or "deadlock detected" in msg
                    or "database is locked" in msg
                    or "lock" in msg
                )
                if not retryable or attempt >= retries - 1:
                    raise
                sleep_for = base_sleep_seconds * float(attempt + 1)
                logger.warning(
                    "Postgres lock contention in HubDB write (attempt {}/{}), retrying in {:.2f}s",
                    attempt + 1,
                    retries,
                    sleep_for,
                )
                time.sleep(sleep_for)
        if last_exc is not None:
            raise last_exc
        raise sqlite3.OperationalError("PostgresHubDB write failed without exception")
