"""Tests for scanner/trending.py."""

from __future__ import annotations

import pytest

from scanner.trending import TrendingCoin, TrendingScanner

# ── TrendingCoin ────────────────────────────────────────────────────


class TestTrendingCoin:
    def test_trading_pair_plain(self):
        c = TrendingCoin(symbol="BTC")
        assert c.trading_pair == "BTC/USDT"

    def test_trading_pair_already_usdt(self):
        c = TrendingCoin(symbol="BTCUSDT")
        assert c.trading_pair == "BTCUSDT"

    def test_momentum_score(self):
        c = TrendingCoin(symbol="BTC", change_1h=5.0, change_24h=10.0, change_7d=20.0)
        expected = 5.0 * 3 + 10.0 * 2 + 20.0 * 0.5
        assert c.momentum_score == pytest.approx(expected)

    def test_is_low_liquidity_low_volume(self):
        c = TrendingCoin(symbol="SHIB", volume_24h=1_000_000, market_cap=1e8)
        assert c.is_low_liquidity is True

    def test_is_low_liquidity_low_mcap(self):
        c = TrendingCoin(symbol="SHIB", volume_24h=1e7, market_cap=1e7)
        assert c.is_low_liquidity is True

    def test_not_low_liquidity(self):
        c = TrendingCoin(symbol="BTC", volume_24h=1e9, market_cap=1e12)
        assert c.is_low_liquidity is False

    def test_volatility_to_liquidity_ratio(self):
        c = TrendingCoin(symbol="BTC", change_1h=5.0, change_24h=4.0, volume_24h=1e9)
        ratio = c.volatility_to_liquidity_ratio
        assert ratio > 0


# ── TrendingScanner ─────────────────────────────────────────────────


class TestTrendingScanner:
    @pytest.fixture()
    def scanner(self):
        return TrendingScanner()

    def test_init_defaults(self, scanner):
        assert scanner.poll_interval == 60
        assert scanner.min_volume_24h == 5_000_000

    def test_on_trending_callback(self, scanner):
        cb = lambda coins: None
        scanner.on_trending(cb)
        assert len(scanner._callbacks) == 1

    def test_hot_movers_empty(self, scanner):
        assert scanner.hot_movers == []

    def test_latest_scan_empty(self, scanner):
        assert scanner.latest_scan == []

    def test_filter_movers_excludes_stablecoins(self, scanner):
        coins = [
            TrendingCoin(symbol="USDT", volume_24h=1e9, market_cap=1e10, change_1h=5.0),
            TrendingCoin(symbol="BTC", volume_24h=1e9, market_cap=1e12, change_1h=5.0),
        ]
        movers = scanner._filter_movers(coins)
        symbols = [c.symbol for c in movers]
        assert "USDT" not in symbols
        assert "BTC" in symbols

    def test_filter_movers_excludes_low_volume(self, scanner):
        coins = [
            TrendingCoin(symbol="MICRO", volume_24h=1000, market_cap=1e8, change_1h=50.0),
        ]
        movers = scanner._filter_movers(coins)
        assert len(movers) == 0

    def test_filter_movers_excludes_low_mcap(self, scanner):
        coins = [
            TrendingCoin(symbol="TINY", volume_24h=1e7, market_cap=1000, change_1h=50.0),
        ]
        movers = scanner._filter_movers(coins)
        assert len(movers) == 0

    def test_filter_movers_includes_hourly_hot(self, scanner):
        coins = [
            TrendingCoin(symbol="SOL", volume_24h=1e8, market_cap=1e10, change_1h=3.0, change_24h=1.0),
        ]
        movers = scanner._filter_movers(coins)
        assert len(movers) == 1

    def test_filter_movers_includes_daily_hot(self, scanner):
        coins = [
            TrendingCoin(symbol="ETH", volume_24h=1e9, market_cap=1e11, change_1h=0.5, change_24h=8.0),
        ]
        movers = scanner._filter_movers(coins)
        assert len(movers) == 1

    def test_filter_movers_sorts_by_momentum(self, scanner):
        coins = [
            TrendingCoin(symbol="A", volume_24h=1e8, market_cap=1e10, change_1h=3.0, change_24h=2.0),
            TrendingCoin(symbol="B", volume_24h=1e8, market_cap=1e10, change_1h=10.0, change_24h=15.0),
        ]
        movers = scanner._filter_movers(coins)
        assert movers[0].symbol == "B"

    def test_get_strongest_bullish(self, scanner):
        scanner._hot_movers = [
            TrendingCoin(symbol="BTC", change_1h=5.0, change_24h=10.0, volume_24h=1e9, market_cap=1e12),
            TrendingCoin(symbol="ETH", change_1h=2.0, change_24h=3.0, volume_24h=1e9, market_cap=1e11),
            TrendingCoin(symbol="DOGE", change_1h=-5.0, change_24h=-10.0, volume_24h=1e8, market_cap=1e10),
        ]
        bulls = scanner.get_strongest_bullish(2)
        assert len(bulls) == 2
        assert bulls[0].symbol == "BTC"

    def test_get_strongest_bearish(self, scanner):
        scanner._hot_movers = [
            TrendingCoin(symbol="DOGE", change_1h=-5.0, change_24h=-10.0, volume_24h=1e8, market_cap=1e10),
            TrendingCoin(symbol="BTC", change_1h=5.0, change_24h=10.0, volume_24h=1e9, market_cap=1e12),
        ]
        bears = scanner.get_strongest_bearish(1)
        assert len(bears) == 1
        assert bears[0].symbol == "DOGE"

    def test_scan_summary_no_movers(self, scanner):
        summary = scanner.scan_summary()
        assert "No hot movers" in summary

    def test_scan_summary_with_movers(self, scanner):
        scanner._hot_movers = [
            TrendingCoin(symbol="BTC", change_1h=5.0, change_24h=10.0, volume_24h=1e9, market_cap=1e12),
        ]
        summary = scanner.scan_summary()
        assert "1 hot movers" in summary
        assert "BTC" in summary

    def test_merge_external_no_intel(self, scanner):
        result = scanner._merge_external_sources()
        assert result == []

    @pytest.mark.asyncio
    async def test_stop(self, scanner):
        scanner._running = True
        await scanner.stop()
        assert scanner._running is False
