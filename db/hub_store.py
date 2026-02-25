"""Hub-side database: receives trade records and deposit events pushed by bots.

This DB lives only on the hub (dashboard) container at ``data/hub.db``.
Trading bots never touch it directly — they push events via HTTP and
query open positions / stats via hub API endpoints.

Extends TradeDB with ``bot_id``, ``request_key`` (idempotency),
``recovery_close`` columns on trades, and a ``deposits`` table.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
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

        try:
            cursor = self._conn.execute(
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
            self._conn.commit()
        except sqlite3.IntegrityError:
            # Idempotent race: another request committed this key first.
            if request_key:
                existing = self._conn.execute("SELECT id FROM trades WHERE request_key = ?", (request_key,)).fetchone()
                if existing:
                    self._mark_confirmed(bot_id, request_key)
                    return int(existing["id"])
                logger.warning(
                    "insert_trade dedup race unresolved for request_key={} (bot_id={}) — accepting as idempotent",
                    request_key,
                    bot_id,
                )
                self._mark_confirmed(bot_id, request_key)
                return 0
            raise
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

        try:
            cursor = self._conn.execute(
                """UPDATE trades SET
                action='close', exit_price=?, amount=?, leverage=?,
                pnl_usd=?, pnl_pct=?, is_winner=?,
                hold_minutes=?, dca_count=?, max_drawdown_pct=?,
                closed_at=?,
                effective_stop_loss=?, effective_take_profit=?,
                stop_source=?, tp_source=?,
                close_source=?, close_reason=?,
                exchange_close_order_id=?, exchange_close_trade_id=?,
                close_detected_at=?,
                request_key=CASE WHEN ?='' THEN request_key ELSE ? END
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
                    data.get("effective_stop_loss", 0),
                    data.get("effective_take_profit", 0),
                    data.get("stop_source", "none"),
                    data.get("tp_source", "none"),
                    data.get("close_source", ""),
                    data.get("close_reason", ""),
                    data.get("exchange_close_order_id", ""),
                    data.get("exchange_close_trade_id", ""),
                    data.get("close_detected_at", ""),
                    request_key,
                    request_key,
                    bot_id,
                    opened_at,
                ),
            )
            self._conn.commit()
        except sqlite3.IntegrityError:
            if request_key:
                existing = self._conn.execute("SELECT id FROM trades WHERE request_key = ?", (request_key,)).fetchone()
                if existing:
                    self._mark_confirmed(bot_id, request_key)
                    return True
                logger.warning(
                    "update_trade_close dedup race unresolved for request_key={} (bot_id={}, opened_at={}) — accepting as idempotent",
                    request_key,
                    bot_id,
                    opened_at,
                )
                self._mark_confirmed(bot_id, request_key)
                return True
            raise
        updated = cursor.rowcount > 0
        if updated and request_key:
            self._mark_confirmed(bot_id, request_key)
        return updated

    def update_trade_runtime(self, bot_id: str, opened_at: str, data: dict[str, Any], request_key: str = "") -> bool:
        """Update runtime SL/TP fields for an open trade row."""
        assert self._conn
        if request_key:
            existing = self._conn.execute("SELECT id FROM trades WHERE request_key = ?", (request_key,)).fetchone()
            if existing:
                self._mark_confirmed(bot_id, request_key)
                return True

        try:
            cursor = self._conn.execute(
                """UPDATE trades SET
                planned_stop_loss=?,
                planned_tp1=?,
                planned_tp2=?,
                exchange_stop_loss=?,
                exchange_take_profit=?,
                bot_stop_loss=?,
                bot_take_profit=?,
                effective_stop_loss=?,
                effective_take_profit=?,
                stop_source=?,
                tp_source=?,
                request_key=CASE WHEN ?='' THEN request_key ELSE ? END
            WHERE bot_id=? AND opened_at=? AND closed_at=''""",
                (
                    data.get("planned_stop_loss", 0),
                    data.get("planned_tp1", 0),
                    data.get("planned_tp2", 0),
                    data.get("exchange_stop_loss", 0),
                    data.get("exchange_take_profit", 0),
                    data.get("bot_stop_loss", 0),
                    data.get("bot_take_profit", 0),
                    data.get("effective_stop_loss", 0),
                    data.get("effective_take_profit", 0),
                    data.get("stop_source", "none"),
                    data.get("tp_source", "none"),
                    request_key,
                    request_key,
                    bot_id,
                    opened_at,
                ),
            )
            self._conn.commit()
        except sqlite3.IntegrityError:
            if request_key:
                existing = self._conn.execute("SELECT id FROM trades WHERE request_key = ?", (request_key,)).fetchone()
                if existing:
                    self._mark_confirmed(bot_id, request_key)
                    return True
                logger.warning(
                    "update_trade_runtime dedup race unresolved for request_key={} (bot_id={}, opened_at={}) — accepting as idempotent",
                    request_key,
                    bot_id,
                    opened_at,
                )
                self._mark_confirmed(bot_id, request_key)
                return True
            raise
        updated = cursor.rowcount > 0
        if updated and request_key:
            self._mark_confirmed(bot_id, request_key)
        return updated

    def mark_recovery_close(self, bot_id: str, opened_at: str) -> bool:
        """Mark an open trade as closed due to bot recovery (no exit stats)."""
        assert self._conn
        closed_at = datetime.now(UTC).isoformat()
        cursor = self._conn.execute(
            """UPDATE trades SET
                action='close', recovery_close=1, closed_at=?,
                close_source='recovery', close_reason='missing_on_exchange'
            WHERE bot_id=? AND opened_at=? AND closed_at=''""",
            (closed_at, bot_id, opened_at),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    # ---- Bot-centric queries ----

    def get_open_trade_symbols(self) -> set[str]:
        """Return the set of symbols with at least one unclosed trade (any bot)."""
        assert self._conn
        rows = self._conn.execute("SELECT DISTINCT symbol FROM trades WHERE closed_at=''").fetchall()
        return {r["symbol"] for r in rows}

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
            except Exception:
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

    def load_binance_snapshots_since(self, since_iso: str) -> list[sqlite3.Row]:
        """Load scanner snapshots since a given ISO timestamp."""
        assert self._conn
        return self._conn.execute(
            """
            SELECT timestamp, symbol, price, quote_volume, change_24h, funding_rate
            FROM cex_binance_snapshots
            WHERE timestamp >= ?
            ORDER BY timestamp ASC
            """,
            (since_iso,),
        ).fetchall()

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

    def load_binance_symbol_states(self) -> list[sqlite3.Row]:
        """Load all persisted one-row-per-symbol aggregate states."""
        assert self._conn
        return self._conn.execute("SELECT * FROM cex_binance_symbol_state ORDER BY symbol ASC").fetchall()

    @property
    def conn(self) -> sqlite3.Connection | None:
        return self._conn

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
            ORDER BY ABS(total_pnl_usd) DESC, trades DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
