"""Hub-side database: receives trade records and deposit events pushed by bots.

This DB lives only on the hub (dashboard) container at ``data/hub.db``.
Trading bots never touch it directly — they push events via HTTP and
query open positions / stats via hub API endpoints.

Extends TradeDB with ``bot_id``, ``request_key`` (idempotency),
``recovery_close`` columns on trades, and a ``deposits`` table.
"""

from __future__ import annotations

import json
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger

from db import local_db as ldb
from db.errors import DBIntegrityError
from db.models import TradeRecord
from db.store import TradeDB
from shared.models import AnalyticsSnapshot
from shared.runtime_tuning import normalize_runtime_tuning, runtime_tuning_revision

_HUB_DB_DEFAULT = Path("data/hub.db")


class HubDB(TradeDB):
    """TradeDB extended with hub-specific tables and bot-centric queries."""

    _REQUIRED_FIELDS_BY_ACTION: dict[str, tuple[str, ...]] = {
        "open": ("bot_id", "opened_at"),
        "close": ("bot_id", "opened_at"),
        "update": ("bot_id", "opened_at"),
        "cancel_reservation": ("bot_id", "opened_at"),
    }

    def __init__(self, path: Path = _HUB_DB_DEFAULT):
        super().__init__(path=path)
        self._ack_buffer: dict[str, set[str]] = {}
        self._runtime_tuning_rows: dict[tuple[str, str], Any] = {}
        self._runtime_tuning_effective_cache: dict[str, tuple[dict[str, Any], str]] = {}
        self._runtime_tuning_loaded: bool = False

    def connect(self) -> None:
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                self._conn = ldb.connect(str(self._path))
                self._conn.row_factory = ldb.Row
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA busy_timeout=30000")
                # Validate DB before creating tables (catches malformed/not-a-db early)
                self._conn.execute("PRAGMA quick_check").fetchone()
                break
            except Exception as e:
                last_exc = e
                with suppress(Exception):
                    if self._conn is not None:
                        self._conn.close()
                self._conn = None
                msg = str(e).lower()
                retryable = "database is locked" in msg or "database table is locked" in msg or "disk i/o error" in msg
                if retryable and attempt < 2:
                    time.sleep(0.2 * float(attempt + 1))
                    continue
                if "not a database" in msg or "malformed" in msg:
                    logger.error(
                        "hub.db is corrupted. Run: ./scripts/recover_hub_postgres.sh and restore from Postgres backup"
                    )
                raise
        if self._conn is None and last_exc is not None:
            raise last_exc
        self._create_tables()
        self._ensure_trade_columns()
        self._create_hub_tables()
        logger.info("HubDB connected: {}", self._path)

    def _create_hub_tables(self) -> None:
        assert self._conn
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS bot_config (
                bot_id TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS exchange_symbols (
                exchange TEXT PRIMARY KEY,
                symbols TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runtime_tuning (
                bot_id TEXT NOT NULL CHECK(bot_id <> ''),
                key TEXT NOT NULL CHECK(key <> ''),
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (bot_id, key)
            );

            CREATE TABLE IF NOT EXISTS cex_binance_snapshots (
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                price REAL NOT NULL,
                quote_volume REAL NOT NULL,
                change_24h REAL NOT NULL,
                funding_rate REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (timestamp, symbol)
            );

            CREATE TABLE IF NOT EXISTS cex_binance_symbol_state (
                symbol TEXT PRIMARY KEY,
                updated_at TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                sample_count INTEGER NOT NULL DEFAULT 0,
                last_price REAL NOT NULL DEFAULT 0,
                last_quote_volume REAL NOT NULL DEFAULT 0,
                last_change_24h REAL NOT NULL DEFAULT 0,
                last_funding_rate REAL NOT NULL DEFAULT 0,
                avg_quote_volume REAL NOT NULL DEFAULT 0,
                vol_accel REAL NOT NULL DEFAULT 0,
                confidence REAL NOT NULL DEFAULT 0,
                score REAL NOT NULL DEFAULT 0,
                chg_1m REAL NOT NULL DEFAULT 0,
                chg_5m REAL NOT NULL DEFAULT 0,
                chg_1h REAL NOT NULL DEFAULT 0,
                chg_4h REAL NOT NULL DEFAULT 0,
                chg_1d REAL NOT NULL DEFAULT 0,
                chg_1w REAL NOT NULL DEFAULT 0,
                chg_3w REAL NOT NULL DEFAULT 0,
                chg_1mo REAL NOT NULL DEFAULT 0,
                chg_3mo REAL NOT NULL DEFAULT 0,
                chg_1y REAL NOT NULL DEFAULT 0,
                anchor_1m_ts TEXT NOT NULL DEFAULT '',
                anchor_5m_ts TEXT NOT NULL DEFAULT '',
                anchor_1h_ts TEXT NOT NULL DEFAULT '',
                anchor_4h_ts TEXT NOT NULL DEFAULT '',
                anchor_1d_ts TEXT NOT NULL DEFAULT '',
                anchor_1w_ts TEXT NOT NULL DEFAULT '',
                anchor_3w_ts TEXT NOT NULL DEFAULT '',
                anchor_1mo_ts TEXT NOT NULL DEFAULT '',
                anchor_3mo_ts TEXT NOT NULL DEFAULT '',
                anchor_1y_ts TEXT NOT NULL DEFAULT '',
                anchor_1m_price REAL NOT NULL DEFAULT 0,
                anchor_5m_price REAL NOT NULL DEFAULT 0,
                anchor_1h_price REAL NOT NULL DEFAULT 0,
                anchor_4h_price REAL NOT NULL DEFAULT 0,
                anchor_1d_price REAL NOT NULL DEFAULT 0,
                anchor_1w_price REAL NOT NULL DEFAULT 0,
                anchor_3w_price REAL NOT NULL DEFAULT 0,
                anchor_1mo_price REAL NOT NULL DEFAULT 0,
                anchor_3mo_price REAL NOT NULL DEFAULT 0,
                anchor_1y_price REAL NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_cex_binance_ts
                ON cex_binance_snapshots(timestamp);
            CREATE INDEX IF NOT EXISTS idx_cex_binance_symbol_ts
                ON cex_binance_snapshots(symbol, timestamp);
            CREATE INDEX IF NOT EXISTS idx_cex_binance_state_updated_at
                ON cex_binance_symbol_state(updated_at);

            CREATE TABLE IF NOT EXISTS openclaw_daily_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_day TEXT NOT NULL,
                run_kind TEXT NOT NULL DEFAULT 'scheduled',
                requested_at TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                lane_used TEXT NOT NULL DEFAULT 'fallback',
                status TEXT NOT NULL DEFAULT 'ok',
                source_url TEXT NOT NULL DEFAULT '',
                context_json TEXT NOT NULL DEFAULT '{}',
                response_json TEXT NOT NULL DEFAULT '{}',
                error_text TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS openclaw_suggestions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                suggestion_key TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL DEFAULT 'openclaw',
                status TEXT NOT NULL DEFAULT 'new',
                suggestion_type TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                strategy TEXT NOT NULL DEFAULT '',
                symbol TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 0,
                current_value TEXT NOT NULL DEFAULT '',
                suggested_value TEXT NOT NULL DEFAULT '',
                expected_improvement TEXT NOT NULL DEFAULT '',
                based_on_trades INTEGER NOT NULL DEFAULT 0,
                notes TEXT NOT NULL DEFAULT '',
                first_seen_report_id INTEGER NOT NULL DEFAULT 0,
                last_seen_report_id INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                implemented_at TEXT NOT NULL DEFAULT '',
                removed_at TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_openclaw_reports_completed
                ON openclaw_daily_reports(completed_at);
            CREATE INDEX IF NOT EXISTS idx_openclaw_suggestions_status
                ON openclaw_suggestions(status, updated_at);

            CREATE TABLE IF NOT EXISTS exchange_equity_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                exchange TEXT NOT NULL,
                available_usdt REAL NOT NULL DEFAULT 0,
                estimated_equity_usdt REAL NOT NULL DEFAULT 0,
                open_positions INTEGER NOT NULL DEFAULT 0,
                source_bot TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'bot_report',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_exchange_equity_snapshots_ex_ts
                ON exchange_equity_snapshots(exchange, created_at);

            CREATE TABLE IF NOT EXISTS analytics_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_json TEXT NOT NULL DEFAULT '{}',
                total_trades_logged INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_analytics_snapshots_updated_at
                ON analytics_snapshots(updated_at);

        """)
        self._ensure_bot_id_column()
        self._ensure_request_key_column()
        self._ensure_recovery_close_column()
        self._ensure_swing_plan_columns()
        self._ensure_runtime_tuning_table_strict()
        self._cleanup_rows_missing_opened_at()
        self._cleanup_open_owner_conflicts()
        self._ensure_single_open_owner_index()

    def _ensure_swing_plan_columns(self) -> None:
        assert self._conn
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS swing_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id TEXT NOT NULL UNIQUE CHECK(plan_id <> ''),
                bot_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'swing_auto',
                exchange TEXT NOT NULL DEFAULT '',
                direction TEXT NOT NULL DEFAULT '',
                first_entry_price REAL NOT NULL DEFAULT 0,
                last_entry_price REAL NOT NULL DEFAULT 0,
                grid_count INTEGER NOT NULL DEFAULT 0,
                leverage INTEGER NOT NULL DEFAULT 1,
                margin_amount REAL NOT NULL DEFAULT 0,
                max_concurrent_limit_orders_on_cex INTEGER NOT NULL DEFAULT 3,
                plan_state TEXT NOT NULL DEFAULT 'active',
                opened_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_swing_plans_lookup
                ON swing_plans(bot_id, symbol, plan_id);
            CREATE INDEX IF NOT EXISTS idx_swing_plans_mode_state
                ON swing_plans(bot_id, mode, exchange, plan_state);

            CREATE TABLE IF NOT EXISTS swing_plan_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id TEXT NOT NULL CHECK(plan_id <> ''),
                entry_idx INTEGER NOT NULL DEFAULT 0,
                side TEXT NOT NULL DEFAULT '',
                price REAL NOT NULL DEFAULT 0,
                amount REAL NOT NULL DEFAULT 0,
                leverage INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'planned',
                order_id TEXT NOT NULL DEFAULT '',
                strategy TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(plan_id, entry_idx)
            );
            CREATE INDEX IF NOT EXISTS idx_swing_plan_entries_plan
                ON swing_plan_entries(plan_id, entry_idx);

            DROP TABLE IF EXISTS swing_entry_plans;
        """)
        self._conn.commit()

    def _ensure_runtime_tuning_table_strict(self) -> None:
        """Enforce non-null/no-default key columns for runtime_tuning."""
        assert self._conn
        info_rows = self._conn.execute("PRAGMA table_info(runtime_tuning)").fetchall()
        if not info_rows:
            return
        by_name = {str(r[1]): r for r in info_rows}
        bot_col = by_name.get("bot_id")
        key_col = by_name.get("key")
        val_col = by_name.get("value_json")
        if bot_col is None or key_col is None or val_col is None:
            return
        needs_rebuild = False
        for col in (bot_col, key_col, val_col):
            if int(col[3] or 0) == 0:
                needs_rebuild = True
                break
        if bot_col[4] is not None or val_col[4] is not None:
            needs_rebuild = True
        if not needs_rebuild:
            return
        self._conn.executescript("""
            BEGIN;
            DROP TABLE IF EXISTS runtime_tuning_new;
            CREATE TABLE runtime_tuning_new (
                bot_id TEXT NOT NULL CHECK(bot_id <> ''),
                key TEXT NOT NULL CHECK(key <> ''),
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (bot_id, key)
            );
            INSERT INTO runtime_tuning_new (bot_id, key, value_json, updated_at)
            SELECT
                COALESCE(NULLIF(bot_id, ''), '*') AS bot_id,
                key,
                COALESCE(value_json, 'null') AS value_json,
                updated_at
            FROM runtime_tuning
            WHERE COALESCE(key, '') <> '';
            DROP TABLE runtime_tuning;
            ALTER TABLE runtime_tuning_new RENAME TO runtime_tuning;
            COMMIT;
        """)

    def _ensure_bot_id_column(self) -> None:
        assert self._conn
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(trades)").fetchall()}
        if "bot_id" not in cols:
            self._conn.execute("ALTER TABLE trades ADD COLUMN bot_id TEXT DEFAULT ''")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_hub_bot ON trades(bot_id)")
            self._conn.commit()

    def _ensure_request_key_column(self) -> None:
        assert self._conn
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(trades)").fetchall()}
        if "request_key" not in cols:
            self._conn.execute("ALTER TABLE trades ADD COLUMN request_key TEXT DEFAULT ''")
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_hub_reqkey ON trades(request_key) WHERE request_key != ''"
        )
        self._conn.commit()

    def _ensure_recovery_close_column(self) -> None:
        assert self._conn
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(trades)").fetchall()}
        if "recovery_close" not in cols:
            self._conn.execute("ALTER TABLE trades ADD COLUMN recovery_close INTEGER DEFAULT 0")
            self._conn.commit()

    def _cleanup_open_owner_conflicts(self) -> None:
        """Keep a single open owner row per symbol (newest row wins)."""
        assert self._conn
        now_iso = datetime.now(UTC).isoformat()
        self._conn.execute(
            """
            WITH ranked AS (
                SELECT id, ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY id DESC) AS rn
                FROM trades
                WHERE closed_at='' AND symbol!=''
            )
            UPDATE trades
            SET closed_at=?,
                close_source='ownership_conflict_cleanup',
                close_reason='duplicate_owner_cleanup',
                recovery_close=1
            WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
            """,
            (now_iso,),
        )
        self._conn.commit()

    def _ensure_single_open_owner_index(self) -> None:
        """Enforce one open ownership row per symbol."""
        assert self._conn
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_open_owner_symbol ON trades(symbol) "
            "WHERE closed_at='' AND symbol!=''",
            (),
        )
        self._conn.commit()

    def _cleanup_rows_missing_opened_at(self) -> None:
        """Delete legacy rows that violate opened_at identity invariant."""
        assert self._conn
        cursor = self._conn.execute("DELETE FROM trades WHERE TRIM(COALESCE(opened_at, '')) = ''", ())
        deleted = int(cursor.rowcount or 0)
        if deleted > 0:
            logger.warning("Deleted {} legacy trade row(s) missing opened_at", deleted)
            self._conn.commit()

    def _require_trade_fields(self, action: str, values: dict[str, Any]) -> None:
        """Reject writes missing required fields for the target action."""
        action_l = str(action or "").strip().lower()
        required = self._REQUIRED_FIELDS_BY_ACTION.get(action_l, ())
        for field in required:
            raw = values.get(field)
            if str(raw or "").strip():
                continue
            raise DBIntegrityError(f"missing_{field}:{action_l}")

    # ---- Trade ingestion (hub-specific) ----

    def _latest_open_row_id(self, bot_id: str, opened_at: str, symbol: str = "") -> int | None:
        """Return the newest unclosed trade row for bot ownership tuple."""
        assert self._conn
        if symbol:
            row = self._conn.execute(
                """
                SELECT id
                FROM trades
                WHERE bot_id=? AND opened_at=? AND symbol=? AND closed_at=''
                ORDER BY id DESC
                LIMIT 1
                """,
                (bot_id, opened_at, symbol),
            ).fetchone()
        else:
            row = self._conn.execute(
                """
                SELECT id
                FROM trades
                WHERE bot_id=? AND opened_at=? AND closed_at=''
                ORDER BY id DESC
                LIMIT 1
                """,
                (bot_id, opened_at),
            ).fetchone()
        return int(row["id"]) if row else None

    def _latest_open_row_id_by_symbol(self, bot_id: str, symbol: str) -> int | None:
        """Return newest unclosed trade row for bot+symbol ownership."""
        assert self._conn
        row = self._conn.execute(
            """
            SELECT id
            FROM trades
            WHERE bot_id=? AND symbol=? AND closed_at=''
            ORDER BY id DESC
            LIMIT 1
            """,
            (bot_id, symbol),
        ).fetchone()
        return int(row["id"]) if row else None

    @staticmethod
    def _patch_value(data: dict[str, Any], key: str) -> Any:
        """Return incoming patch value only when explicitly provided."""
        return data.get(key)

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        """Safely coerce optional flag-like payload values to int."""
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return int(value)

    def _execute_write_with_lock_retry(
        self,
        sql: str,
        params: tuple[Any, ...],
        *,
        retries: int = 3,
        base_sleep_seconds: float = 0.05,
    ) -> Any:
        """Run a write query with rollback + retry on transient lock contention."""
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
                if "database is locked" not in msg and "database table is locked" not in msg:
                    with suppress(Exception):
                        self._conn.rollback()
                    raise
                with suppress(Exception):
                    self._conn.rollback()
                if attempt >= retries - 1:
                    raise
                sleep_for = base_sleep_seconds * float(attempt + 1)
                logger.warning(
                    "DB lock contention in HubDB write (attempt {}/{}), retrying in {:.2f}s",
                    attempt + 1,
                    retries,
                    sleep_for,
                )
                time.sleep(sleep_for)
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("HubDB write failed without exception")

    def insert_trade(self, bot_id: str, trade: dict[str, Any], request_key: str = "") -> int:
        """Insert a trade record pushed by a bot. Deduplicates by request_key."""
        assert self._conn
        if request_key:
            existing = self._conn.execute("SELECT id FROM trades WHERE request_key = ?", (request_key,)).fetchone()
            if existing:
                self._mark_confirmed(bot_id, request_key)
                return int(existing["id"])
        symbol = str(trade.get("symbol", "") or "").strip()
        action = str(trade.get("action", "") or "").strip().lower()
        opened_at = str(trade.get("opened_at", "") or "").strip()
        self._require_trade_fields(
            action,
            {
                "bot_id": bot_id,
                "opened_at": opened_at,
            },
        )
        if action == "open" and not symbol:
            raise DBIntegrityError("missing_symbol:open")
        closed_at = str(trade.get("closed_at", "") or "").strip()
        if action == "open" and symbol and not closed_at:
            existing_open = self._conn.execute(
                "SELECT id, bot_id FROM trades WHERE symbol=? AND closed_at='' ORDER BY id DESC LIMIT 1",
                (symbol,),
            ).fetchone()
            if existing_open:
                existing_owner = str(existing_open["bot_id"] or "")
                if existing_owner != bot_id:
                    if request_key:
                        self._mark_confirmed(bot_id, request_key)
                    raise DBIntegrityError(f"open_owner_conflict:{symbol}:{existing_owner}")
                if request_key:
                    self._mark_confirmed(bot_id, request_key)
                return int(existing_open["id"])

        try:
            cursor = self._execute_write_with_lock_retry(
                """INSERT INTO trades (
                bot_id, symbol, side, strategy, action, scale_mode,
                entry_price, exit_price, amount, leverage,
                pnl_usd, pnl_pct, is_winner, hold_minutes,
                was_quick_trade, was_low_liquidity, dca_count, max_drawdown_pct,
                market_regime, fear_greed, daily_tier, daily_pnl_at_entry,
                signal_strength, hour_utc, day_of_week, volatility_pct,
                opened_at, closed_at,
                planned_stop_loss, planned_tp1, planned_tp2,
                exchange_stop_loss, exchange_take_profit,
                bot_stop_loss, bot_take_profit,
                effective_stop_loss, effective_take_profit,
                stop_source, tp_source, close_source, close_reason,
                exchange_close_order_id, exchange_close_trade_id, close_detected_at,
                request_key, recovery_close
            ) VALUES (
                ?,?,?,?,?,?,?,?,?,?,
                ?,?,?,?,?,?,?,?,?,?,
                ?,?,?,?,?,?,?,?,?,?,
                ?,?,?,?,?,?,?,?,?,?,
                ?,?,?,?,?,?
            )""",
                (
                    bot_id,
                    trade.get("symbol", ""),
                    trade.get("side", ""),
                    trade.get("strategy", ""),
                    trade.get("action", ""),
                    trade.get("scale_mode", ""),
                    trade.get("entry_price", 0),
                    trade.get("exit_price", 0),
                    trade.get("amount", 0),
                    trade.get("leverage", 1),
                    trade.get("pnl_usd", 0),
                    trade.get("pnl_pct", 0),
                    int(trade.get("is_winner", False)),
                    trade.get("hold_minutes", 0),
                    int(trade.get("was_quick_trade", False)),
                    int(trade.get("was_low_liquidity", False)),
                    trade.get("dca_count", 0),
                    trade.get("max_drawdown_pct", 0),
                    trade.get("market_regime", ""),
                    trade.get("fear_greed", 50),
                    trade.get("daily_tier", ""),
                    trade.get("daily_pnl_at_entry", 0),
                    trade.get("signal_strength", 0),
                    trade.get("hour_utc", 0),
                    trade.get("day_of_week", 0),
                    trade.get("volatility_pct", 0),
                    trade.get("opened_at", ""),
                    trade.get("closed_at", ""),
                    trade.get("planned_stop_loss", 0),
                    trade.get("planned_tp1", 0),
                    trade.get("planned_tp2", 0),
                    trade.get("exchange_stop_loss", 0),
                    trade.get("exchange_take_profit", 0),
                    trade.get("bot_stop_loss", 0),
                    trade.get("bot_take_profit", 0),
                    trade.get("effective_stop_loss", 0),
                    trade.get("effective_take_profit", 0),
                    trade.get("stop_source", "none"),
                    trade.get("tp_source", "none"),
                    trade.get("close_source", ""),
                    trade.get("close_reason", ""),
                    trade.get("exchange_close_order_id", ""),
                    trade.get("exchange_close_trade_id", ""),
                    trade.get("close_detected_at", ""),
                    request_key,
                    int(trade.get("recovery_close", False)),
                ),
            )
        except (DBIntegrityError, ldb.IntegrityError):
            # Idempotent race: another request committed this key first.
            if request_key:
                existing = self._conn.execute("SELECT id FROM trades WHERE request_key = ?", (request_key,)).fetchone()
                if existing:
                    self._mark_confirmed(bot_id, request_key)
                    return int(existing["id"])
            raise
        row_id = cursor.lastrowid or 0
        if request_key:
            self._mark_confirmed(bot_id, request_key)
        return row_id

    def update_trade_open(self, bot_id: str, opened_at: str, data: dict[str, Any], request_key: str = "") -> bool:
        """Upgrade an existing reservation/open row with actual fill details."""
        assert self._conn
        self._require_trade_fields(
            "open",
            {
                "bot_id": bot_id,
                "opened_at": opened_at,
            },
        )
        if request_key:
            existing = self._conn.execute("SELECT id FROM trades WHERE request_key = ?", (request_key,)).fetchone()
            if existing:
                self._mark_confirmed(bot_id, request_key)
                return True

        row_id = self._latest_open_row_id(bot_id, opened_at, str(data.get("symbol", "") or ""))
        if row_id is None:
            row_id = self._latest_open_row_id(bot_id, opened_at)
        if row_id is None:
            return False

        try:
            cursor = self._execute_write_with_lock_retry(
                """UPDATE trades SET
                action='open',
                symbol=COALESCE(NULLIF(?, ''), symbol),
                side=COALESCE(NULLIF(?, ''), side),
                strategy=COALESCE(NULLIF(?, ''), strategy),
                scale_mode=COALESCE(NULLIF(?, ''), scale_mode),
                entry_price=COALESCE(?, entry_price),
                amount=COALESCE(?, amount),
                leverage=COALESCE(?, leverage),
                market_regime=COALESCE(NULLIF(?, ''), market_regime),
                fear_greed=COALESCE(?, fear_greed),
                daily_tier=COALESCE(NULLIF(?, ''), daily_tier),
                daily_pnl_at_entry=COALESCE(?, daily_pnl_at_entry),
                signal_strength=COALESCE(?, signal_strength),
                hour_utc=COALESCE(?, hour_utc),
                day_of_week=COALESCE(?, day_of_week),
                volatility_pct=COALESCE(?, volatility_pct),
                planned_stop_loss=COALESCE(?, planned_stop_loss),
                planned_tp1=COALESCE(?, planned_tp1),
                planned_tp2=COALESCE(?, planned_tp2),
                exchange_stop_loss=COALESCE(?, exchange_stop_loss),
                exchange_take_profit=COALESCE(?, exchange_take_profit),
                bot_stop_loss=COALESCE(?, bot_stop_loss),
                bot_take_profit=COALESCE(?, bot_take_profit),
                effective_stop_loss=COALESCE(?, effective_stop_loss),
                effective_take_profit=COALESCE(?, effective_take_profit),
                stop_source=COALESCE(NULLIF(?, ''), stop_source),
                tp_source=COALESCE(NULLIF(?, ''), tp_source),
                was_quick_trade=COALESCE(?, was_quick_trade),
                was_low_liquidity=COALESCE(?, was_low_liquidity),
                dca_count=COALESCE(?, dca_count),
                max_drawdown_pct=COALESCE(?, max_drawdown_pct),
                request_key=CASE WHEN ?='' THEN request_key ELSE ? END
            WHERE id=?""",
                (
                    self._patch_value(data, "symbol"),
                    self._patch_value(data, "side"),
                    self._patch_value(data, "strategy"),
                    self._patch_value(data, "scale_mode"),
                    self._patch_value(data, "entry_price"),
                    self._patch_value(data, "amount"),
                    self._patch_value(data, "leverage"),
                    self._patch_value(data, "market_regime"),
                    self._patch_value(data, "fear_greed"),
                    self._patch_value(data, "daily_tier"),
                    self._patch_value(data, "daily_pnl_at_entry"),
                    self._patch_value(data, "signal_strength"),
                    self._patch_value(data, "hour_utc"),
                    self._patch_value(data, "day_of_week"),
                    self._patch_value(data, "volatility_pct"),
                    self._patch_value(data, "planned_stop_loss"),
                    self._patch_value(data, "planned_tp1"),
                    self._patch_value(data, "planned_tp2"),
                    self._patch_value(data, "exchange_stop_loss"),
                    self._patch_value(data, "exchange_take_profit"),
                    self._patch_value(data, "bot_stop_loss"),
                    self._patch_value(data, "bot_take_profit"),
                    self._patch_value(data, "effective_stop_loss"),
                    self._patch_value(data, "effective_take_profit"),
                    self._patch_value(data, "stop_source"),
                    self._patch_value(data, "tp_source"),
                    self._optional_int(data.get("was_quick_trade")),
                    self._optional_int(data.get("was_low_liquidity")),
                    self._patch_value(data, "dca_count"),
                    self._patch_value(data, "max_drawdown_pct"),
                    request_key,
                    request_key,
                    row_id,
                ),
            )
        except (DBIntegrityError, ldb.IntegrityError):
            if request_key:
                existing = self._conn.execute("SELECT id FROM trades WHERE request_key = ?", (request_key,)).fetchone()
                if existing:
                    self._mark_confirmed(bot_id, request_key)
                    return True
            raise
        updated = cursor.rowcount > 0
        if updated and request_key:
            self._mark_confirmed(bot_id, request_key)
        return bool(updated)

    def update_trade_close(self, bot_id: str, opened_at: str, data: dict[str, Any], request_key: str = "") -> bool:
        """Update an open trade row with exit data (matched by bot_id + opened_at)."""
        assert self._conn
        self._require_trade_fields("close", {"bot_id": bot_id, "opened_at": opened_at})
        if request_key:
            existing = self._conn.execute("SELECT id FROM trades WHERE request_key = ?", (request_key,)).fetchone()
            if existing:
                self._mark_confirmed(bot_id, request_key)
                return True

        row_id = self._latest_open_row_id(bot_id, opened_at, str(data.get("symbol", "") or ""))
        if row_id is None:
            row_id = self._latest_open_row_id(bot_id, opened_at)
        if row_id is None:
            return False

        try:
            cursor = self._execute_write_with_lock_retry(
                """UPDATE trades SET
                action='close',
                exit_price=COALESCE(?, exit_price),
                amount=COALESCE(?, amount),
                leverage=COALESCE(?, leverage),
                pnl_usd=COALESCE(?, pnl_usd),
                pnl_pct=COALESCE(?, pnl_pct),
                is_winner=COALESCE(?, is_winner),
                hold_minutes=COALESCE(?, hold_minutes),
                dca_count=COALESCE(?, dca_count),
                max_drawdown_pct=COALESCE(?, max_drawdown_pct),
                closed_at=COALESCE(NULLIF(?, ''), closed_at),
                effective_stop_loss=COALESCE(?, effective_stop_loss),
                effective_take_profit=COALESCE(?, effective_take_profit),
                stop_source=COALESCE(NULLIF(?, ''), stop_source),
                tp_source=COALESCE(NULLIF(?, ''), tp_source),
                close_source=COALESCE(NULLIF(?, ''), close_source),
                close_reason=COALESCE(NULLIF(?, ''), close_reason),
                exchange_close_order_id=COALESCE(NULLIF(?, ''), exchange_close_order_id),
                exchange_close_trade_id=COALESCE(NULLIF(?, ''), exchange_close_trade_id),
                close_detected_at=COALESCE(NULLIF(?, ''), close_detected_at),
                request_key=CASE WHEN ?='' THEN request_key ELSE ? END
            WHERE id=?""",
                (
                    self._patch_value(data, "exit_price"),
                    self._patch_value(data, "amount"),
                    self._patch_value(data, "leverage"),
                    self._patch_value(data, "pnl_usd"),
                    self._patch_value(data, "pnl_pct"),
                    self._optional_int(data.get("is_winner")),
                    self._patch_value(data, "hold_minutes"),
                    self._patch_value(data, "dca_count"),
                    self._patch_value(data, "max_drawdown_pct"),
                    self._patch_value(data, "closed_at"),
                    self._patch_value(data, "effective_stop_loss"),
                    self._patch_value(data, "effective_take_profit"),
                    self._patch_value(data, "stop_source"),
                    self._patch_value(data, "tp_source"),
                    self._patch_value(data, "close_source"),
                    self._patch_value(data, "close_reason"),
                    self._patch_value(data, "exchange_close_order_id"),
                    self._patch_value(data, "exchange_close_trade_id"),
                    self._patch_value(data, "close_detected_at"),
                    request_key,
                    request_key,
                    row_id,
                ),
            )
        except (DBIntegrityError, ldb.IntegrityError):
            if request_key:
                existing = self._conn.execute("SELECT id FROM trades WHERE request_key = ?", (request_key,)).fetchone()
                if existing:
                    self._mark_confirmed(bot_id, request_key)
                    return True
            raise
        updated = cursor.rowcount > 0
        if updated:
            self._clear_swing_plan_after_trade_close(
                bot_id=bot_id,
                opened_at=opened_at,
                symbol_hint=str(data.get("symbol", "") or ""),
            )
        if updated and request_key:
            self._mark_confirmed(bot_id, request_key)
        return bool(updated)

    def update_trade_runtime(self, bot_id: str, opened_at: str, data: dict[str, Any], request_key: str = "") -> bool:
        """Update runtime SL/TP fields for an open trade row."""
        assert self._conn
        self._require_trade_fields("update", {"bot_id": bot_id, "opened_at": opened_at})
        if request_key:
            existing = self._conn.execute("SELECT id FROM trades WHERE request_key = ?", (request_key,)).fetchone()
            if existing:
                self._mark_confirmed(bot_id, request_key)
                return True

        row_id = self._latest_open_row_id(bot_id, opened_at, str(data.get("symbol", "") or ""))
        if row_id is None:
            row_id = self._latest_open_row_id(bot_id, opened_at)
        if row_id is None:
            return False

        try:
            cursor = self._execute_write_with_lock_retry(
                """UPDATE trades SET
                planned_stop_loss=COALESCE(?, planned_stop_loss),
                planned_tp1=COALESCE(?, planned_tp1),
                planned_tp2=COALESCE(?, planned_tp2),
                exchange_stop_loss=COALESCE(?, exchange_stop_loss),
                exchange_take_profit=COALESCE(?, exchange_take_profit),
                bot_stop_loss=COALESCE(?, bot_stop_loss),
                bot_take_profit=COALESCE(?, bot_take_profit),
                effective_stop_loss=COALESCE(?, effective_stop_loss),
                effective_take_profit=COALESCE(?, effective_take_profit),
                stop_source=COALESCE(NULLIF(?, ''), stop_source),
                tp_source=COALESCE(NULLIF(?, ''), tp_source),
                request_key=CASE WHEN ?='' THEN request_key ELSE ? END
            WHERE id=?""",
                (
                    self._patch_value(data, "planned_stop_loss"),
                    self._patch_value(data, "planned_tp1"),
                    self._patch_value(data, "planned_tp2"),
                    self._patch_value(data, "exchange_stop_loss"),
                    self._patch_value(data, "exchange_take_profit"),
                    self._patch_value(data, "bot_stop_loss"),
                    self._patch_value(data, "bot_take_profit"),
                    self._patch_value(data, "effective_stop_loss"),
                    self._patch_value(data, "effective_take_profit"),
                    self._patch_value(data, "stop_source"),
                    self._patch_value(data, "tp_source"),
                    request_key,
                    request_key,
                    row_id,
                ),
            )
        except (DBIntegrityError, ldb.IntegrityError):
            if request_key:
                existing = self._conn.execute("SELECT id FROM trades WHERE request_key = ?", (request_key,)).fetchone()
                if existing:
                    self._mark_confirmed(bot_id, request_key)
                    return True
            raise
        updated = cursor.rowcount > 0
        if updated and request_key:
            self._mark_confirmed(bot_id, request_key)
        return bool(updated)

    def cancel_trade_reservation(self, bot_id: str, opened_at: str, request_key: str = "") -> bool:
        """Delete an unexecuted pre-open reservation row.

        Used when a reserved open never turns into a real exchange fill
        (risk gate reject, open exception, or fill timeout).
        """
        assert self._conn
        self._require_trade_fields("cancel_reservation", {"bot_id": bot_id, "opened_at": opened_at})
        if request_key:
            existing = self._conn.execute("SELECT id FROM trades WHERE request_key = ?", (request_key,)).fetchone()
            if existing:
                self._mark_confirmed(bot_id, request_key)
                return True

        row_id = self._latest_open_row_id(bot_id, opened_at)
        if row_id is None:
            return False

        cursor = self._execute_write_with_lock_retry(
            """
            DELETE FROM trades
            WHERE id=? AND action='open'
            """,
            (row_id,),
        )
        deleted = cursor.rowcount > 0
        if request_key and deleted:
            self._mark_confirmed(bot_id, request_key)
        return bool(deleted)

    def cleanup_non_executed_close_noise(self) -> int:
        """Delete close rows that represent failed pre-open flow, not executed trades."""
        assert self._conn
        cursor = self._execute_write_with_lock_retry(
            """
            DELETE FROM trades
            WHERE action='close'
              AND COALESCE(recovery_close, 0)=0
              AND (
                close_source='reservation_cancel'
                OR close_reason IN ('risk_or_gate', 'open_exception', 'failed_fill:pending')
              )
              AND COALESCE(exit_price, 0) <= 0
              AND ABS(COALESCE(pnl_usd, 0)) < 1e-12
            """,
            (),
        )
        return int(cursor.rowcount or 0)

    def cleanup_duplicate_trade_rows(self) -> int:
        """Deduplicate rows created by retried/reserved writes.

        Keep only the newest row per `(bot_id, symbol, opened_at, action, closed_at)`.
        Rows without `opened_at` are ignored to avoid collapsing historical legacy data.
        """
        assert self._conn
        cursor = self._execute_write_with_lock_retry(
            """
            DELETE FROM trades
            WHERE COALESCE(opened_at, '') != ''
              AND id NOT IN (
                SELECT MAX(id)
                FROM trades
                WHERE COALESCE(opened_at, '') != ''
                GROUP BY
                  bot_id,
                  symbol,
                  opened_at,
                  action,
                  COALESCE(closed_at, '')
              )
            """,
            (),
        )
        return int(cursor.rowcount or 0)

    def mark_recovery_close(
        self,
        bot_id: str,
        opened_at: str,
        estimated_exit_price: float = 0.0,
        estimated_pnl_usd: float = 0.0,
        estimated_pnl_pct: float = 0.0,
    ) -> bool:
        """Mark an open trade as closed due to bot recovery.

        Recovery rows remain excluded from strategy analytics, but we store
        best-effort exit estimates when available for operational forensics.
        """
        assert self._conn
        closed_at = datetime.now(UTC).isoformat()
        has_estimate = abs(float(estimated_exit_price or 0.0)) > 0 or abs(float(estimated_pnl_usd or 0.0)) > 0
        cursor = self._conn.execute(
            """UPDATE trades SET
                action='close', recovery_close=1, closed_at=?,
                close_source='recovery',
                close_reason=?,
                exit_price=CASE WHEN ? > 0 THEN ? ELSE exit_price END,
                pnl_usd=CASE WHEN ? != 0 THEN ? ELSE pnl_usd END,
                pnl_pct=CASE WHEN ? != 0 THEN ? ELSE pnl_pct END,
                is_winner=CASE
                    WHEN ? != 0 THEN CASE WHEN ? > 0 THEN 1 ELSE 0 END
                    ELSE is_winner
                END
            WHERE bot_id=? AND opened_at=? AND closed_at=''""",
            (
                closed_at,
                "missing_on_exchange_estimated" if has_estimate else "missing_on_exchange",
                float(estimated_exit_price or 0.0),
                float(estimated_exit_price or 0.0),
                float(estimated_pnl_usd or 0.0),
                float(estimated_pnl_usd or 0.0),
                float(estimated_pnl_pct or 0.0),
                float(estimated_pnl_pct or 0.0),
                float(estimated_pnl_usd or 0.0),
                float(estimated_pnl_usd or 0.0),
                bot_id,
                opened_at,
            ),
        )
        self._conn.commit()
        updated = bool(cursor.rowcount > 0)
        if updated:
            self._clear_swing_plan_after_trade_close(bot_id=bot_id, opened_at=opened_at, symbol_hint="")
        return updated

    def _clear_swing_plan_after_trade_close(self, bot_id: str, opened_at: str, symbol_hint: str = "") -> None:
        """Delete runtime swing plan rows once the owning trade is closed."""
        assert self._conn
        opened = str(opened_at or "").strip()
        bid = str(bot_id or "").strip().lower()
        if not bid or not opened:
            return
        symbol = str(symbol_hint or "").strip().upper()
        if not symbol:
            row = self._conn.execute(
                """
                SELECT symbol
                FROM trades
                WHERE bot_id=? AND opened_at=?
                ORDER BY id DESC
                LIMIT 1
                """,
                (bid, opened),
            ).fetchone()
            symbol = str(row["symbol"] or "").strip().upper() if row else ""
        if not symbol:
            return
        self.clear_swing_entry_plan(bid, symbol, opened)

    def insert_exchange_equity_snapshot(
        self,
        exchange: str,
        available_usdt: float,
        estimated_equity_usdt: float,
        open_positions: int,
        source_bot: str,
        source: str = "bot_report",
    ) -> None:
        """Persist a point-in-time account equity snapshot for an exchange."""
        assert self._conn
        now_iso = datetime.now(UTC).isoformat()
        self._conn.execute(
            """
            INSERT INTO exchange_equity_snapshots (
                exchange, available_usdt, estimated_equity_usdt,
                open_positions, source_bot, source, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(exchange or "").strip().upper(),
                float(available_usdt or 0.0),
                float(estimated_equity_usdt or 0.0),
                int(open_positions or 0),
                str(source_bot or ""),
                str(source or "bot_report"),
                now_iso,
            ),
        )
        self._conn.commit()

    def get_exchange_equity_baselines(self, day_start_iso: str) -> dict[str, dict[str, float]]:
        """Return per-exchange inception and day-start equity baselines.

        Day-start baseline selection per exchange:
        - Prefer latest snapshot at or before day_start_iso.
        - Otherwise use first snapshot after day_start_iso.
        - Otherwise fall back to inception snapshot.
        """
        assert self._conn
        rows = self._conn.execute(
            """
            SELECT exchange, estimated_equity_usdt, created_at
            FROM exchange_equity_snapshots
            WHERE exchange != ''
            ORDER BY exchange ASC, created_at ASC, id ASC
            """
        ).fetchall()

        inception: dict[str, float] = {}
        day_start: dict[str, float] = {}
        before_cutoff: dict[str, float] = {}
        after_cutoff_first: dict[str, float] = {}

        cutoff = str(day_start_iso or "")
        for r in rows:
            ex = str(r["exchange"] or "").strip().upper()
            if not ex:
                continue
            eq = float(r["estimated_equity_usdt"] or 0.0)
            ts = str(r["created_at"] or "")
            if ex not in inception:
                inception[ex] = eq
            if cutoff and ts <= cutoff:
                before_cutoff[ex] = eq
            elif ex not in after_cutoff_first:
                after_cutoff_first[ex] = eq

        exchanges = set(inception.keys()) | set(before_cutoff.keys()) | set(after_cutoff_first.keys())
        for ex in exchanges:
            if ex in before_cutoff:
                day_start[ex] = before_cutoff[ex]
            elif ex in after_cutoff_first:
                day_start[ex] = after_cutoff_first[ex]
            else:
                day_start[ex] = inception.get(ex, 0.0)

        return {"inception": inception, "day_start": day_start}

    # ---- Bot-centric queries ----

    def get_open_trade_symbols(self) -> set[str]:
        """Return the set of symbols with at least one unclosed trade (any bot)."""
        assert self._conn
        rows = self._conn.execute("SELECT DISTINCT symbol FROM trades WHERE closed_at=''").fetchall()
        return {r["symbol"] for r in rows}

    def get_open_trade_owner_rows(self) -> list[dict[str, Any]]:
        """Return newest open-trade ownership rows for hub-side classifiers."""
        assert self._conn
        rows = self._conn.execute(
            "SELECT id, bot_id, symbol FROM trades WHERE closed_at='' ORDER BY id DESC"
        ).fetchall()
        return [{"id": int(r["id"]), "bot_id": str(r["bot_id"] or ""), "symbol": str(r["symbol"] or "")} for r in rows]

    def get_original_trade_owner_rows(self) -> list[dict[str, Any]]:
        """Return immutable original owner per symbol (first trade row ever)."""
        assert self._conn
        rows = self._conn.execute(
            """
            SELECT id, bot_id, symbol
            FROM (
                SELECT id, bot_id, symbol,
                       ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY id ASC) AS rn
                FROM trades
                WHERE symbol!='' AND bot_id!=''
            ) first_owner
            WHERE rn=1
            ORDER BY id ASC
            """
        ).fetchall()
        return [{"id": int(r["id"]), "bot_id": str(r["bot_id"] or ""), "symbol": str(r["symbol"] or "")} for r in rows]

    def get_recent_closed_owner_rows(self, lookback_hours: int = 24) -> list[dict[str, Any]]:
        """Return latest close-owner hints for symbols closed recently.

        Used to recover ownership when a close was marked locally but the exchange
        position is still live (or reappears). Excludes synthetic/non-executed
        close reasons.
        """
        assert self._conn
        cutoff = (datetime.now(UTC) - timedelta(hours=max(1, int(lookback_hours)))).isoformat()
        rows = self._conn.execute(
            """
            SELECT id, bot_id, symbol
            FROM (
                SELECT id, bot_id, symbol,
                       ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY closed_at DESC, id DESC) AS rn
                FROM trades
                WHERE action='close'
                  AND closed_at != ''
                  AND closed_at >= ?
                  AND symbol != ''
                  AND bot_id != ''
                  AND COALESCE(close_source, '') NOT IN ('reservation_cancel', 'recovery')
                  AND COALESCE(close_reason, '') NOT IN ('risk_or_gate', 'open_exception', 'failed_fill:pending')
            ) latest
            WHERE rn = 1
            ORDER BY id DESC
            """,
            (cutoff,),
        ).fetchall()
        return [{"id": int(r["id"]), "bot_id": str(r["bot_id"] or ""), "symbol": str(r["symbol"] or "")} for r in rows]

    def get_recent_recovery_owner_rows(self, lookback_hours: int = 24) -> list[dict[str, Any]]:
        """Return recent recovery-closed owner rows as fallback ownership hints."""
        assert self._conn
        cutoff = (datetime.now(UTC) - timedelta(hours=max(1, int(lookback_hours)))).isoformat()
        rows = self._conn.execute(
            """
            SELECT id, bot_id, symbol
            FROM trades
            WHERE closed_at != ''
              AND closed_at >= ?
              AND close_source = 'recovery'
              AND close_reason LIKE ?
            ORDER BY id DESC
            """,
            (cutoff, "missing_on_exchange%"),
        ).fetchall()
        return [{"id": int(r["id"]), "bot_id": str(r["bot_id"] or ""), "symbol": str(r["symbol"] or "")} for r in rows]

    def get_recent_recovery_owner_symbols(self, bot_id: str, lookback_hours: int = 24) -> list[str]:
        """Return symbols whose latest recovery owner is this bot."""
        assert self._conn
        bid = str(bot_id or "").strip().lower()
        if not bid:
            return []
        cutoff = (datetime.now(UTC) - timedelta(hours=max(1, int(lookback_hours)))).isoformat()
        rows = self._conn.execute(
            """
            SELECT symbol
            FROM (
                SELECT symbol, bot_id,
                       ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY closed_at DESC, id DESC) AS rn
                FROM trades
                WHERE closed_at != ''
                  AND closed_at >= ?
                  AND close_source = 'recovery'
                  AND close_reason LIKE ?
                  AND symbol != ''
            ) latest
            WHERE rn = 1 AND bot_id = ?
            ORDER BY symbol
            """,
            (cutoff, "missing_on_exchange%", bid),
        ).fetchall()
        return [str(r["symbol"] or "") for r in rows if str(r["symbol"] or "").strip()]

    def get_recent_closed_owner_symbols(self, bot_id: str, lookback_hours: int = 24) -> list[str]:
        """Return symbols whose latest close-owner hint maps to this bot."""
        assert self._conn
        bid = str(bot_id or "").strip().lower()
        if not bid:
            return []
        cutoff = (datetime.now(UTC) - timedelta(hours=max(1, int(lookback_hours)))).isoformat()
        rows = self._conn.execute(
            """
            SELECT symbol
            FROM (
                SELECT symbol, bot_id,
                       ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY closed_at DESC, id DESC) AS rn
                FROM trades
                WHERE action='close'
                  AND closed_at != ''
                  AND closed_at >= ?
                  AND symbol != ''
                  AND bot_id != ''
                  AND COALESCE(close_source, '') NOT IN ('reservation_cancel', 'recovery')
                  AND COALESCE(close_reason, '') NOT IN ('risk_or_gate', 'open_exception', 'failed_fill:pending')
            ) latest
            WHERE rn = 1 AND bot_id = ?
            ORDER BY symbol
            """,
            (cutoff, bid),
        ).fetchall()
        return [str(r["symbol"] or "") for r in rows if str(r["symbol"] or "").strip()]

    def get_open_trades_for_bot(self, bot_id: str) -> list[TradeRecord]:
        """Return unclosed trades for a bot, deduped by symbol.

        We keep the newest row per symbol because reserve/update flows can leave
        multiple historical open rows with the same symbol/opened_at during
        retries; bots must recover one live position per symbol.
        """
        assert self._conn
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE bot_id=? AND closed_at='' ORDER BY id DESC",
            (bot_id,),
        ).fetchall()
        deduped: list[TradeRecord] = []
        seen_symbols: set[str] = set()
        for row in rows:
            symbol = str(row["symbol"] or "")
            if symbol in seen_symbols:
                continue
            seen_symbols.add(symbol)
            deduped.append(self._row_to_trade(row))
        return deduped

    def get_strategy_stats_for_bot(self, bot_id: str, strategy: str, symbol: str = "") -> dict[str, Any]:
        """Strategy stats scoped to a single bot (excludes recovery_close trades)."""
        assert self._conn
        where = "bot_id=? AND strategy=? AND recovery_close=0 AND action='close'"
        params: list[str | int] = [bot_id, strategy]
        if symbol:
            where += " AND symbol=?"
            params.append(symbol)

        row = self._conn.execute(
            f"""SELECT
                COUNT(*) as total,
                SUM(CASE WHEN is_winner=1 THEN 1 ELSE 0 END) as winners,
                SUM(CASE WHEN is_winner=0 AND pnl_usd!=0 THEN 1 ELSE 0 END) as losers,
                SUM(pnl_usd) as total_pnl
            FROM trades WHERE {where}""",
            params,
        ).fetchone()
        if not row:
            return {}
        result = dict(row)
        if result.get("total_pnl") is None:
            result["total_pnl"] = 0.0
        return result

    def get_all_strategy_stats_for_bot(self, bot_id: str) -> dict[str, dict[str, Any]]:
        """Return stats keyed by 'strategy:symbol' for all strategies a bot has traded."""
        assert self._conn
        rows = self._conn.execute(
            """SELECT strategy, symbol,
                COUNT(*) as total,
                SUM(CASE WHEN is_winner=1 THEN 1 ELSE 0 END) as winners,
                SUM(CASE WHEN is_winner=0 AND pnl_usd!=0 THEN 1 ELSE 0 END) as losers,
                SUM(pnl_usd) as total_pnl
            FROM trades
            WHERE bot_id=? AND recovery_close=0 AND action='close'
            GROUP BY strategy, symbol""",
            (bot_id,),
        ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for r in rows:
            key = f"{r['strategy']}:{r['symbol']}" if r["symbol"] else r["strategy"]
            d = dict(r)
            if d.get("total_pnl") is None:
                d["total_pnl"] = 0.0
            result[key] = d
        return result

    def get_bot_summary(self, bot_id: str) -> dict[str, Any]:
        """Aggregate wins, losses, total PnL for a bot (excludes recovery closes)."""
        assert self._conn
        row = self._conn.execute(
            """SELECT
                SUM(CASE WHEN is_winner=1 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN is_winner=0 AND pnl_usd!=0 THEN 1 ELSE 0 END) as losses,
                COALESCE(SUM(pnl_usd), 0) as total_pnl
            FROM trades
            WHERE bot_id=? AND action='close' AND recovery_close=0""",
            (bot_id,),
        ).fetchone()
        if not row:
            return {"wins": 0, "losses": 0, "total_pnl": 0.0}
        return {"wins": row["wins"] or 0, "losses": row["losses"] or 0, "total_pnl": row["total_pnl"] or 0.0}

    # ---- Swing entry ladder persistence ----

    def replace_swing_entry_plan(
        self,
        bot_id: str,
        symbol: str,
        opened_at: str,
        entries: list[dict[str, Any]],
    ) -> None:
        """Replace persisted ladder entries for one open trade."""
        assert self._conn
        bid = str(bot_id or "").strip().lower()
        sym = str(symbol or "").strip().upper()
        opened = str(opened_at or "").strip()
        if not bid or not sym or not opened:
            return
        now_iso = datetime.now(UTC).isoformat()
        if not entries:
            self.clear_swing_entry_plan(bid, sym, opened)
            return
        first = entries[0]
        plan_id = str(first.get("parent_plan_id", "") or "").strip()
        if not plan_id:
            raise DBIntegrityError("missing_parent_plan_id:swing_plan")
        mode = str(first.get("mode", "swing_auto") or "swing_auto").strip().lower() or "swing_auto"
        exchange = str(first.get("exchange", "") or "").strip().upper()
        direction = "short" if str(first.get("side", "") or "").strip().lower() in {"short", "sell"} else "long"
        first_entry_price = float(first.get("first_entry_price", 0.0) or 0.0)
        last_entry_price = float(first.get("last_entry_price", 0.0) or 0.0)
        grid_count = int(first.get("grid_count", 0) or 0)
        leverage = int(first.get("leverage", 1) or 1)
        margin_amount = float(first.get("margin_amount", 0.0) or 0.0)
        plan_state = str(first.get("plan_state", "active") or "active").strip().lower() or "active"
        cex_cap = int(first.get("max_concurrent_limit_orders_on_cex", 3) or 3)
        existing = self._conn.execute(
            "SELECT created_at FROM swing_plans WHERE plan_id=?",
            (plan_id,),
        ).fetchone()
        created_at = (
            str(existing["created_at"] or now_iso) if existing else str(first.get("created_at", now_iso) or now_iso)
        )
        self._conn.execute(
            """
            INSERT INTO swing_plans (
                plan_id, bot_id, symbol, mode, exchange, direction,
                first_entry_price, last_entry_price, grid_count, leverage,
                margin_amount, max_concurrent_limit_orders_on_cex, plan_state,
                opened_at, updated_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(plan_id) DO UPDATE SET
                bot_id=excluded.bot_id,
                symbol=excluded.symbol,
                mode=excluded.mode,
                exchange=excluded.exchange,
                direction=excluded.direction,
                first_entry_price=excluded.first_entry_price,
                last_entry_price=excluded.last_entry_price,
                grid_count=excluded.grid_count,
                leverage=excluded.leverage,
                margin_amount=excluded.margin_amount,
                max_concurrent_limit_orders_on_cex=excluded.max_concurrent_limit_orders_on_cex,
                plan_state=excluded.plan_state,
                opened_at=excluded.opened_at,
                updated_at=excluded.updated_at
            """,
            (
                plan_id,
                bid,
                sym,
                mode,
                exchange,
                direction,
                first_entry_price,
                last_entry_price,
                grid_count,
                leverage,
                margin_amount,
                cex_cap,
                plan_state,
                opened,
                now_iso,
                created_at,
            ),
        )
        self._conn.execute("DELETE FROM swing_plan_entries WHERE plan_id=?", (plan_id,))
        for item in entries:
            parent_plan_id = str(item.get("parent_plan_id", "") or "").strip()
            if not parent_plan_id:
                raise DBIntegrityError("missing_parent_plan_id:swing_plan")
            self._conn.execute(
                """
                INSERT INTO swing_plan_entries (
                    plan_id, entry_idx, side, price, amount, leverage,
                    status, order_id, strategy, updated_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    int(item.get("entry_idx", 0) or 0),
                    str(item.get("side", "") or ""),
                    float(item.get("price", 0.0) or 0.0),
                    float(item.get("amount", 0.0) or 0.0),
                    int(item.get("leverage", 1) or 1),
                    str(item.get("status", "planned") or "planned"),
                    str(item.get("order_id", "") or ""),
                    str(item.get("strategy", "") or ""),
                    now_iso,
                    str(item.get("created_at", now_iso) or now_iso),
                ),
            )
        self._conn.commit()

    def get_swing_entry_plan(self, bot_id: str, symbol: str, opened_at: str) -> list[dict[str, Any]]:
        """Load persisted ladder entries for one open trade."""
        assert self._conn
        rows = self._conn.execute(
            """
            SELECT
                p.plan_id AS parent_plan_id,
                e.entry_idx, e.side, e.price, e.amount, e.leverage, e.status, e.order_id, e.strategy,
                p.mode, p.exchange, p.first_entry_price, p.last_entry_price, p.grid_count,
                p.margin_amount, p.plan_state, p.max_concurrent_limit_orders_on_cex,
                e.updated_at, e.created_at
            FROM swing_plans p
            JOIN swing_plan_entries e ON e.plan_id = p.plan_id
            WHERE p.bot_id=? AND p.symbol=? AND p.opened_at=?
            ORDER BY e.entry_idx ASC
            """,
            (str(bot_id or "").strip().lower(), str(symbol or "").strip().upper(), str(opened_at or "").strip()),
        ).fetchall()
        return [dict(r) for r in rows]

    def clear_swing_entry_plan(self, bot_id: str, symbol: str, opened_at: str) -> None:
        """Delete persisted ladder entries for one open trade."""
        assert self._conn
        bid = str(bot_id or "").strip().lower()
        sym = str(symbol or "").strip().upper()
        opened = str(opened_at or "").strip()
        plan_rows = self._conn.execute(
            "SELECT plan_id FROM swing_plans WHERE bot_id=? AND symbol=? AND opened_at=?",
            (bid, sym, opened),
        ).fetchall()
        plan_ids = [str(r["plan_id"] or "").strip() for r in plan_rows if str(r["plan_id"] or "").strip()]
        for pid in plan_ids:
            self._conn.execute("DELETE FROM swing_plan_entries WHERE plan_id=?", (pid,))
        self._conn.execute(
            "DELETE FROM swing_plans WHERE bot_id=? AND symbol=? AND opened_at=?",
            (bid, sym, opened),
        )
        self._conn.commit()

    def create_manual_swing_plan(
        self,
        bot_id: str,
        exchange: str,
        symbol: str,
        direction: str,
        first_entry_price: float,
        last_entry_price: float,
        grid_count: int,
        leverage: int,
        margin_amount: float,
        max_concurrent_limit_orders_on_cex: int,
    ) -> dict[str, Any]:
        """Create one manual swing plan with all grid legs persisted upfront."""
        assert self._conn
        bid = str(bot_id or "").strip().lower() or "swing"
        ex = str(exchange or "").strip().upper()
        sym = str(symbol or "").strip().upper()
        side = "short" if str(direction or "").strip().lower() in {"short", "sell"} else "long"
        first_px = float(first_entry_price or 0.0)
        last_px = float(last_entry_price or 0.0)
        legs = max(1, int(grid_count or 1))
        lev = max(1, int(leverage or 1))
        margin = max(0.0, float(margin_amount or 0.0))
        cex_cap = max(1, int(max_concurrent_limit_orders_on_cex or 3))
        plan_id = datetime.now(UTC).isoformat()
        created_at = datetime.now(UTC).isoformat()
        step = 0.0 if legs <= 1 else (last_px - first_px) / float(legs - 1)
        per_leg_margin = margin / float(legs) if legs > 0 else 0.0

        rows: list[dict[str, Any]] = []
        for idx in range(1, legs + 1):
            price = float(first_px + step * float(idx - 1))
            amount = 0.0
            if price > 0 and per_leg_margin > 0:
                amount = (per_leg_margin * float(lev)) / price
            rows.append(
                {
                    "parent_plan_id": plan_id,
                    "entry_idx": idx,
                    "side": side,
                    "price": price,
                    "amount": amount,
                    "leverage": lev,
                    "status": "planned",
                    "order_id": "",
                    "strategy": "swing_manual",
                    "mode": "swing_manual",
                    "exchange": ex,
                    "first_entry_price": first_px,
                    "last_entry_price": last_px,
                    "grid_count": legs,
                    "margin_amount": margin,
                    "plan_state": "active",
                    "max_concurrent_limit_orders_on_cex": cex_cap,
                    "created_at": created_at,
                }
            )
        self.replace_swing_entry_plan(bid, sym, plan_id, rows)
        return {
            "plan_id": plan_id,
            "bot_id": bid,
            "mode": "swing_manual",
            "exchange": ex,
            "symbol": sym,
            "direction": side,
            "first_entry_price": first_px,
            "last_entry_price": last_px,
            "grid_count": legs,
            "leverage": lev,
            "margin_amount": margin,
            "max_concurrent_limit_orders_on_cex": cex_cap,
            "plan_state": "active",
            "entries": rows,
        }

    def list_swing_plans(self, bot_id: str, mode: str) -> list[dict[str, Any]]:
        assert self._conn
        rows = self._conn.execute(
            """
            SELECT
                p.plan_id,
                p.bot_id,
                p.mode,
                p.exchange,
                p.symbol,
                p.direction,
                p.first_entry_price,
                p.last_entry_price,
                p.grid_count,
                p.leverage,
                p.margin_amount,
                p.max_concurrent_limit_orders_on_cex,
                p.plan_state,
                p.created_at,
                SUM(CASE WHEN e.status='planned' THEN 1 ELSE 0 END) AS planned_legs,
                SUM(CASE WHEN e.status='placed_on_cex' THEN 1 ELSE 0 END) AS placed_legs,
                SUM(CASE WHEN e.status='filled' THEN 1 ELSE 0 END) AS filled_legs,
                SUM(CASE WHEN e.status='cancelled' THEN 1 ELSE 0 END) AS cancelled_legs,
                SUM(CASE WHEN e.status='failed' THEN 1 ELSE 0 END) AS failed_legs
            FROM swing_plans p
            LEFT JOIN swing_plan_entries e ON e.plan_id=p.plan_id
            WHERE p.bot_id=? AND p.mode=?
            GROUP BY
                p.plan_id, p.bot_id, p.mode, p.exchange, p.symbol, p.direction, p.first_entry_price,
                p.last_entry_price, p.grid_count, p.leverage, p.margin_amount,
                p.max_concurrent_limit_orders_on_cex, p.plan_state, p.created_at
            ORDER BY p.created_at DESC
            """,
            (str(bot_id or "").strip().lower(), str(mode or "").strip().lower()),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_swing_plan(self, bot_id: str, symbol: str, plan_id: str) -> dict[str, Any] | None:
        assert self._conn
        entries = self.get_swing_entry_plan(bot_id, symbol, plan_id)
        if not entries:
            return None
        hdr = self._conn.execute(
            """
            SELECT
                plan_id, bot_id, mode, exchange, symbol, direction,
                first_entry_price, last_entry_price, grid_count, leverage,
                margin_amount, max_concurrent_limit_orders_on_cex, plan_state
            FROM swing_plans
            WHERE plan_id=? AND bot_id=? AND symbol=?
            LIMIT 1
            """,
            (
                str(plan_id or "").strip(),
                str(bot_id or "").strip().lower(),
                str(symbol or "").strip().upper(),
            ),
        ).fetchone()
        if not hdr:
            return None
        return {
            "plan_id": str(hdr["plan_id"] or ""),
            "bot_id": str(hdr["bot_id"] or ""),
            "mode": str(hdr["mode"] or ""),
            "exchange": str(hdr["exchange"] or ""),
            "symbol": str(hdr["symbol"] or ""),
            "direction": str(hdr["direction"] or ""),
            "first_entry_price": float(hdr["first_entry_price"] or 0.0),
            "last_entry_price": float(hdr["last_entry_price"] or 0.0),
            "grid_count": int(hdr["grid_count"] or 0),
            "leverage": int(hdr["leverage"] or 1),
            "margin_amount": float(hdr["margin_amount"] or 0.0),
            "max_concurrent_limit_orders_on_cex": int(hdr["max_concurrent_limit_orders_on_cex"] or 3),
            "plan_state": str(hdr["plan_state"] or "active"),
            "entries": entries,
        }

    def set_swing_plan_state(self, bot_id: str, symbol: str, plan_id: str, plan_state: str) -> int:
        assert self._conn
        now_iso = datetime.now(UTC).isoformat()
        cur = self._conn.execute(
            """
            UPDATE swing_plans
            SET plan_state=?, updated_at=?
            WHERE bot_id=? AND symbol=? AND (plan_id=? OR opened_at=?)
            """,
            (
                str(plan_state or "").strip().lower() or "active",
                now_iso,
                str(bot_id or "").strip().lower(),
                str(symbol or "").strip().upper(),
                str(plan_id or "").strip(),
                str(plan_id or "").strip(),
            ),
        )
        self._conn.commit()
        return int(cur.rowcount or 0)

    def list_active_swing_manual_plans(self, bot_id: str, exchange: str) -> list[dict[str, Any]]:
        assert self._conn
        rows = self._conn.execute(
            """
            SELECT symbol, plan_id
            FROM swing_plans
            WHERE
                bot_id=? AND mode='swing_manual' AND exchange=? AND plan_state='active'
            ORDER BY created_at ASC
            """,
            (str(bot_id or "").strip().lower(), str(exchange or "").strip().upper()),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- Acknowledgment buffer ----

    def _mark_confirmed(self, bot_id: str, request_key: str) -> None:
        """Add a request_key to the per-bot confirmation buffer."""
        if bot_id not in self._ack_buffer:
            self._ack_buffer[bot_id] = set()
        self._ack_buffer[bot_id].add(request_key)

    def drain_confirmed_keys(self, bot_id: str) -> list[str]:
        """Return and clear all confirmed request_keys for a bot."""
        keys = list(self._ack_buffer.pop(bot_id, set()))
        return keys

    # ---- Bot enable/disable ----

    def set_bot_enabled(self, bot_id: str, enabled: bool) -> None:
        assert self._conn
        self._conn.execute(
            "INSERT INTO bot_config (bot_id, enabled) VALUES (?, ?) "
            "ON CONFLICT(bot_id) DO UPDATE SET enabled=excluded.enabled",
            (bot_id, int(enabled)),
        )
        self._conn.commit()

    def is_bot_enabled(self, bot_id: str, default: bool = True) -> bool:
        assert self._conn
        row = self._conn.execute("SELECT enabled FROM bot_config WHERE bot_id=?", (bot_id,)).fetchone()
        return bool(row["enabled"]) if row else default

    def get_all_bot_enabled(self) -> dict[str, bool]:
        assert self._conn
        rows = self._conn.execute("SELECT bot_id, enabled FROM bot_config").fetchall()
        return {r["bot_id"]: bool(r["enabled"]) for r in rows}

    # ---- Runtime tuning (global + per-bot) ----

    def set_runtime_tuning(self, key: str, value: Any, bot_id: str = "*") -> None:
        assert self._conn
        normalized_key = str(key or "").strip()
        target_bot = str(bot_id or "*").strip().lower() or "*"
        if not normalized_key:
            return
        if value is None:
            self._conn.execute(
                "DELETE FROM runtime_tuning WHERE bot_id=? AND key=?",
                (target_bot, normalized_key),
            )
            self._conn.commit()
            self._runtime_tuning_rows.pop((target_bot, normalized_key), None)
            self._runtime_tuning_effective_cache.clear()
            return

        normalized = normalize_runtime_tuning({normalized_key: value})
        if normalized_key not in normalized:
            return
        normalized_value = normalized[normalized_key]
        self._conn.execute(
            "INSERT INTO runtime_tuning (bot_id, key, value_json, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(bot_id, key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
            (
                target_bot,
                normalized_key,
                json.dumps(normalized_value),
                datetime.now(UTC).isoformat(),
            ),
        )
        self._conn.commit()
        self._runtime_tuning_rows[(target_bot, normalized_key)] = normalized_value
        self._runtime_tuning_effective_cache.clear()

    def get_runtime_tuning(self, bot_id: str) -> tuple[dict[str, Any], str]:
        target_bot = str(bot_id or "").strip().lower()
        cached = self._runtime_tuning_effective_cache.get(target_bot)
        if cached is not None:
            return dict(cached[0]), cached[1]
        self._ensure_runtime_tuning_loaded()
        merged: dict[str, Any] = {}
        for (row_bot, row_key), row_value in self._runtime_tuning_rows.items():
            if row_bot == "*":
                merged[row_key] = row_value
        if target_bot:
            for (row_bot, row_key), row_value in self._runtime_tuning_rows.items():
                if row_bot == target_bot:
                    merged[row_key] = row_value
        normalized = normalize_runtime_tuning(merged)
        rev = runtime_tuning_revision(normalized)
        self._runtime_tuning_effective_cache[target_bot] = (dict(normalized), rev)
        return normalized, rev

    def get_runtime_tuning_overrides(self, bot_id: str) -> tuple[dict[str, Any], str]:
        target_bot = str(bot_id or "").strip().lower()
        if not target_bot:
            return {}, runtime_tuning_revision({})
        self._ensure_runtime_tuning_loaded()
        overrides: dict[str, Any] = {}
        for (row_bot, row_key), row_value in self._runtime_tuning_rows.items():
            if row_bot == target_bot:
                overrides[row_key] = row_value
        normalized = normalize_runtime_tuning(overrides)
        return normalized, runtime_tuning_revision(normalized)

    def _ensure_runtime_tuning_loaded(self) -> None:
        if self._runtime_tuning_loaded:
            return
        assert self._conn
        rows = self._conn.execute("SELECT bot_id, key, value_json FROM runtime_tuning").fetchall()
        loaded: dict[tuple[str, str], Any] = {}
        for row in rows:
            row_bot = str(row["bot_id"] or "*").strip().lower() or "*"
            row_key = str(row["key"] or "").strip()
            if not row_key:
                continue
            try:
                row_value = json.loads(row["value_json"] or "null")
            except Exception:
                continue
            normalized = normalize_runtime_tuning({row_key: row_value})
            if row_key not in normalized:
                continue
            loaded[(row_bot, row_key)] = normalized[row_key]
        self._runtime_tuning_rows = loaded
        self._runtime_tuning_effective_cache.clear()
        self._runtime_tuning_loaded = True

    # ---- Exchange symbols (hub fetches directly from exchanges) ----

    def save_exchange_symbols(self, exchange: str, symbols: set[str]) -> None:
        """Persist the symbol set for an exchange (upsert)."""
        assert self._conn
        self._conn.execute(
            "INSERT INTO exchange_symbols (exchange, symbols, updated_at) "
            "VALUES (?, ?, ?) ON CONFLICT(exchange) DO UPDATE SET symbols=excluded.symbols, updated_at=excluded.updated_at",
            (exchange.upper(), json.dumps(sorted(symbols)), datetime.now(UTC).isoformat()),
        )
        self._conn.commit()

    def load_all_exchange_symbols(self) -> dict[str, set[str]]:
        """Load persisted exchange symbols from DB (used as startup seed)."""
        assert self._conn
        rows = self._conn.execute("SELECT exchange, symbols FROM exchange_symbols").fetchall()
        result: dict[str, set[str]] = {}
        for r in rows:
            try:
                result[r["exchange"]] = set(json.loads(r["symbols"]))
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning("Skipping corrupt exchange_symbols row for {}: {}", r.get("exchange", "?"), e)
                continue
        return result

    # ---- Binance futures scanner snapshots ----

    def save_binance_snapshots(self, rows: list[dict[str, Any]]) -> None:
        """Upsert minute snapshots produced by BinanceFuturesScanner."""
        if not rows:
            return
        assert self._conn
        self._conn.executemany(
            """
            INSERT INTO cex_binance_snapshots
            (timestamp, symbol, price, quote_volume, change_24h, funding_rate)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(timestamp, symbol) DO UPDATE SET
                price=excluded.price,
                quote_volume=excluded.quote_volume,
                change_24h=excluded.change_24h,
                funding_rate=excluded.funding_rate
            """,
            [
                (
                    r.get("timestamp", ""),
                    r.get("symbol", ""),
                    float(r.get("price", 0.0)),
                    float(r.get("quote_volume", 0.0)),
                    float(r.get("change_24h", 0.0)),
                    float(r.get("funding_rate", 0.0)),
                )
                for r in rows
            ],
        )
        self._conn.commit()

    def load_binance_snapshots_since(self, since_iso: str) -> list[Any]:
        """Load scanner snapshots since a given ISO timestamp."""
        assert self._conn
        rows = self._conn.execute(
            """
            SELECT timestamp, symbol, price, quote_volume, change_24h, funding_rate
            FROM cex_binance_snapshots
            WHERE timestamp >= ?
            ORDER BY timestamp ASC
            """,
            (since_iso,),
        ).fetchall()
        return list(rows)

    def cleanup_binance_snapshots_before(self, cutoff_iso: str) -> int:
        """Delete old scanner snapshots; returns deleted row count."""
        assert self._conn
        cur = self._conn.execute("DELETE FROM cex_binance_snapshots WHERE timestamp < ?", (cutoff_iso,))
        self._conn.commit()
        return cur.rowcount if cur.rowcount is not None else 0

    def save_binance_symbol_states(self, rows: list[dict[str, Any]]) -> None:
        """Upsert one state row per symbol for incremental scanner metrics."""
        if not rows:
            return
        assert self._conn
        self._conn.executemany(
            """
            INSERT INTO cex_binance_symbol_state (
                symbol, updated_at, first_seen_at, sample_count,
                last_price, last_quote_volume, last_change_24h, last_funding_rate,
                avg_quote_volume, vol_accel, confidence, score,
                chg_1m, chg_5m, chg_1h, chg_4h, chg_1d, chg_1w, chg_3w, chg_1mo, chg_3mo, chg_1y,
                anchor_1m_ts, anchor_5m_ts, anchor_1h_ts, anchor_4h_ts, anchor_1d_ts, anchor_1w_ts, anchor_3w_ts, anchor_1mo_ts, anchor_3mo_ts, anchor_1y_ts,
                anchor_1m_price, anchor_5m_price, anchor_1h_price, anchor_4h_price, anchor_1d_price, anchor_1w_price, anchor_3w_price, anchor_1mo_price, anchor_3mo_price, anchor_1y_price
            ) VALUES (
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(symbol) DO UPDATE SET
                updated_at=excluded.updated_at,
                first_seen_at=excluded.first_seen_at,
                sample_count=excluded.sample_count,
                last_price=excluded.last_price,
                last_quote_volume=excluded.last_quote_volume,
                last_change_24h=excluded.last_change_24h,
                last_funding_rate=excluded.last_funding_rate,
                avg_quote_volume=excluded.avg_quote_volume,
                vol_accel=excluded.vol_accel,
                confidence=excluded.confidence,
                score=excluded.score,
                chg_1m=excluded.chg_1m,
                chg_5m=excluded.chg_5m,
                chg_1h=excluded.chg_1h,
                chg_4h=excluded.chg_4h,
                chg_1d=excluded.chg_1d,
                chg_1w=excluded.chg_1w,
                chg_3w=excluded.chg_3w,
                chg_1mo=excluded.chg_1mo,
                chg_3mo=excluded.chg_3mo,
                chg_1y=excluded.chg_1y,
                anchor_1m_ts=excluded.anchor_1m_ts,
                anchor_5m_ts=excluded.anchor_5m_ts,
                anchor_1h_ts=excluded.anchor_1h_ts,
                anchor_4h_ts=excluded.anchor_4h_ts,
                anchor_1d_ts=excluded.anchor_1d_ts,
                anchor_1w_ts=excluded.anchor_1w_ts,
                anchor_3w_ts=excluded.anchor_3w_ts,
                anchor_1mo_ts=excluded.anchor_1mo_ts,
                anchor_3mo_ts=excluded.anchor_3mo_ts,
                anchor_1y_ts=excluded.anchor_1y_ts,
                anchor_1m_price=excluded.anchor_1m_price,
                anchor_5m_price=excluded.anchor_5m_price,
                anchor_1h_price=excluded.anchor_1h_price,
                anchor_4h_price=excluded.anchor_4h_price,
                anchor_1d_price=excluded.anchor_1d_price,
                anchor_1w_price=excluded.anchor_1w_price,
                anchor_3w_price=excluded.anchor_3w_price,
                anchor_1mo_price=excluded.anchor_1mo_price,
                anchor_3mo_price=excluded.anchor_3mo_price,
                anchor_1y_price=excluded.anchor_1y_price
            """,
            [
                (
                    str(r.get("symbol", "")),
                    str(r.get("updated_at", "")),
                    str(r.get("first_seen_at", "")),
                    int(r.get("sample_count", 0)),
                    float(r.get("last_price", 0.0)),
                    float(r.get("last_quote_volume", 0.0)),
                    float(r.get("last_change_24h", 0.0)),
                    float(r.get("last_funding_rate", 0.0)),
                    float(r.get("avg_quote_volume", 0.0)),
                    float(r.get("vol_accel", 0.0)),
                    float(r.get("confidence", 0.0)),
                    float(r.get("score", 0.0)),
                    float(r.get("chg_1m", 0.0)),
                    float(r.get("chg_5m", 0.0)),
                    float(r.get("chg_1h", 0.0)),
                    float(r.get("chg_4h", 0.0)),
                    float(r.get("chg_1d", 0.0)),
                    float(r.get("chg_1w", 0.0)),
                    float(r.get("chg_3w", 0.0)),
                    float(r.get("chg_1mo", 0.0)),
                    float(r.get("chg_3mo", 0.0)),
                    float(r.get("chg_1y", 0.0)),
                    str(r.get("anchor_1m_ts", "")),
                    str(r.get("anchor_5m_ts", "")),
                    str(r.get("anchor_1h_ts", "")),
                    str(r.get("anchor_4h_ts", "")),
                    str(r.get("anchor_1d_ts", "")),
                    str(r.get("anchor_1w_ts", "")),
                    str(r.get("anchor_3w_ts", "")),
                    str(r.get("anchor_1mo_ts", "")),
                    str(r.get("anchor_3mo_ts", "")),
                    str(r.get("anchor_1y_ts", "")),
                    float(r.get("anchor_1m_price", 0.0)),
                    float(r.get("anchor_5m_price", 0.0)),
                    float(r.get("anchor_1h_price", 0.0)),
                    float(r.get("anchor_4h_price", 0.0)),
                    float(r.get("anchor_1d_price", 0.0)),
                    float(r.get("anchor_1w_price", 0.0)),
                    float(r.get("anchor_3w_price", 0.0)),
                    float(r.get("anchor_1mo_price", 0.0)),
                    float(r.get("anchor_3mo_price", 0.0)),
                    float(r.get("anchor_1y_price", 0.0)),
                )
                for r in rows
            ],
        )
        self._conn.commit()

    def load_binance_symbol_states(self) -> list[Any]:
        """Load all persisted one-row-per-symbol aggregate states."""
        assert self._conn
        rows = self._conn.execute("SELECT * FROM cex_binance_symbol_state ORDER BY symbol ASC").fetchall()
        return list(rows)

    @property
    def conn(self) -> Any | None:
        return self._conn

    # ---- Analytics snapshot persistence ----

    def save_analytics_snapshot(self, snapshot: AnalyticsSnapshot) -> None:
        """Persist one analytics snapshot row (latest row is authoritative)."""
        assert self._conn
        payload = snapshot.model_dump_json()
        self._execute_write_with_lock_retry(
            """
            INSERT INTO analytics_snapshots (snapshot_json, total_trades_logged, updated_at)
            VALUES (?, ?, ?)
            """,
            (
                payload,
                int(snapshot.total_trades_logged or 0),
                str(snapshot.updated_at or datetime.now(UTC).isoformat()),
            ),
        )

    def load_latest_analytics_snapshot(self) -> AnalyticsSnapshot | None:
        """Load latest persisted analytics snapshot from hub.db."""
        assert self._conn
        row = self._conn.execute(
            """
            SELECT snapshot_json
            FROM analytics_snapshots
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
        if not row:
            return None
        raw = str(row["snapshot_json"] or "")
        if not raw:
            return None
        try:
            return AnalyticsSnapshot.model_validate_json(raw)
        except Exception as e:
            logger.warning("Invalid analytics snapshot JSON in hub.db: {}", e)
            return None

    # ---- OpenClaw advisory persistence ----

    def insert_openclaw_daily_report(
        self,
        *,
        report_day: str,
        run_kind: str,
        requested_at: str,
        completed_at: str,
        lane_used: str,
        source_url: str,
        context_payload: dict[str, Any],
        response_payload: dict[str, Any],
        status: str = "ok",
        error_text: str = "",
    ) -> int:
        """Persist one OpenClaw daily optimization run."""
        assert self._conn
        cur = self._conn.execute(
            """
            INSERT INTO openclaw_daily_reports
            (report_day, run_kind, requested_at, completed_at, lane_used, status, source_url, context_json, response_json, error_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report_day,
                run_kind,
                requested_at,
                completed_at,
                lane_used,
                status,
                source_url,
                json.dumps(context_payload, ensure_ascii=True),
                json.dumps(response_payload, ensure_ascii=True),
                error_text,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def get_latest_openclaw_daily_report(self) -> dict[str, Any] | None:
        """Return latest OpenClaw daily report metadata and payload."""
        assert self._conn
        row = self._conn.execute(
            """
            SELECT * FROM openclaw_daily_reports
            ORDER BY completed_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
        if not row:
            return None
        out = dict(row)
        with_data: dict[str, Any] = dict(out)
        for key in ("context_json", "response_json"):
            raw = out.get(key, "{}")
            try:
                with_data[key.replace("_json", "")] = json.loads(raw) if raw else {}
            except Exception:
                with_data[key.replace("_json", "")] = {}
        return with_data

    def get_latest_openclaw_report_completed_at(self) -> str:
        """Return ISO timestamp for latest OpenClaw report completion, or empty."""
        latest = self.get_latest_openclaw_daily_report()
        return str((latest or {}).get("completed_at", "") or "")

    def _build_openclaw_suggestion_key(self, suggestion: dict[str, Any]) -> str:
        strategy = str(suggestion.get("strategy", "") or "").strip().lower()
        symbol = str(suggestion.get("symbol", "") or "").strip().upper()
        s_type = str(suggestion.get("suggestion_type", "") or "").strip().lower()
        title = str(suggestion.get("title", "") or "").strip().lower()
        suggestion_key = str(suggestion.get("suggestion_key", "") or "").strip().lower()
        base = suggestion_key or f"{s_type}|{strategy}|{symbol}|{title}"
        return base[:300]

    def upsert_openclaw_suggestion(self, suggestion: dict[str, Any], *, report_id: int) -> int:
        """Insert or update an OpenClaw suggestion while preserving lifecycle state."""
        assert self._conn
        now_iso = datetime.now(UTC).isoformat()
        skey = self._build_openclaw_suggestion_key(suggestion)
        existing = self._conn.execute(
            "SELECT id, status, implemented_at, removed_at, created_at, first_seen_report_id FROM openclaw_suggestions WHERE suggestion_key=?",
            (skey,),
        ).fetchone()

        payload = {
            "suggestion_type": str(suggestion.get("suggestion_type", "") or ""),
            "title": str(suggestion.get("title", "") or ""),
            "description": str(suggestion.get("description", "") or ""),
            "strategy": str(suggestion.get("strategy", "") or ""),
            "symbol": str(suggestion.get("symbol", "") or ""),
            "confidence": float(suggestion.get("confidence", 0.0) or 0.0),
            "current_value": str(suggestion.get("current_value", "") or ""),
            "suggested_value": str(suggestion.get("suggested_value", "") or ""),
            "expected_improvement": str(suggestion.get("expected_improvement", "") or ""),
            "based_on_trades": int(suggestion.get("based_on_trades", 0) or 0),
        }

        if existing:
            self._conn.execute(
                """
                UPDATE openclaw_suggestions SET
                    source='openclaw',
                    suggestion_type=?,
                    title=?,
                    description=?,
                    strategy=?,
                    symbol=?,
                    confidence=?,
                    current_value=?,
                    suggested_value=?,
                    expected_improvement=?,
                    based_on_trades=?,
                    last_seen_report_id=?,
                    updated_at=?
                WHERE id=?
                """,
                (
                    payload["suggestion_type"],
                    payload["title"],
                    payload["description"],
                    payload["strategy"],
                    payload["symbol"],
                    payload["confidence"],
                    payload["current_value"],
                    payload["suggested_value"],
                    payload["expected_improvement"],
                    payload["based_on_trades"],
                    report_id,
                    now_iso,
                    int(existing["id"]),
                ),
            )
            self._conn.commit()
            return int(existing["id"])

        cur = self._conn.execute(
            """
            INSERT INTO openclaw_suggestions (
                suggestion_key, source, status, suggestion_type, title, description, strategy, symbol,
                confidence, current_value, suggested_value, expected_improvement, based_on_trades, notes,
                first_seen_report_id, last_seen_report_id, created_at, updated_at, implemented_at, removed_at
            ) VALUES (?, 'openclaw', 'new', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, '', '')
            """,
            (
                skey,
                payload["suggestion_type"],
                payload["title"],
                payload["description"],
                payload["strategy"],
                payload["symbol"],
                payload["confidence"],
                payload["current_value"],
                payload["suggested_value"],
                payload["expected_improvement"],
                payload["based_on_trades"],
                report_id,
                report_id,
                now_iso,
                now_iso,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def mark_openclaw_suggestion_status(self, suggestion_id: int, status: str, notes: str = "") -> bool:
        """Transition suggestion lifecycle state (new/accepted/rejected/implemented/removed)."""
        assert self._conn
        if status not in {"new", "accepted", "rejected", "implemented", "removed"}:
            return False
        now_iso = datetime.now(UTC).isoformat()
        implemented_at = now_iso if status == "implemented" else ""
        removed_at = now_iso if status == "removed" else ""
        row = self._conn.execute("SELECT id FROM openclaw_suggestions WHERE id=?", (suggestion_id,)).fetchone()
        if not row:
            return False
        self._conn.execute(
            """
            UPDATE openclaw_suggestions
            SET status=?,
                notes=CASE WHEN ?='' THEN notes ELSE ? END,
                implemented_at=CASE WHEN ?='' THEN implemented_at ELSE ? END,
                removed_at=CASE WHEN ?='' THEN removed_at ELSE ? END,
                updated_at=?
            WHERE id=?
            """,
            (
                status,
                notes,
                notes,
                implemented_at,
                implemented_at,
                removed_at,
                removed_at,
                now_iso,
                suggestion_id,
            ),
        )
        self._conn.commit()
        return True

    def list_openclaw_suggestions(self, *, include_removed: bool = False, limit: int = 200) -> list[dict[str, Any]]:
        """List persisted OpenClaw suggestions for dashboard/API use."""
        assert self._conn
        if include_removed:
            rows = self._conn.execute(
                "SELECT * FROM openclaw_suggestions ORDER BY updated_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM openclaw_suggestions WHERE status != 'removed' ORDER BY updated_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_openclaw_suggestion_context(self, limit: int = 40) -> list[dict[str, Any]]:
        """Return compact historical suggestion context for next OpenClaw runs."""
        assert self._conn
        rows = self._conn.execute(
            """
            SELECT suggestion_key, status, suggestion_type, title, strategy, symbol, suggested_value, updated_at
            FROM openclaw_suggestions
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_openclaw_daily_trade_rollup(self, days: int = 30) -> list[dict[str, Any]]:
        """Compact day-level performance history for OpenClaw context."""
        assert self._conn
        rows = self._conn.execute(
            """
            SELECT
                substr(closed_at, 1, 10) as day,
                COUNT(*) as trades,
                SUM(CASE WHEN is_winner=1 THEN 1 ELSE 0 END) as wins,
                COALESCE(SUM(pnl_usd), 0) as total_pnl_usd,
                COALESCE(AVG(pnl_pct), 0) as avg_pnl_pct
            FROM trades
            WHERE action='close' AND closed_at != '' AND recovery_close=0
            GROUP BY substr(closed_at, 1, 10)
            ORDER BY day DESC
            LIMIT ?
            """,
            (days,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_openclaw_strategy_rollup(self, limit: int = 20) -> list[dict[str, Any]]:
        """Compact strategy-level performance rollup for OpenClaw context."""
        assert self._conn
        rows = self._conn.execute(
            """
            SELECT
                strategy,
                COUNT(*) as trades,
                SUM(CASE WHEN is_winner=1 THEN 1 ELSE 0 END) as wins,
                COALESCE(SUM(pnl_usd), 0) as total_pnl_usd,
                COALESCE(AVG(pnl_pct), 0) as avg_pnl_pct
            FROM trades
            WHERE action='close' AND closed_at != '' AND recovery_close=0
            GROUP BY strategy
            ORDER BY trades DESC, total_pnl_usd ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_openclaw_symbol_rollup(self, limit: int = 20) -> list[dict[str, Any]]:
        """Compact symbol-level performance rollup for OpenClaw context."""
        assert self._conn
        rows = self._conn.execute(
            """
            SELECT
                symbol,
                COUNT(*) as trades,
                SUM(CASE WHEN is_winner=1 THEN 1 ELSE 0 END) as wins,
                COALESCE(SUM(pnl_usd), 0) as total_pnl_usd,
                COALESCE(AVG(pnl_pct), 0) as avg_pnl_pct
            FROM trades
            WHERE action='close' AND closed_at != '' AND recovery_close=0
            GROUP BY symbol
            ORDER BY ABS(COALESCE(SUM(pnl_usd), 0)) DESC, trades DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
