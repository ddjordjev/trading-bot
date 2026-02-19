from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from loguru import logger

from db.models import TradeRecord

DB_PATH = Path("data/trades.db")


class TradeDB:
    """SQLite-backed trade history for analytics and pattern detection."""

    def __init__(self, path: Path = DB_PATH):
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        self._conn = sqlite3.connect(str(self._path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()
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
                closed_at TEXT DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);
            CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
            CREATE INDEX IF NOT EXISTS idx_trades_closed ON trades(closed_at);
            CREATE INDEX IF NOT EXISTS idx_trades_winner ON trades(is_winner);
        """)

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
                opened_at, closed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )
        self._conn.commit()
        return cursor.lastrowid or 0

    def get_all_trades(self, limit: int = 500) -> list[TradeRecord]:
        assert self._conn
        rows = self._conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
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
        rows = self._conn.execute("SELECT DISTINCT strategy FROM trades ORDER BY strategy").fetchall()
        return [r["strategy"] for r in rows]

    def get_strategy_stats(self, strategy: str, symbol: str = "") -> dict[str, Any]:
        assert self._conn
        where = "strategy = ?"
        params: list[str] = [strategy]
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

        return dict(row) if row else {}

    def get_hourly_performance(self, strategy: str = "") -> list[dict[str, Any]]:
        assert self._conn
        where = "WHERE strategy = ?" if strategy else ""
        params = [strategy] if strategy else []
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
        return [dict(r) for r in rows]

    def get_regime_performance(self, strategy: str = "") -> list[dict[str, Any]]:
        assert self._conn
        where = "WHERE strategy = ? AND market_regime != ''" if strategy else "WHERE market_regime != ''"
        params = [strategy] if strategy else []
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
        return [dict(r) for r in rows]

    def get_recent_streak(self, strategy: str) -> int:
        """Positive = consecutive wins, negative = consecutive losses."""
        assert self._conn
        rows = self._conn.execute(
            "SELECT is_winner FROM trades WHERE strategy = ? AND pnl_usd != 0 ORDER BY id DESC LIMIT 20",
            (strategy,),
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
            "SELECT is_winner FROM trades WHERE strategy = ? AND pnl_usd != 0 ORDER BY id",
            (strategy,),
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
    def _row_to_trade(row: sqlite3.Row) -> TradeRecord:
        return TradeRecord(
            id=row["id"],
            symbol=row["symbol"],
            side=row["side"],
            strategy=row["strategy"],
            action=row["action"],
            scale_mode=row["scale_mode"],
            entry_price=row["entry_price"],
            exit_price=row["exit_price"],
            amount=row["amount"],
            leverage=row["leverage"],
            pnl_usd=row["pnl_usd"],
            pnl_pct=row["pnl_pct"],
            is_winner=bool(row["is_winner"]),
            hold_minutes=row["hold_minutes"],
            was_quick_trade=bool(row["was_quick_trade"]),
            was_low_liquidity=bool(row["was_low_liquidity"]),
            dca_count=row["dca_count"],
            max_drawdown_pct=row["max_drawdown_pct"],
            market_regime=row["market_regime"],
            fear_greed=row["fear_greed"],
            daily_tier=row["daily_tier"],
            daily_pnl_at_entry=row["daily_pnl_at_entry"],
            signal_strength=row["signal_strength"],
            hour_utc=row["hour_utc"],
            day_of_week=row["day_of_week"],
            volatility_pct=row["volatility_pct"],
            opened_at=row["opened_at"],
            closed_at=row["closed_at"],
        )
