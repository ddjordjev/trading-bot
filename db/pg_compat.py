from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row


def _qmark_to_percent(sql: str) -> str:
    # Existing codebase uses SQLite qmark placeholders. Translate to psycopg format.
    return sql.replace("?", "%s")


@dataclass
class PgCursorCompat:
    _cursor: psycopg.Cursor[Any]
    lastrowid: int | None = None

    @property
    def rowcount(self) -> int:
        return int(self._cursor.rowcount or 0)

    def fetchone(self) -> Any:
        return self._cursor.fetchone()

    def fetchall(self) -> list[Any]:
        return list(self._cursor.fetchall())


class PgConnCompat:
    """Compatibility wrapper exposing sqlite-like execute/executescript surface."""

    def __init__(self, dsn: str):
        self._conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> PgCursorCompat:
        sql_clean = sql.strip()
        lower = sql_clean.lower()
        rewritten = _qmark_to_percent(sql)
        cur = self._conn.cursor()
        try:
            # Emulate sqlite lastrowid behavior for insert statements.
            if lower.startswith("insert into") and "returning" not in lower:
                rewritten = rewritten.rstrip().rstrip(";") + " RETURNING id"
                cur.execute(rewritten, params)
                row = cur.fetchone()
                lastrowid = int(row["id"]) if row and "id" in row else None
                return PgCursorCompat(cur, lastrowid=lastrowid)
            cur.execute(rewritten, params)
            return PgCursorCompat(cur)
        except psycopg.errors.UniqueViolation as exc:
            raise sqlite3.IntegrityError(str(exc)) from exc
        except psycopg.OperationalError as exc:
            raise sqlite3.OperationalError(str(exc)) from exc
        except psycopg.Error as exc:
            raise sqlite3.DatabaseError(str(exc)) from exc

    def executemany(self, sql: str, params_seq: list[tuple[Any, ...]] | list[list[Any]]) -> PgCursorCompat:
        cur = self._conn.cursor()
        try:
            cur.executemany(_qmark_to_percent(sql), params_seq)
            return PgCursorCompat(cur)
        except psycopg.errors.UniqueViolation as exc:
            raise sqlite3.IntegrityError(str(exc)) from exc
        except psycopg.OperationalError as exc:
            raise sqlite3.OperationalError(str(exc)) from exc
        except psycopg.Error as exc:
            raise sqlite3.DatabaseError(str(exc)) from exc

    def executescript(self, script: str) -> None:
        statements = [s.strip() for s in script.split(";") if s.strip()]
        for stmt in statements:
            self.execute(stmt)

    def commit(self) -> None:
        # autocommit mode; kept for sqlite API parity
        return

    def rollback(self) -> None:
        # autocommit mode; kept for sqlite API parity
        return

    def close(self) -> None:
        self._conn.close()
