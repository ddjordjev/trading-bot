from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from loguru import logger

from db.hub_store import HubDB
from db.pg_compat import PgConnCompat

_POSTGRES_SCHEMA_FILE = Path("db/migrations/postgres/001_init.sql")


class PostgresHubDB(HubDB):
    """Postgres-backed HubDB adapter."""

    _did_log_connect_info = False

    def __init__(self, dsn: str, path: Path = Path("data/hub.db")):
        super().__init__(path=path)
        self._dsn = dsn

    def connect(self) -> None:
        try:
            self._conn = PgConnCompat(self._dsn, local_fallback_path=str(self._path))
        except TypeError:
            # Test doubles may still expose the previous constructor signature.
            self._conn = PgConnCompat(self._dsn)
        if getattr(self._conn, "is_local_fallback", False):
            self._create_tables()
            self._ensure_trade_columns()
            self._create_hub_tables()
        else:
            self._apply_schema()
            self._sync_serial_sequences()
            # Keep Postgres runtime aligned with hub invariants enforced by HubDB.
            self._cleanup_rows_missing_opened_at()
            self._cleanup_open_owner_conflicts()
            self._ensure_single_open_owner_index()
        if not PostgresHubDB._did_log_connect_info:
            logger.info("PostgresHubDB connected")
            PostgresHubDB._did_log_connect_info = True
        else:
            logger.debug("PostgresHubDB reconnected")

    def _apply_schema(self) -> None:
        if not _POSTGRES_SCHEMA_FILE.exists():
            raise RuntimeError(f"missing postgres schema file: {_POSTGRES_SCHEMA_FILE}")
        script = _POSTGRES_SCHEMA_FILE.read_text()
        assert self._conn
        self._conn.executescript(script)

    def _sync_serial_sequences(self) -> None:
        """Keep serial sequences aligned to avoid duplicate key inserts on id PKs."""
        assert self._conn
        tables_with_serial_id = [
            "trades",
            "openclaw_daily_reports",
            "openclaw_suggestions",
            "exchange_equity_snapshots",
            "analytics_snapshots",
            "swing_plans",
            "swing_plan_entries",
        ]
        for table in tables_with_serial_id:
            try:
                self._conn.execute(
                    f"""
                    SELECT setval(
                        pg_get_serial_sequence('{table}', 'id'),
                        COALESCE((SELECT MAX(id) FROM {table}), 1),
                        COALESCE((SELECT MAX(id) FROM {table}), 0) > 0
                    )
                    """,
                    (),
                )
            except Exception as exc:
                logger.warning("Sequence sync skipped for {}: {}", table, exc)

    def _ensure_bot_id_column(self) -> None:
        assert self._conn
        if getattr(self._conn, "is_local_fallback", False):
            super()._ensure_bot_id_column()

    def _ensure_request_key_column(self) -> None:
        assert self._conn
        if getattr(self._conn, "is_local_fallback", False):
            super()._ensure_request_key_column()

    def _ensure_recovery_close_column(self) -> None:
        assert self._conn
        if getattr(self._conn, "is_local_fallback", False):
            super()._ensure_recovery_close_column()

    def _execute_write_with_lock_retry(
        self,
        sql: str,
        params: tuple[Any, ...],
        *,
        retries: int = 3,
        base_sleep_seconds: float = 0.05,
    ) -> Any:
        assert self._conn
        last_exc: Exception | None = None
        for attempt in range(retries):
            try:
                cursor = self._conn.execute(sql, params)
                self._conn.commit()
                return cursor
            except Exception as exc:
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
        raise RuntimeError("PostgresHubDB write failed without exception")
