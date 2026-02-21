"""Hub-side database: receives trade records and deposit events pushed by bots.

This DB lives only on the hub (dashboard) container at ``data/hub.db``.
Trading bots never touch it directly — they push events via HTTP and
query open positions / stats via hub API endpoints.

Extends TradeDB with ``bot_id``, ``request_key`` (idempotency),
``recovery_close`` columns on trades, and a ``deposits`` table.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from loguru import logger

from db.models import TradeRecord
from db.store import TradeDB

_HUB_DB_DEFAULT = Path("data/hub.db")


class HubDB(TradeDB):
    """TradeDB extended with hub-specific tables and bot-centric queries."""

    def __init__(self, path: Path = _HUB_DB_DEFAULT):
        super().__init__(path=path)
        self._ack_buffer: dict[str, set[str]] = {}

    def connect(self) -> None:
        self._conn = sqlite3.connect(str(self._path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._create_tables()
        self._create_hub_tables()
        logger.info("HubDB connected: {}", self._path)

    def _create_hub_tables(self) -> None:
        assert self._conn
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS deposits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id TEXT NOT NULL,
                amount REAL NOT NULL,
                exchange TEXT DEFAULT '',
                detected_at TEXT NOT NULL,
                balance_before REAL DEFAULT 0,
                balance_after REAL DEFAULT 0,
                notes TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS bot_config (
                bot_id TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1
            );

            CREATE INDEX IF NOT EXISTS idx_deposits_bot ON deposits(bot_id);
            CREATE INDEX IF NOT EXISTS idx_deposits_date ON deposits(detected_at);
        """)
        self._ensure_bot_id_column()
        self._ensure_request_key_column()
        self._ensure_recovery_close_column()

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

    # ---- Trade ingestion (hub-specific) ----

    def insert_trade(self, bot_id: str, trade: dict[str, Any], request_key: str = "") -> int:
        """Insert a trade record pushed by a bot. Deduplicates by request_key."""
        assert self._conn
        if request_key:
            existing = self._conn.execute("SELECT id FROM trades WHERE request_key = ?", (request_key,)).fetchone()
            if existing:
                self._mark_confirmed(bot_id, request_key)
                return int(existing["id"])

        cursor = self._conn.execute(
            """INSERT INTO trades (
                bot_id, symbol, side, strategy, action, scale_mode,
                entry_price, exit_price, amount, leverage,
                pnl_usd, pnl_pct, is_winner, hold_minutes,
                was_quick_trade, was_low_liquidity, dca_count, max_drawdown_pct,
                market_regime, fear_greed, daily_tier, daily_pnl_at_entry,
                signal_strength, hour_utc, day_of_week, volatility_pct,
                opened_at, closed_at, request_key, recovery_close
            ) VALUES (?,?,?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?)""",
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
                request_key,
                int(trade.get("recovery_close", False)),
            ),
        )
        self._conn.commit()
        row_id = cursor.lastrowid or 0
        if request_key:
            self._mark_confirmed(bot_id, request_key)
        return row_id

    def update_trade_close(self, bot_id: str, opened_at: str, data: dict[str, Any], request_key: str = "") -> bool:
        """Update an open trade row with exit data (matched by bot_id + opened_at)."""
        assert self._conn
        if request_key:
            existing = self._conn.execute("SELECT id FROM trades WHERE request_key = ?", (request_key,)).fetchone()
            if existing:
                self._mark_confirmed(bot_id, request_key)
                return True

        cursor = self._conn.execute(
            """UPDATE trades SET
                action='close', exit_price=?, amount=?, leverage=?,
                pnl_usd=?, pnl_pct=?, is_winner=?,
                hold_minutes=?, dca_count=?, max_drawdown_pct=?,
                closed_at=?, request_key=CASE WHEN ?='' THEN request_key ELSE ? END
            WHERE bot_id=? AND opened_at=? AND closed_at=''""",
            (
                data.get("exit_price", 0),
                data.get("amount", 0),
                data.get("leverage", 1),
                data.get("pnl_usd", 0),
                data.get("pnl_pct", 0),
                int(data.get("is_winner", False)),
                data.get("hold_minutes", 0),
                data.get("dca_count", 0),
                data.get("max_drawdown_pct", 0),
                data.get("closed_at", ""),
                request_key,
                request_key,
                bot_id,
                opened_at,
            ),
        )
        self._conn.commit()
        updated = cursor.rowcount > 0
        if updated and request_key:
            self._mark_confirmed(bot_id, request_key)
        return updated

    def mark_recovery_close(self, bot_id: str, opened_at: str) -> bool:
        """Mark an open trade as closed due to bot recovery (no exit stats)."""
        assert self._conn
        cursor = self._conn.execute(
            """UPDATE trades SET
                action='close', recovery_close=1, closed_at=?
            WHERE bot_id=? AND opened_at=? AND closed_at=''""",
            (opened_at, bot_id, opened_at),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    # ---- Bot-centric queries ----

    def get_open_trades_for_bot(self, bot_id: str) -> list[TradeRecord]:
        """Return all unclosed trades for a specific bot."""
        assert self._conn
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE bot_id=? AND closed_at='' ORDER BY id",
            (bot_id,),
        ).fetchall()
        return [self._row_to_trade(r) for r in rows]

    def get_strategy_stats_for_bot(self, bot_id: str, strategy: str, symbol: str = "") -> dict[str, Any]:
        """Strategy stats scoped to a single bot (excludes recovery_close trades)."""
        assert self._conn
        where = "bot_id=? AND strategy=? AND recovery_close=0"
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
            WHERE bot_id=? AND recovery_close=0
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

    # ---- Deposit ingestion ----

    def insert_deposit(
        self,
        bot_id: str,
        amount: float,
        exchange: str,
        detected_at: str,
        balance_before: float = 0,
        balance_after: float = 0,
        notes: str = "",
    ) -> int:
        assert self._conn
        cursor = self._conn.execute(
            """INSERT INTO deposits (bot_id, amount, exchange, detected_at,
               balance_before, balance_after, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (bot_id, amount, exchange, detected_at, balance_before, balance_after, notes),
        )
        self._conn.commit()
        return cursor.lastrowid or 0

    def get_deposits(self, limit: int = 100) -> list[dict[str, Any]]:
        assert self._conn
        rows = self._conn.execute("SELECT * FROM deposits ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ---- Bot enable/disable ----

    def set_bot_enabled(self, bot_id: str, enabled: bool) -> None:
        assert self._conn
        self._conn.execute(
            "INSERT INTO bot_config (bot_id, enabled) VALUES (?, ?) "
            "ON CONFLICT(bot_id) DO UPDATE SET enabled=excluded.enabled",
            (bot_id, int(enabled)),
        )
        self._conn.commit()

    def is_bot_enabled(self, bot_id: str) -> bool:
        assert self._conn
        row = self._conn.execute("SELECT enabled FROM bot_config WHERE bot_id=?", (bot_id,)).fetchone()
        return bool(row["enabled"]) if row else True

    def get_all_bot_enabled(self) -> dict[str, bool]:
        assert self._conn
        rows = self._conn.execute("SELECT bot_id, enabled FROM bot_config").fetchall()
        return {r["bot_id"]: bool(r["enabled"]) for r in rows}

    @property
    def conn(self) -> sqlite3.Connection | None:
        return self._conn
