from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta

from scanner.binance_futures import BinanceFuturesScanner


class TestBinanceFuturesScanner:
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
        now = datetime.now(UTC).replace(second=0, microsecond=0)

        btc = deque()
        btc.append((now - timedelta(minutes=65), 100.0, 10_000_000.0, 1.0, 0.0001))
        btc.append((now - timedelta(minutes=5), 104.0, 12_000_000.0, 2.0, 0.00012))
        btc.append((now, 106.0, 20_000_000.0, 3.0, 0.00015))
        scanner._samples["BTC/USDT"] = btc

        eth = deque()
        eth.append((now - timedelta(minutes=65), 100.0, 10_000_000.0, 1.0, 0.0001))
        eth.append((now - timedelta(minutes=5), 100.2, 10_500_000.0, 1.1, 0.0001))
        eth.append((now, 100.3, 10_700_000.0, 1.2, 0.0001))
        scanner._samples["ETH/USDT"] = eth

        movers = scanner._compute_hot_movers()
        assert movers
        assert movers[0].symbol == "BTC/USDT"
        assert movers[0].change_1h > movers[1].change_1h

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
