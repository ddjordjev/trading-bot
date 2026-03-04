from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

TABLES = [
    "trades",
    "bot_config",
    "exchange_symbols",
    "cex_binance_snapshots",
    "cex_binance_symbol_state",
    "openclaw_daily_reports",
    "openclaw_suggestions",
    "exchange_equity_snapshots",
    "analytics_snapshots",
]


def _sqlite_rows(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    return conn.execute(f"SELECT * FROM {table}").fetchall()


def _sqlite_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [str(r[1]) for r in rows]


def _pg_columns(conn: psycopg.Connection, table: str) -> set[str]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name=%s
            """,
            (table,),
        )
        return {str(r["column_name"]) for r in cur.fetchall()}


def _copy_table(sqlite_conn: sqlite3.Connection, pg_conn: psycopg.Connection, table: str) -> tuple[int, int]:
    src_cols = _sqlite_columns(sqlite_conn, table)
    dst_cols = _pg_columns(pg_conn, table)
    cols = [c for c in src_cols if c in dst_cols]
    if not cols:
        return 0, 0

    rows = _sqlite_rows(sqlite_conn, table)
    placeholders = ", ".join(["%s"] * len(cols))
    col_sql = ", ".join(cols)
    sql = f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"

    with pg_conn.cursor() as cur:
        if rows:
            cur.executemany(sql, [tuple(row[c] for c in cols) for row in rows])

    src_count = len(rows)
    with pg_conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        dst_count = int(cur.fetchone()[0])
    return src_count, dst_count


def main() -> None:
    sqlite_path = Path(os.getenv("SQLITE_HUB_DB_PATH", "data/hub.db"))
    dsn = os.getenv("HUB_POSTGRES_DSN", "postgresql://tradeborg:tradeborg@localhost:5438/trading_db")
    if not sqlite_path.exists():
        raise SystemExit(f"sqlite hub db not found: {sqlite_path}")

    sqlite_conn = sqlite3.connect(str(sqlite_path))
    sqlite_conn.row_factory = sqlite3.Row
    pg_conn = psycopg.connect(dsn, autocommit=True)
    try:
        print(f"migrating from sqlite={sqlite_path} -> postgres={dsn}")
        for table in TABLES:
            src, dst = _copy_table(sqlite_conn, pg_conn, table)
            print(f"{table}: source_rows={src} postgres_rows={dst}")
        print("migration completed")
    finally:
        sqlite_conn.close()
        pg_conn.close()


if __name__ == "__main__":
    main()
