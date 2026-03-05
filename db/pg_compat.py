from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row

from db import local_db as ldb
from db.errors import DBError, DBIntegrityError, DBOperationalError


def _qmark_to_percent(sql: str) -> str:
    # Existing codebase uses qmark placeholders. Translate to psycopg format.
    return sql.replace("?", "%s")


@dataclass
class PgCursorCompat:
    _cursor: psycopg.Cursor[Any]
    lastrowid: int | None = None

    @property
    def rowcount(self) -> int:
        return int(self._cursor.rowcount or 0)

    @property
    def description(self) -> Any:
        return getattr(self._cursor, "description", None)

    def fetchone(self) -> Any:
        return self._cursor.fetchone()

    def fetchall(self) -> list[Any]:
        return list(self._cursor.fetchall())


class PgConnCompat:
    """Compatibility wrapper exposing DB-API style execute/executescript surface."""

    def __init__(self, dsn: str, local_fallback_path: str | None = None):
        try:
            self._conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
            self._is_local_fallback = False
        except psycopg.OperationalError:
            # Unit tests run without networked Postgres; allow isolated local fallback there.
            if os.getenv("PYTEST_CURRENT_TEST"):
                self._conn = ldb.connect(local_fallback_path or ":memory:")
                self._conn.row_factory = ldb.Row
                self._is_local_fallback = True
            else:
                raise

    @property
    def is_local_fallback(self) -> bool:
        return bool(self._is_local_fallback)

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> PgCursorCompat:
        if self._is_local_fallback:
            cur = self._conn.execute(sql, params)
            return PgCursorCompat(cur, lastrowid=getattr(cur, "lastrowid", None))
        sql_clean = sql.strip()
        lower = sql_clean.lower()
        rewritten = _qmark_to_percent(sql)
        base_rewritten = rewritten
        cur = self._conn.cursor()
        try:
            # Emulate lastrowid behavior for insert statements.
            if lower.startswith("insert into") and "returning" not in lower:
                rewritten = rewritten.rstrip().rstrip(";") + " RETURNING id"
                try:
                    cur.execute(rewritten, params)
                    row = cur.fetchone()
                    lastrowid = int(row["id"]) if row and "id" in row else None
                    return PgCursorCompat(cur, lastrowid=lastrowid)
                except psycopg.errors.UndefinedColumn:
                    # Some UPSERT targets (e.g. bot_config/exchange_symbols) do not expose id.
                    # Re-run without synthetic RETURNING to preserve execute behavior.
                    cur.execute(base_rewritten, params)
                    return PgCursorCompat(cur, lastrowid=None)
            cur.execute(rewritten, params)
            return PgCursorCompat(cur)
        except psycopg.errors.UniqueViolation as exc:
            raise DBIntegrityError(str(exc)) from exc
        except psycopg.OperationalError as exc:
            raise DBOperationalError(str(exc)) from exc
        except psycopg.Error as exc:
            raise DBError(str(exc)) from exc

    def executemany(self, sql: str, params_seq: list[tuple[Any, ...]] | list[list[Any]]) -> PgCursorCompat:
        if self._is_local_fallback:
            cur = self._conn.cursor()
            cur.executemany(sql, params_seq)
            return PgCursorCompat(cur, lastrowid=getattr(cur, "lastrowid", None))
        cur = self._conn.cursor()
        try:
            cur.executemany(_qmark_to_percent(sql), params_seq)
            return PgCursorCompat(cur)
        except psycopg.errors.UniqueViolation as exc:
            raise DBIntegrityError(str(exc)) from exc
        except psycopg.OperationalError as exc:
            raise DBOperationalError(str(exc)) from exc
        except psycopg.Error as exc:
            raise DBError(str(exc)) from exc

    def executescript(self, script: str) -> None:
        statements = [s.strip() for s in script.split(";") if s.strip()]
        for stmt in statements:
            self.execute(stmt)

    def commit(self) -> None:
        # autocommit mode; kept for API parity
        return

    def rollback(self) -> None:
        # autocommit mode; kept for API parity
        return

    def close(self) -> None:
        self._conn.close()
