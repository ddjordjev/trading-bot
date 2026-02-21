"""Tests for scanner/trending.py."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

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

    def test_exchange_pair_overrides_trading_pair(self):
        c = TrendingCoin(symbol="BTC", exchange_pair="1000LUNC/USDT")
        assert c.trading_pair == "1000LUNC/USDT"

    def test_exchange_pair_empty_uses_default(self):
        c = TrendingCoin(symbol="BTC", exchange_pair="")
        assert c.trading_pair == "BTC/USDT"


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

    def test_set_exchange_symbols_builds_alias_map(self, scanner):
        scanner.set_exchange_symbols(["BTC/USDT:USDT", "1000LUNC/USDT:USDT", "1000SHIB/USDT:USDT"])
        assert scanner._exchange_symbols == {"BTC/USDT", "1000LUNC/USDT", "1000SHIB/USDT"}
        assert scanner._symbol_alias_map["LUNC/USDT"] == ("1000LUNC/USDT", 1000)
        assert scanner._symbol_alias_map["SHIB/USDT"] == ("1000SHIB/USDT", 1000)

    def test_set_exchange_symbols_million_multiplier(self, scanner):
        scanner.set_exchange_symbols(["1000000MOG/USDT:USDT"])
        assert scanner._symbol_alias_map["MOG/USDT"] == ("1000000MOG/USDT", 1000000)

    def test_resolve_exchange_symbol_exact_match(self, scanner):
        scanner.set_exchange_symbols(["BTC/USDT:USDT"])
        coin = TrendingCoin(symbol="BTC")
        assert scanner._resolve_exchange_symbol(coin) is True
        assert coin.exchange_pair == ""

    def test_resolve_exchange_symbol_fuzzy_match(self, scanner):
        scanner.set_exchange_symbols(["1000LUNC/USDT:USDT"])
        coin = TrendingCoin(symbol="LUNC", price=0.00008)
        assert scanner._resolve_exchange_symbol(coin) is True
        assert coin.exchange_pair == "1000LUNC/USDT"

    def test_resolve_exchange_symbol_no_match(self, scanner):
        scanner.set_exchange_symbols(["BTC/USDT:USDT"])
        coin = TrendingCoin(symbol="DOGE")
        assert scanner._resolve_exchange_symbol(coin) is False

    def test_resolve_exchange_symbol_price_sanity_fail(self, scanner):
        scanner.set_exchange_symbols(["1000BADCOIN/USDT:USDT"])
        coin = TrendingCoin(symbol="BADCOIN", price=0.0)
        assert scanner._resolve_exchange_symbol(coin) is False

    def test_filter_movers_fuzzy_match_passes(self, scanner):
        scanner.set_exchange_symbols(["1000LUNC/USDT:USDT", "BTC/USDT:USDT"])
        coins = [
            TrendingCoin(symbol="BTC", volume_24h=1e9, market_cap=1e12, change_1h=5.0),
            TrendingCoin(symbol="LUNC", price=0.00008, volume_24h=1e7, market_cap=1e8, change_1h=10.0),
        ]
        movers = scanner._filter_movers(coins)
        symbols = [c.symbol for c in movers]
        assert "LUNC" in symbols
        lunc = next(c for c in movers if c.symbol == "LUNC")
        assert lunc.trading_pair == "1000LUNC/USDT"

    def test_filter_movers_no_exchange_match_excluded(self, scanner):
        scanner.set_exchange_symbols(["BTC/USDT:USDT"])
        coins = [
            TrendingCoin(symbol="DOGE", volume_24h=1e9, market_cap=1e10, change_1h=5.0),
        ]
        movers = scanner._filter_movers(coins)
        assert len(movers) == 0

    def test_set_exchange_symbols_no_alias_for_plain(self, scanner):
        scanner.set_exchange_symbols(["BTC/USDT:USDT", "ETH/USDT:USDT"])
        assert scanner._symbol_alias_map == {}

    def test_resolve_no_exchange_symbols_always_true(self, scanner):
        coin = TrendingCoin(symbol="DOGE")
        assert scanner._resolve_exchange_symbol(coin) is True

    @pytest.mark.asyncio
    async def test_stop(self, scanner):
        scanner._running = True
        await scanner.stop()
        assert scanner._running is False


# ── _merge_external_sources (with intel) ─────────────────────────────────────


class TestTrendingScannerMergeExternal:
    """Tests for _merge_external_sources with mocked MarketIntel."""

    def test_merge_external_cmc_only(self):
        scanner = TrendingScanner()
        cmc_coin = MagicMock()
        cmc_coin.symbol = "BTC"
        cmc_coin.name = "Bitcoin"
        cmc_coin.price = 50000.0
        cmc_coin.market_cap = 1e12
        cmc_coin.volume_24h = 1e9
        cmc_coin.change_1h = 2.0
        cmc_coin.change_24h = 5.0
        cmc_coin.change_7d = 10.0
        mock_cmc = MagicMock()
        mock_cmc.all_interesting = [cmc_coin]
        scanner._intel = MagicMock()
        scanner._intel.coinmarketcap = mock_cmc
        del scanner._intel.coingecko

        result = scanner._merge_external_sources()

        assert len(result) == 1
        assert result[0].symbol == "BTC"
        assert result[0].name == "Bitcoin"
        assert result[0].change_1h == 2.0
        assert result[0].change_24h == 5.0

    def test_merge_external_coingecko_only(self):
        scanner = TrendingScanner()
        gc_coin = MagicMock()
        gc_coin.symbol = "ETH"
        gc_coin.name = "Ethereum"
        gc_coin.price = 3000.0
        gc_coin.market_cap = 1e11
        gc_coin.volume_24h = 5e8
        gc_coin.change_1h = 1.0
        gc_coin.change_24h = 4.0
        gc_coin.change_7d = 8.0
        mock_gecko = MagicMock()
        mock_gecko.all_interesting = [gc_coin]
        scanner._intel = MagicMock()
        scanner._intel.coingecko = mock_gecko
        del scanner._intel.coinmarketcap

        result = scanner._merge_external_sources()

        assert len(result) == 1
        assert result[0].symbol == "ETH"
        assert result[0].change_7d == 8.0

    def test_merge_external_cmc_and_coingecko(self):
        scanner = TrendingScanner()
        cmc_coin = MagicMock()
        cmc_coin.symbol = "BTC"
        cmc_coin.name = "Bitcoin"
        cmc_coin.price = 50000.0
        cmc_coin.market_cap = 1e12
        cmc_coin.volume_24h = 1e9
        cmc_coin.change_1h = 2.0
        cmc_coin.change_24h = 5.0
        cmc_coin.change_7d = 10.0
        gc_coin = MagicMock()
        gc_coin.symbol = "SOL"
        gc_coin.name = "Solana"
        gc_coin.price = 100.0
        gc_coin.market_cap = 1e10
        gc_coin.volume_24h = 1e8
        gc_coin.change_1h = 3.0
        gc_coin.change_24h = 6.0
        gc_coin.change_7d = 12.0
        scanner._intel = MagicMock()
        scanner._intel.coinmarketcap = MagicMock(all_interesting=[cmc_coin])
        scanner._intel.coingecko = MagicMock(all_interesting=[gc_coin])

        result = scanner._merge_external_sources()

        assert len(result) == 2
        symbols = {c.symbol for c in result}
        assert symbols == {"BTC", "SOL"}

    def test_merge_external_exception_returns_empty(self):
        scanner = TrendingScanner()
        scanner._intel = MagicMock()

        class RaisesOnIter:
            def __iter__(self):
                raise RuntimeError("API down")

        scanner._intel.coinmarketcap = MagicMock()
        scanner._intel.coinmarketcap.all_interesting = RaisesOnIter()
        del scanner._intel.coingecko

        result = scanner._merge_external_sources()

        # Exception is caught and logged; returns coins collected so far (empty)
        assert result == []


# ── _fetch_cryptobubbles ─────────────────────────────────────────────────────


class TestTrendingScannerFetchCryptobubbles:
    """Tests for _fetch_cryptobubbles with mocked aiohttp."""

    @pytest.mark.asyncio
    async def test_fetch_parses_json_and_returns_coins(self):
        scanner = TrendingScanner()
        sample_data = [
            {
                "symbol": "BTC",
                "name": "Bitcoin",
                "price": 50000,
                "marketcap": 1e12,
                "volume": 1e9,
                "performance": {"hour": 2.0, "day": 5.0, "week": 10.0, "month": 15.0},
            },
            {
                "symbol": "ETH",
                "name": "Ethereum",
                "price": 3000,
                "marketcap": 1e11,
                "volume": 5e8,
                "performance": {"hour": -1.0, "day": 3.0, "week": 8.0, "month": 0},
            },
        ]
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=sample_data)
        mock_get_cm = MagicMock()
        mock_get_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_get_cm.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_get_cm)
        mock_session_cm = MagicMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with patch("scanner.trending.aiohttp.ClientSession", return_value=mock_session_cm):
            result = await scanner._fetch_cryptobubbles()

        assert len(result) == 2
        btc = next(c for c in result if c.symbol == "BTC")
        assert btc.name == "Bitcoin"
        assert btc.price == 50000.0
        assert btc.change_1h == 2.0
        assert btc.change_24h == 5.0
        assert btc.change_7d == 10.0
        assert btc.change_30d == 15.0
        eth = next(c for c in result if c.symbol == "ETH")
        assert eth.change_1h == -1.0
        assert eth.change_24h == 3.0

    @pytest.mark.asyncio
    async def test_fetch_skips_empty_symbol(self):
        scanner = TrendingScanner()
        sample_data = [
            {"symbol": "", "name": "Empty", "price": 0, "marketcap": 0, "volume": 0, "performance": {}},
            {"symbol": "BTC", "name": "Bitcoin", "price": 50000, "marketcap": 1e12, "volume": 1e9, "performance": {}},
        ]
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=sample_data)
        mock_get_cm = MagicMock()
        mock_get_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_get_cm.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_get_cm)
        mock_session_cm = MagicMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with patch("scanner.trending.aiohttp.ClientSession", return_value=mock_session_cm):
            result = await scanner._fetch_cryptobubbles()

        assert len(result) == 1
        assert result[0].symbol == "BTC"

    @pytest.mark.asyncio
    async def test_fetch_non_200_calls_fallback(self):
        scanner = TrendingScanner()
        mock_resp = AsyncMock()
        mock_resp.status = 404
        mock_get_cm = MagicMock()
        mock_get_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_get_cm.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_get_cm)
        mock_session_cm = MagicMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("scanner.trending.aiohttp.ClientSession", return_value=mock_session_cm),
            patch.object(scanner, "_fallback_fetch", new_callable=AsyncMock, return_value=[]) as mock_fallback,
        ):
            result = await scanner._fetch_cryptobubbles()

        mock_fallback.assert_called_once()
        assert result == []

    @pytest.mark.asyncio
    async def test_fetch_exception_calls_fallback(self):
        scanner = TrendingScanner()

        with (
            patch("scanner.trending.aiohttp.ClientSession", side_effect=ConnectionError("network down")),
            patch.object(scanner, "_fallback_fetch", new_callable=AsyncMock, return_value=[]) as mock_fallback,
        ):
            result = await scanner._fetch_cryptobubbles()

        mock_fallback.assert_called_once()
        assert result == []


# ── _scan_loop ──────────────────────────────────────────────────────────────


class TestTrendingScannerScanLoop:
    """Tests for _scan_loop: one iteration updates _hot_movers and calls callbacks."""

    @pytest.mark.asyncio
    async def test_scan_loop_one_iteration_updates_hot_movers_and_calls_callbacks(self):
        scanner = TrendingScanner()
        scanner._running = True
        coin = TrendingCoin(
            symbol="BTC",
            volume_24h=1e9,
            market_cap=1e12,
            change_1h=5.0,
            change_24h=10.0,
        )
        call_count = 0
        received_movers = []

        async def on_trending(movers):
            nonlocal call_count, received_movers
            call_count += 1
            received_movers = list(movers)

        scanner.on_trending(on_trending)
        sleep_count = 0

        async def fake_sleep(interval):
            nonlocal sleep_count
            sleep_count += 1
            scanner._running = False

        with (
            patch.object(scanner, "_fetch_cryptobubbles", new_callable=AsyncMock, return_value=[coin]),
            patch.object(scanner, "_filter_movers", return_value=[coin]),
            patch("scanner.trending.asyncio.sleep", side_effect=fake_sleep),
        ):
            await scanner._scan_loop()

        assert scanner._hot_movers == [coin]
        assert scanner._latest_scan == [coin]
        assert call_count == 1
        assert len(received_movers) == 1
        assert received_movers[0].symbol == "BTC"

    @pytest.mark.asyncio
    async def test_scan_loop_callback_exception_logged_loop_continues(self):
        scanner = TrendingScanner()
        scanner._running = True

        async def bad_cb(_movers):
            raise ValueError("callback error")

        scanner.on_trending(bad_cb)

        async def stop_after_sleep(_interval=0):
            scanner._running = False

        with (
            patch.object(scanner, "_fetch_cryptobubbles", new_callable=AsyncMock, return_value=[]),
            patch("scanner.trending.asyncio.sleep", side_effect=stop_after_sleep),
        ):
            await scanner._scan_loop()

        assert scanner._running is False


# ── _fallback_fetch ──────────────────────────────────────────────────────────


class TestTrendingScannerFallbackFetch:
    """Tests for _fallback_fetch (returns empty list; regex exists but coins not populated)."""

    @pytest.mark.asyncio
    async def test_fallback_fetch_returns_list(self):
        scanner = TrendingScanner()
        mock_resp = AsyncMock()
        mock_resp.text = AsyncMock(return_value="<html>no table match</html>")
        mock_get_cm = MagicMock()
        mock_get_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_get_cm.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_get_cm)
        mock_session_cm = MagicMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with patch("scanner.trending.aiohttp.ClientSession", return_value=mock_session_cm):
            result = await scanner._fallback_fetch()

        assert result == []

    @pytest.mark.asyncio
    async def test_fallback_fetch_exception_returns_empty(self):
        scanner = TrendingScanner()
        with patch("scanner.trending.aiohttp.ClientSession", side_effect=ConnectionError("offline")):
            result = await scanner._fallback_fetch()
        assert result == []


# ── start / stop ────────────────────────────────────────────────────────────


class TestTrendingScannerStartStop:
    """Tests for start() and stop() lifecycle."""

    @pytest.mark.asyncio
    async def test_start_sets_running_and_creates_task(self):
        scanner = TrendingScanner()
        assert scanner._running is False
        assert len(scanner._background_tasks) == 0

        with patch.object(scanner, "_scan_loop", new_callable=AsyncMock) as mock_loop:
            await scanner.start()
            assert scanner._running is True
            assert len(scanner._background_tasks) == 1
            # Allow the background task one tick so it can run
            await asyncio.sleep(0)
            mock_loop.assert_called()

        await scanner.stop()
        assert scanner._running is False

    @pytest.mark.asyncio
    async def test_stop_sets_running_false(self):
        scanner = TrendingScanner()
        scanner._running = True
        await scanner.stop()
        assert scanner._running is False


# ── scan_summary with intel (lines 371-374) ───────────────────────────────────


class TestTrendingScannerScanSummaryIntel:
    """Tests for scan_summary when intel has CMC/CoinGecko (sources line)."""

    def test_scan_summary_includes_cmc_and_coingecko_sources_when_intel_has_both(self):
        scanner = TrendingScanner()
        scanner._hot_movers = [
            TrendingCoin(symbol="BTC", change_1h=5.0, change_24h=10.0, volume_24h=1e9, market_cap=1e12),
        ]
        scanner._intel = MagicMock()
        scanner._intel.coinmarketcap = MagicMock(trending=True)
        scanner._intel.coingecko = MagicMock(trending=True)

        summary = scanner.scan_summary()

        assert "CMC" in summary
        assert "CoinGecko" in summary
        assert "CryptoBubbles" in summary
        assert "1 hot movers" in summary or "hot movers" in summary
