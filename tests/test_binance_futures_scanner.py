from __future__ import annotations

import sqlite3
from collections import deque
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from scanner.binance_futures import BinanceFuturesScanner


class TestBinanceFuturesScanner:
    def test_sample_at_or_before_empty_returns_cutoff_and_zero(self):
        """Guard against IndexError when sample lists are empty."""
        cutoff = datetime.now(UTC)
        ts, price = BinanceFuturesScanner._sample_at_or_before([], [], cutoff)
        assert ts == cutoff
        assert price == 0.0

    def test_confidence_grows_with_samples(self):
        assert BinanceFuturesScanner._confidence(0) == 0.1
        assert BinanceFuturesScanner._confidence(5) > BinanceFuturesScanner._confidence(1)
        assert BinanceFuturesScanner._confidence(60) == 1.0
        assert BinanceFuturesScanner._confidence(120) == 1.0

    def test_compute_hot_movers_uses_volume_and_momentum(self, tmp_path):
        scanner = BinanceFuturesScanner(
            db_path=tmp_path / "hub.db",
            min_quote_volume=1_000_000,
            top_movers_count=5,
            enabled=False,
        )
        scanner._states["BTC/USDT"] = {
            "symbol": "BTC/USDT",
            "last_price": 106.0,
            "last_quote_volume": 20_000_000.0,
            "last_change_24h": 3.0,
            "last_funding_rate": 0.00015,
            "chg_5m": 1.9,
            "chg_1h": 6.0,
            "chg_1w": 0.0,
            "chg_1mo": 0.0,
            "confidence": 1.0,
            "score": 22.0,
        }
        scanner._states["ETH/USDT"] = {
            "symbol": "ETH/USDT",
            "last_price": 100.3,
            "last_quote_volume": 10_700_000.0,
            "last_change_24h": 1.2,
            "last_funding_rate": 0.0001,
            "chg_5m": 0.1,
            "chg_1h": 0.3,
            "chg_1w": 0.0,
            "chg_1mo": 0.0,
            "confidence": 1.0,
            "score": 2.0,
        }

        movers = scanner._compute_hot_movers()
        assert movers
        assert movers[0].symbol == "BTC/USDT"
        assert movers[0].change_1h > movers[1].change_1h

    def test_compute_hot_movers_ignores_stale_symbols_not_in_exchange_inventory(self, tmp_path):
        scanner = BinanceFuturesScanner(
            db_path=tmp_path / "hub.db",
            min_quote_volume=1_000_000,
            top_movers_count=10,
            enabled=False,
        )
        scanner.set_exchange_symbols({"BTC/USDT"})
        scanner._states["BTC/USDT"] = {
            "symbol": "BTC/USDT",
            "last_price": 100.0,
            "last_quote_volume": 20_000_000.0,
            "last_change_24h": 1.0,
            "last_funding_rate": 0.0,
            "chg_5m": 0.2,
            "chg_1h": 0.6,
            "chg_1w": 0.0,
            "chg_1mo": 0.0,
            "confidence": 1.0,
            "score": 2.0,
        }
        scanner._states["ALPACA/USDT"] = {
            "symbol": "ALPACA/USDT",
            "last_price": 1.0,
            "last_quote_volume": 100_000_000.0,
            "last_change_24h": 300.0,
            "last_funding_rate": 0.0,
            "chg_5m": 1.0,
            "chg_1h": 5.0,
            "chg_1w": 0.0,
            "chg_1mo": 0.0,
            "confidence": 1.0,
            "score": 99.0,
        }

        movers = scanner._compute_hot_movers()
        symbols = {m.symbol for m in movers}
        assert "BTC/USDT" in symbols
        assert "ALPACA/USDT" not in symbols

    def test_evict_old_samples(self, tmp_path):
        scanner = BinanceFuturesScanner(
            db_path=tmp_path / "hub.db",
            enabled=False,
            history_hours=1,
        )
        now = datetime.now(UTC)
        scanner._samples["X/USDT"] = deque(
            [
                (now - timedelta(hours=2), 1.0, 1_000_000.0, 0.0, 0.0),
                (now - timedelta(minutes=10), 1.1, 1_100_000.0, 1.0, 0.0),
            ]
        )
        scanner._evict_old_samples(now - timedelta(hours=1))
        assert len(scanner._samples["X/USDT"]) == 1

    def test_update_symbol_state_populates_horizons(self, tmp_path):
        scanner = BinanceFuturesScanner(
            db_path=tmp_path / "hub.db",
            enabled=False,
        )
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        scanner._update_symbol_state(
            symbol="SOL/USDT",
            ts=now,
            price=150.0,
            quote_volume=10_000_000.0,
            change_24h=1.0,
            funding_rate=0.0001,
        )
        scanner._update_symbol_state(
            symbol="SOL/USDT",
            ts=now + timedelta(minutes=2),
            price=153.0,
            quote_volume=11_000_000.0,
            change_24h=2.0,
            funding_rate=0.00012,
        )
        st = scanner._states["SOL/USDT"]
        assert st["sample_count"] == 2
        assert st["chg_1m"] >= 0.0
        assert "anchor_1h_ts" in st

    @pytest.mark.asyncio
    async def test_persist_tick_retries_on_database_locked(self, tmp_path):
        scanner = BinanceFuturesScanner(db_path=tmp_path / "hub.db", enabled=False)
        now = datetime.now(UTC)
        rows = [{"symbol": "BTC/USDT", "price": 1.0}]
        states = [{"symbol": "BTC/USDT", "last_price": 1.0}]

        db = MagicMock()
        db.save_binance_snapshots.side_effect = [sqlite3.OperationalError("database is locked"), None]
        db.save_binance_symbol_states.return_value = None
        db.cleanup_binance_snapshots_before.return_value = 0

        with patch("scanner.binance_futures.HubDB", return_value=db):
            await scanner._persist_tick(rows, states, now)

        assert db.connect.call_count == 2
        assert db.save_binance_snapshots.call_count == 2
        assert db.save_binance_symbol_states.call_count == 1
