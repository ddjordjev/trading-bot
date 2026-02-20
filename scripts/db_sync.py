"""Background service that merges per-bot trade DBs into a unified trades_all.db.

Best-effort: copies what it can, when it can. Runs every 30 seconds.
Each bot writes to  data/{bot_id}/trades.db
This script merges into  data/trades_all.db

On startup, logs the size of every data subdirectory and warns if any exceed 10 MB.
"""

from __future__ import annotations

import contextlib
import sqlite3
import time
from pathlib import Path

from loguru import logger

DATA_ROOT = Path("data")
UNIFIED_DB = DATA_ROOT / "trades_all.db"
SYNC_INTERVAL = 30
SIZE_WARN_MB = 10


def _check_data_sizes() -> None:
    """Log sizes of all data directories on startup. Warn if any > 10 MB."""
    logger.info("=== Data directory size check ===")
    total = 0
    for item in sorted(DATA_ROOT.iterdir()):
        if item.is_dir():
            size = sum(f.stat().st_size for f in item.rglob("*") if f.is_file())
        elif item.is_file():
            size = item.stat().st_size
        else:
            continue
        total += size
        size_mb = size / (1024 * 1024)
        tag = " *** WARNING: >10MB ***" if size_mb > SIZE_WARN_MB else ""
        logger.info("  {:30s} {:>8.2f} MB{}", str(item.name) + "/", size_mb, tag)

    total_mb = total / (1024 * 1024)
    logger.info("  {:30s} {:>8.2f} MB", "TOTAL", total_mb)
    if total_mb > SIZE_WARN_MB * 3:
        logger.warning("Total data size {:.1f} MB — consider cleanup or migration", total_mb)


