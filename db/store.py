from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from db import local_db as ldb
from db.models import TradeRecord

DB_PATH = Path("data/trades.db")
_ANALYTICS_EXCLUDED_CLOSE_SOURCES = ("reservation_cancel", "recovery")
_ANALYTICS_EXCLUDED_CLOSE_REASONS = ("risk_or_gate", "open_exception", "failed_fill:pending")
_ANALYTICS_EXCLUDED_STRATEGIES = (
    "risk_manager",
    "manual_override",
    "stop",
    "manual_claim",
    "runtime_recovered",
    "unknown",
)
_ANALYTICS_CLOSE_SOURCE_PH = ", ".join("?" for _ in _ANALYTICS_EXCLUDED_CLOSE_SOURCES)
_ANALYTICS_CLOSE_REASON_PH = ", ".join("?" for _ in _ANALYTICS_EXCLUDED_CLOSE_REASONS)
_ANALYTICS_STRATEGY_PH = ", ".join("?" for _ in _ANALYTICS_EXCLUDED_STRATEGIES)


class TradeDB:
    """Local file-backed trade history for analytics and pattern detection."""

    def __init__(self, path: Path = DB_PATH):
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Any | None = None

    def connect(self) -> None:
        self._conn = ldb.connect(str(self._path))
        self._conn.row_factory = ldb.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._create_tables()
        self._ensure_trade_columns()
        logger.info("TradeDB connected: {}", self._path)

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def _create_tables(self) -> None:
        assert self._conn
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                strategy TEXT NOT NULL,
                action TEXT NOT NULL,
                scale_mode TEXT DEFAULT '',
                entry_price REAL DEFAULT 0,
                exit_price REAL DEFAULT 0,
                amount REAL DEFAULT 0,
                leverage INTEGER DEFAULT 1,
                pnl_usd REAL DEFAULT 0,
                pnl_pct REAL DEFAULT 0,
                is_winner INTEGER DEFAULT 0,
                hold_minutes REAL DEFAULT 0,
                was_quick_trade INTEGER DEFAULT 0,
                was_low_liquidity INTEGER DEFAULT 0,
                dca_count INTEGER DEFAULT 0,
                max_drawdown_pct REAL DEFAULT 0,
                market_regime TEXT DEFAULT '',
                fear_greed INTEGER DEFAULT 50,
                daily_tier TEXT DEFAULT '',
                daily_pnl_at_entry REAL DEFAULT 0,
                signal_strength REAL DEFAULT 0,
                hour_utc INTEGER DEFAULT 0,
                day_of_week INTEGER DEFAULT 0,
                volatility_pct REAL DEFAULT 0,
                opened_at TEXT DEFAULT '',
                closed_at TEXT DEFAULT '',
                planned_stop_loss REAL DEFAULT 0,
                planned_tp1 REAL DEFAULT 0,
                planned_tp2 REAL DEFAULT 0,
                exchange_stop_loss REAL DEFAULT 0,
                exchange_take_profit REAL DEFAULT 0,
                bot_stop_loss REAL DEFAULT 0,
                bot_take_profit REAL DEFAULT 0,
                effective_stop_loss REAL DEFAULT 0,
                effective_take_profit REAL DEFAULT 0,
                stop_source TEXT DEFAULT 'none',
                tp_source TEXT DEFAULT 'none',
                close_source TEXT DEFAULT '',
                close_reason TEXT DEFAULT '',
                exchange_close_order_id TEXT DEFAULT '',
                exchange_close_trade_id TEXT DEFAULT '',
                close_detected_at TEXT DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);
            CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
            CREATE INDEX IF NOT EXISTS idx_trades_closed ON trades(closed_at);
            CREATE INDEX IF NOT EXISTS idx_trades_winner ON trades(is_winner);
        """)

    def _ensure_trade_columns(self) -> None:
        assert self._conn
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(trades)").fetchall()}
        additions: dict[str, str] = {
            "planned_stop_loss": "REAL DEFAULT 0",
            "planned_tp1": "REAL DEFAULT 0",
            "planned_tp2": "REAL DEFAULT 0",
            "exchange_stop_loss": "REAL DEFAULT 0",
            "exchange_take_profit": "REAL DEFAULT 0",
            "bot_stop_loss": "REAL DEFAULT 0",
            "bot_take_profit": "REAL DEFAULT 0",
            "effective_stop_loss": "REAL DEFAULT 0",
            "effective_take_profit": "REAL DEFAULT 0",
            "stop_source": "TEXT DEFAULT 'none'",
            "tp_source": "TEXT DEFAULT 'none'",
            "close_source": "TEXT DEFAULT ''",
            "close_reason": "TEXT DEFAULT ''",
            "exchange_close_order_id": "TEXT DEFAULT ''",
            "exchange_close_trade_id": "TEXT DEFAULT ''",
            "close_detected_at": "TEXT DEFAULT ''",
        }
        altered = False
        for name, ddl in additions.items():
            if name in cols:
                continue
            self._conn.execute(f"ALTER TABLE trades ADD COLUMN {name} {ddl}")
            altered = True
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_close_source ON trades(close_source)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol_closed ON trades(symbol, closed_at)")
        if altered:
            self._conn.commit()

    def log_trade(self, trade: TradeRecord) -> int:
        assert self._conn
        cursor = self._conn.execute(
            """
            INSERT INTO trades (
                symbol, side, strategy, action, scale_mode,
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
                exchange_close_order_id, exchange_close_trade_id, close_detected_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                trade.symbol,
                trade.side,
                trade.strategy,
                trade.action,
                trade.scale_mode,
                trade.entry_price,
                trade.exit_price,
                trade.amount,
                trade.leverage,
                trade.pnl_usd,
                trade.pnl_pct,
                int(trade.is_winner),
                trade.hold_minutes,
                int(trade.was_quick_trade),
                int(trade.was_low_liquidity),
                trade.dca_count,
                trade.max_drawdown_pct,
                trade.market_regime,
                trade.fear_greed,
                trade.daily_tier,
                trade.daily_pnl_at_entry,
                trade.signal_strength,
                trade.hour_utc,
                trade.day_of_week,
                trade.volatility_pct,
                trade.opened_at,
                trade.closed_at,
                trade.planned_stop_loss,
                trade.planned_tp1,
                trade.planned_tp2,
                trade.exchange_stop_loss,
                trade.exchange_take_profit,
                trade.bot_stop_loss,
                trade.bot_take_profit,
                trade.effective_stop_loss,
                trade.effective_take_profit,
                trade.stop_source,
                trade.tp_source,
                trade.close_source,
                trade.close_reason,
                trade.exchange_close_order_id,
                trade.exchange_close_trade_id,
                trade.close_detected_at,
            ),
        )
        self._conn.commit()
        return cursor.lastrowid or 0

    def open_trade(self, trade: TradeRecord) -> int:
        """Insert a row when a position is first opened (no exit data yet)."""
        return self.log_trade(trade)

    def close_trade(self, trade_id: int, record: TradeRecord) -> None:
        """Update an existing open-trade row with exit data."""
        assert self._conn
        self._conn.execute(
            """
            UPDATE trades SET
                exit_price = ?, amount = ?, leverage = ?,
                pnl_usd = ?, pnl_pct = ?, is_winner = ?,
                hold_minutes = ?, dca_count = ?, max_drawdown_pct = ?,
                action = 'close', closed_at = ?,
                effective_stop_loss = ?, effective_take_profit = ?,
                stop_source = ?, tp_source = ?,
                close_source = ?, close_reason = ?,
                exchange_close_order_id = ?, exchange_close_trade_id = ?,
                close_detected_at = ?
            WHERE id = ?
        """,
            (
                record.exit_price,
                record.amount,
                record.leverage,
                record.pnl_usd,
                record.pnl_pct,
                int(record.is_winner),
                record.hold_minutes,
                record.dca_count,
                record.max_drawdown_pct,
                record.closed_at,
                record.effective_stop_loss,
                record.effective_take_profit,
                record.stop_source,
                record.tp_source,
                record.close_source,
                record.close_reason,
                record.exchange_close_order_id,
                record.exchange_close_trade_id,
                record.close_detected_at,
                trade_id,
            ),
        )
        self._conn.commit()

    def find_open_trade(self, symbol: str) -> TradeRecord | None:
        """Find the most recent unclosed trade for a symbol."""
        assert self._conn
        row = self._conn.execute(
            "SELECT * FROM trades WHERE symbol = ? AND closed_at = '' ORDER BY id DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        return self._row_to_trade(row) if row else None

    def get_all_trades(self, limit: int = 500) -> list[TradeRecord]:
        assert self._conn
        rows = self._conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row_to_trade(r) for r in rows]

    def get_analytics_trades(self, limit: int = 500) -> list[TradeRecord]:
        """Return close-trade records suitable for analytics scoring/patterns."""
        assert self._conn
        rows = self._conn.execute(
            f"""
            SELECT * FROM trades
            WHERE action='close'
              AND close_source NOT IN ({_ANALYTICS_CLOSE_SOURCE_PH})
              AND close_reason NOT IN ({_ANALYTICS_CLOSE_REASON_PH})
              AND strategy NOT IN ({_ANALYTICS_STRATEGY_PH})
            ORDER BY id DESC LIMIT ?
            """,
            (
                *_ANALYTICS_EXCLUDED_CLOSE_SOURCES,
                *_ANALYTICS_EXCLUDED_CLOSE_REASONS,
                *_ANALYTICS_EXCLUDED_STRATEGIES,
                limit,
            ),
        ).fetchall()
        return [self._row_to_trade(r) for r in rows]

    def get_trades_by_strategy(self, strategy: str, limit: int = 200) -> list[TradeRecord]:
        assert self._conn
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE strategy = ? ORDER BY id DESC LIMIT ?",
            (strategy, limit),
        ).fetchall()
        return [self._row_to_trade(r) for r in rows]

    def get_trades_by_symbol(self, symbol: str, limit: int = 200) -> list[TradeRecord]:
        assert self._conn
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE symbol = ? ORDER BY id DESC LIMIT ?",
            (symbol, limit),
        ).fetchall()
        return [self._row_to_trade(r) for r in rows]

    def get_losing_trades(self, limit: int = 200) -> list[TradeRecord]:
        assert self._conn
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE is_winner = 0 AND pnl_usd != 0 ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_trade(r) for r in rows]

    def get_strategy_names(self) -> list[str]:
        assert self._conn
        rows = self._conn.execute(
            f"""
            SELECT DISTINCT strategy
            FROM trades
            WHERE action='close'
              AND close_source NOT IN ({_ANALYTICS_CLOSE_SOURCE_PH})
              AND close_reason NOT IN ({_ANALYTICS_CLOSE_REASON_PH})
              AND strategy NOT IN ({_ANALYTICS_STRATEGY_PH})
            ORDER BY strategy
            """,
            (
                *_ANALYTICS_EXCLUDED_CLOSE_SOURCES,
                *_ANALYTICS_EXCLUDED_CLOSE_REASONS,
                *_ANALYTICS_EXCLUDED_STRATEGIES,
            ),
        ).fetchall()
        return [r["strategy"] for r in rows]

    def get_strategy_stats(self, strategy: str, symbol: str = "") -> dict[str, Any]:
        assert self._conn
        where = (
            "strategy = ? AND action = 'close' "
            f"AND close_source NOT IN ({_ANALYTICS_CLOSE_SOURCE_PH}) "
            f"AND close_reason NOT IN ({_ANALYTICS_CLOSE_REASON_PH}) "
            f"AND strategy NOT IN ({_ANALYTICS_STRATEGY_PH})"
        )
        params: list[str] = [strategy]
        params.extend(
            [
                *_ANALYTICS_EXCLUDED_CLOSE_SOURCES,
                *_ANALYTICS_EXCLUDED_CLOSE_REASONS,
                *_ANALYTICS_EXCLUDED_STRATEGIES,
            ]
        )
        if symbol:
            where += " AND symbol = ?"
            params.append(symbol)

        row = self._conn.execute(
            f"""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN is_winner = 1 THEN 1 ELSE 0 END) as winners,
                SUM(CASE WHEN is_winner = 0 AND pnl_usd != 0 THEN 1 ELSE 0 END) as losers,
                AVG(CASE WHEN is_winner = 1 THEN pnl_pct END) as avg_win,
                AVG(CASE WHEN is_winner = 0 AND pnl_usd != 0 THEN pnl_pct END) as avg_loss,
                SUM(pnl_usd) as total_pnl,
                SUM(CASE WHEN is_winner = 1 THEN pnl_usd ELSE 0 END) as gross_profit,
                SUM(CASE WHEN is_winner = 0 THEN ABS(pnl_usd) ELSE 0 END) as gross_loss,
                AVG(hold_minutes) as avg_hold
            FROM trades WHERE {where}
        """,
            params,
        ).fetchone()

        if not row:
            return {}
        result = dict(row)
        for key in ("avg_win", "avg_loss", "avg_hold", "total_pnl", "gross_profit", "gross_loss"):
            if result.get(key) is None:
                result[key] = 0.0
        return result

    def get_hourly_performance(self, strategy: str = "") -> list[dict[str, Any]]:
        assert self._conn
        where = (
            "WHERE action='close' "
            f"AND close_source NOT IN ({_ANALYTICS_CLOSE_SOURCE_PH}) "
            f"AND close_reason NOT IN ({_ANALYTICS_CLOSE_REASON_PH}) "
            f"AND strategy NOT IN ({_ANALYTICS_STRATEGY_PH})"
        )
        params: list[Any] = [
            *_ANALYTICS_EXCLUDED_CLOSE_SOURCES,
            *_ANALYTICS_EXCLUDED_CLOSE_REASONS,
            *_ANALYTICS_EXCLUDED_STRATEGIES,
        ]
        if strategy:
            where += " AND strategy = ?"
            params.append(strategy)
        rows = self._conn.execute(
            f"""
            SELECT
                hour_utc,
                COUNT(*) as trades,
                SUM(CASE WHEN is_winner = 1 THEN 1 ELSE 0 END) as wins,
                AVG(pnl_pct) as avg_pnl,
                SUM(pnl_usd) as total_pnl
            FROM trades {where}
            GROUP BY hour_utc ORDER BY hour_utc
        """,
            params,
        ).fetchall()
        result = []
        for r in rows:
            row_dict = dict(r)
            for key in ("avg_pnl", "total_pnl"):
                if row_dict.get(key) is None:
                    row_dict[key] = 0.0
            result.append(row_dict)
        return result

    def get_regime_performance(self, strategy: str = "") -> list[dict[str, Any]]:
        assert self._conn
        where = (
            "WHERE action='close' AND market_regime != '' "
            f"AND close_source NOT IN ({_ANALYTICS_CLOSE_SOURCE_PH}) "
            f"AND close_reason NOT IN ({_ANALYTICS_CLOSE_REASON_PH}) "
            f"AND strategy NOT IN ({_ANALYTICS_STRATEGY_PH})"
        )
        params: list[Any] = [
            *_ANALYTICS_EXCLUDED_CLOSE_SOURCES,
            *_ANALYTICS_EXCLUDED_CLOSE_REASONS,
            *_ANALYTICS_EXCLUDED_STRATEGIES,
        ]
        if strategy:
            where += " AND strategy = ?"
            params.append(strategy)
        rows = self._conn.execute(
            f"""
            SELECT
                market_regime,
                COUNT(*) as trades,
                SUM(CASE WHEN is_winner = 1 THEN 1 ELSE 0 END) as wins,
                AVG(pnl_pct) as avg_pnl,
                SUM(pnl_usd) as total_pnl
            FROM trades {where}
            GROUP BY market_regime ORDER BY total_pnl DESC
        """,
            params,
        ).fetchall()
        result = []
        for r in rows:
            row_dict = dict(r)
            for key in ("avg_pnl", "total_pnl"):
                if row_dict.get(key) is None:
                    row_dict[key] = 0.0
            result.append(row_dict)
        return result

    def get_recent_streak(self, strategy: str) -> int:
        """Positive = consecutive wins, negative = consecutive losses."""
        assert self._conn
        rows = self._conn.execute(
            f"""
            SELECT is_winner FROM trades
            WHERE strategy = ?
              AND pnl_usd != 0
              AND action='close'
              AND close_source NOT IN ({_ANALYTICS_CLOSE_SOURCE_PH})
              AND close_reason NOT IN ({_ANALYTICS_CLOSE_REASON_PH})
              AND strategy NOT IN ({_ANALYTICS_STRATEGY_PH})
            ORDER BY id DESC LIMIT 20
            """,
            (
                strategy,
                *_ANALYTICS_EXCLUDED_CLOSE_SOURCES,
                *_ANALYTICS_EXCLUDED_CLOSE_REASONS,
                *_ANALYTICS_EXCLUDED_STRATEGIES,
            ),
        ).fetchall()
        if not rows:
            return 0
        first = rows[0]["is_winner"]
        streak = 0
        for r in rows:
            if r["is_winner"] == first:
                streak += 1
            else:
                break
        return streak if first else -streak

    def get_max_loss_streak(self, strategy: str) -> int:
        assert self._conn
        rows = self._conn.execute(
            f"""
            SELECT is_winner FROM trades
            WHERE strategy = ?
              AND pnl_usd != 0
              AND action='close'
              AND close_source NOT IN ({_ANALYTICS_CLOSE_SOURCE_PH})
              AND close_reason NOT IN ({_ANALYTICS_CLOSE_REASON_PH})
              AND strategy NOT IN ({_ANALYTICS_STRATEGY_PH})
            ORDER BY id
            """,
            (
                strategy,
                *_ANALYTICS_EXCLUDED_CLOSE_SOURCES,
                *_ANALYTICS_EXCLUDED_CLOSE_REASONS,
                *_ANALYTICS_EXCLUDED_STRATEGIES,
            ),
        ).fetchall()
        max_streak = 0
        current = 0
        for r in rows:
            if not r["is_winner"]:
                current += 1
                max_streak = max(max_streak, current)
            else:
                current = 0
        return max_streak

    def trade_count(self) -> int:
        assert self._conn
        row = self._conn.execute("SELECT COUNT(*) as c FROM trades").fetchone()
        return row["c"] if row else 0

    @staticmethod
    def _row_to_trade(row: Any) -> TradeRecord:
        """Build TradeRecord from row; coerce NULLs to type-safe defaults."""

        def _str(v: Any) -> str:
            return str(v) if v is not None else ""

        def _float(v: Any) -> float:
            return float(v) if v is not None else 0.0

        def _int(v: Any) -> int:
            return int(v) if v is not None else 0

        return TradeRecord(
            id=_int(row["id"]),
            symbol=_str(row["symbol"]),
            side=_str(row["side"]),
            strategy=_str(row["strategy"]),
            action=_str(row["action"]),
            scale_mode=_str(row["scale_mode"]),
            entry_price=_float(row["entry_price"]),
            exit_price=_float(row["exit_price"]),
            amount=_float(row["amount"]),
            leverage=_int(row["leverage"]),
            pnl_usd=_float(row["pnl_usd"]),
            pnl_pct=_float(row["pnl_pct"]),
            is_winner=bool(row["is_winner"]),
            hold_minutes=_float(row["hold_minutes"]),
            was_quick_trade=bool(row["was_quick_trade"]),
            was_low_liquidity=bool(row["was_low_liquidity"]),
            dca_count=_int(row["dca_count"]),
            max_drawdown_pct=_float(row["max_drawdown_pct"]),
            market_regime=_str(row["market_regime"]),
            fear_greed=_int(row["fear_greed"]),
            daily_tier=_str(row["daily_tier"]),
            daily_pnl_at_entry=_float(row["daily_pnl_at_entry"]),
            signal_strength=_float(row["signal_strength"]),
            hour_utc=_int(row["hour_utc"]),
            day_of_week=_int(row["day_of_week"]),
            volatility_pct=_float(row["volatility_pct"]),
            opened_at=_str(row["opened_at"]),
            closed_at=_str(row["closed_at"]),
            planned_stop_loss=_float(row["planned_stop_loss"]),
            planned_tp1=_float(row["planned_tp1"]),
            planned_tp2=_float(row["planned_tp2"]),
            exchange_stop_loss=_float(row["exchange_stop_loss"]),
            exchange_take_profit=_float(row["exchange_take_profit"]),
            bot_stop_loss=_float(row["bot_stop_loss"]),
            bot_take_profit=_float(row["bot_take_profit"]),
            effective_stop_loss=_float(row["effective_stop_loss"]),
            effective_take_profit=_float(row["effective_take_profit"]),
            stop_source=_str(row["stop_source"]),
            tp_source=_str(row["tp_source"]),
            close_source=_str(row["close_source"]),
            close_reason=_str(row["close_reason"]),
            exchange_close_order_id=_str(row["exchange_close_order_id"]),
            exchange_close_trade_id=_str(row["exchange_close_trade_id"]),
            close_detected_at=_str(row["close_detected_at"]),
        )
