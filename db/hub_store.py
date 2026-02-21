"""Hub-side database: receives trade records and deposit events pushed by bots.

This DB lives only on the hub (dashboard) container at ``data/hub.db``.
Trading bots never touch it directly — they push events via HTTP.

Extends TradeDB with a ``bot_id`` column on trades and a ``deposits`` table.
All read/query methods are inherited from TradeDB.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from loguru import logger

from db.store import TradeDB

_HUB_DB_DEFAULT = Path("data/hub.db")


class HubDB(TradeDB):
    """TradeDB extended with hub-specific tables (bot_id on trades, deposits)."""

    def __init__(self, path: Path = _HUB_DB_DEFAULT):
        super().__init__(path=path)

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

            CREATE INDEX IF NOT EXISTS idx_deposits_bot ON deposits(bot_id);
            CREATE INDEX IF NOT EXISTS idx_deposits_date ON deposits(detected_at);
        """)
        self._ensure_bot_id_column()

    def _ensure_bot_id_column(self) -> None:
        """Add bot_id column to trades table if it doesn't exist yet."""
        assert self._conn
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(trades)").fetchall()}
        if "bot_id" not in cols:
            self._conn.execute("ALTER TABLE trades ADD COLUMN bot_id TEXT DEFAULT ''")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_hub_bot ON trades(bot_id)")
            self._conn.commit()

    # ---- Trade ingestion (hub-specific) ----

    def insert_trade(self, bot_id: str, trade: dict[str, Any]) -> int:
        """Insert a trade record pushed by a bot. Returns the new row id."""
        assert self._conn
        cursor = self._conn.execute(
            """INSERT INTO trades (
                bot_id, symbol, side, strategy, action, scale_mode,
                entry_price, exit_price, amount, leverage,
                pnl_usd, pnl_pct, is_winner, hold_minutes,
                was_quick_trade, was_low_liquidity, dca_count, max_drawdown_pct,
                market_regime, fear_greed, daily_tier, daily_pnl_at_entry,
                signal_strength, hour_utc, day_of_week, volatility_pct,
                opened_at, closed_at
            ) VALUES (?,?,?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?)""",
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
            ),
        )
        self._conn.commit()
        return cursor.lastrowid or 0

    def update_trade_close(self, bot_id: str, opened_at: str, data: dict[str, Any]) -> bool:
        """Update an open trade row with exit data (matched by bot_id + opened_at)."""
        assert self._conn
        cursor = self._conn.execute(
            """UPDATE trades SET
                action='close', exit_price=?, amount=?, leverage=?,
                pnl_usd=?, pnl_pct=?, is_winner=?,
                hold_minutes=?, dca_count=?, max_drawdown_pct=?,
                closed_at=?
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
                bot_id,
                opened_at,
            ),
        )
        self._conn.commit()
        return cursor.rowcount > 0

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

    @property
    def conn(self) -> sqlite3.Connection | None:
        return self._conn