def _ensure_unified_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id TEXT DEFAULT '',
            source_id INTEGER DEFAULT 0,
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

        CREATE INDEX IF NOT EXISTS idx_unified_bot ON trades(bot_id);
        CREATE INDEX IF NOT EXISTS idx_unified_source ON trades(bot_id, source_id);
        CREATE INDEX IF NOT EXISTS idx_unified_closed ON trades(closed_at);
        CREATE INDEX IF NOT EXISTS idx_unified_symbol ON trades(symbol);

        CREATE TABLE IF NOT EXISTS sync_state (
            bot_id TEXT PRIMARY KEY,
            last_synced_id INTEGER DEFAULT 0,
            last_synced_at TEXT DEFAULT ''
        );
    """)


def _get_last_synced_id(conn: sqlite3.Connection, bot_id: str) -> int:
    row = conn.execute("SELECT last_synced_id FROM sync_state WHERE bot_id = ?", (bot_id,)).fetchone()
    return row[0] if row else 0


def _set_last_synced_id(conn: sqlite3.Connection, bot_id: str, last_id: int) -> None:
    conn.execute(
        """INSERT INTO sync_state (bot_id, last_synced_id, last_synced_at)
           VALUES (?, ?, datetime('now'))
           ON CONFLICT(bot_id) DO UPDATE SET last_synced_id=?, last_synced_at=datetime('now')""",
        (bot_id, last_id, last_id),
    )
    conn.commit()


def _sync_bot(unified: sqlite3.Connection, bot_id: str, bot_db_path: Path) -> int:
    last_id = _get_last_synced_id(unified, bot_id)

    try:
        src = sqlite3.connect(str(bot_db_path))
        src.row_factory = sqlite3.Row
    except Exception as e:
        logger.debug("Cannot open {}: {}", bot_db_path, e)
        return 0

    try:
        rows = src.execute("SELECT * FROM trades WHERE id > ? ORDER BY id", (last_id,)).fetchall()
    except Exception as e:
        logger.debug("Cannot read from {}: {}", bot_db_path, e)
        src.close()
        return 0

    copied = 0
    max_id = last_id
    for row in rows:
        try:
            unified.execute(
                """INSERT INTO trades (
                    bot_id, source_id,
                    symbol, side, strategy, action, scale_mode,
                    entry_price, exit_price, amount, leverage,
                    pnl_usd, pnl_pct, is_winner, hold_minutes,
                    was_quick_trade, was_low_liquidity, dca_count, max_drawdown_pct,
                    market_regime, fear_greed, daily_tier, daily_pnl_at_entry,
                    signal_strength, hour_utc, day_of_week, volatility_pct,
                    opened_at, closed_at
                ) VALUES (?,?, ?,?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?)""",
                (
                    bot_id,
                    row["id"],
                    row["symbol"],
                    row["side"],
                    row["strategy"],
                    row["action"],
                    row["scale_mode"] or "",
                    row["entry_price"] or 0,
                    row["exit_price"] or 0,
                    row["amount"] or 0,
                    row["leverage"] or 1,
                    row["pnl_usd"] or 0,
                    row["pnl_pct"] or 0,
                    row["is_winner"] or 0,
                    row["hold_minutes"] or 0,
                    row["was_quick_trade"] or 0,
                    row["was_low_liquidity"] or 0,
                    row["dca_count"] or 0,
                    row["max_drawdown_pct"] or 0,
                    row["market_regime"] or "",
                    row["fear_greed"] or 50,
                    row["daily_tier"] or "",
                    row["daily_pnl_at_entry"] or 0,
                    row["signal_strength"] or 0,
                    row["hour_utc"] or 0,
                    row["day_of_week"] or 0,
                    row["volatility_pct"] or 0,
                    row["opened_at"] or "",
                    row["closed_at"] or "",
                ),
            )
            copied += 1
            max_id = max(max_id, row["id"])
        except Exception:
            pass

    if copied > 0:
        unified.commit()
        _set_last_synced_id(unified, bot_id, max_id)

    _resync_updated(unified, src, bot_id, last_id)
    src.close()
    return copied


def _resync_updated(unified: sqlite3.Connection, src: sqlite3.Connection, bot_id: str, up_to_id: int) -> None:
    """Re-copy rows that existed before but got updated (e.g. action open->close)."""
    try:
        rows = src.execute(
            "SELECT * FROM trades WHERE id <= ? AND closed_at != '' ORDER BY id",
            (up_to_id,),
        ).fetchall()
    except Exception:
        return

    for row in rows:
        with contextlib.suppress(Exception):
            unified.execute(
                """UPDATE trades SET
                    action=?, exit_price=?, amount=?, leverage=?,
                    pnl_usd=?, pnl_pct=?, is_winner=?, hold_minutes=?,
                    dca_count=?, max_drawdown_pct=?, closed_at=?
                WHERE bot_id=? AND source_id=? AND closed_at=''""",
                (
                    row["action"],
                    row["exit_price"] or 0,
                    row["amount"] or 0,
                    row["leverage"] or 1,
                    row["pnl_usd"] or 0,
                    row["pnl_pct"] or 0,
                    row["is_winner"] or 0,
                    row["hold_minutes"] or 0,
                    row["dca_count"] or 0,
                    row["max_drawdown_pct"] or 0,
                    row["closed_at"] or "",
                    bot_id,
                    row["id"],
                ),
            )
    unified.commit()


def main() -> None:
    logger.info("Trade Borg DB sync service starting — unified DB: {}", UNIFIED_DB)
    DATA_ROOT.mkdir(parents=True, exist_ok=True)

    if DATA_ROOT.exists():
        _check_data_sizes()

    unified = sqlite3.connect(str(UNIFIED_DB))
    unified.execute("PRAGMA journal_mode=WAL")
    _ensure_unified_db(unified)

    while True:
        bot_dirs = sorted(d for d in DATA_ROOT.iterdir() if d.is_dir() and (d / "trades.db").exists())

        total = 0
        for bot_dir in bot_dirs:
            bid = bot_dir.name
            copied = _sync_bot(unified, bid, bot_dir / "trades.db")
            if copied > 0:
                logger.info("Synced {} new rows from bot '{}'", copied, bid)
            total += copied

        if total > 0:
            logger.info("Sync cycle: {} new rows total across {} bots", total, len(bot_dirs))

        time.sleep(SYNC_INTERVAL)


if __name__ == "__main__":
    main()
