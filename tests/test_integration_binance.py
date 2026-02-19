"""Integration tests — Binance futures testnet connectivity.

Run:  pytest tests/test_integration_binance.py -v -m integration --no-cov
"""

from __future__ import annotations

import pytest

from config.settings import get_settings
from core.exchange.binance import BinanceExchange
from core.models import MarketType

pytestmark = pytest.mark.integration


@pytest.fixture
async def ex():
    s = get_settings()
    exchange = BinanceExchange(
        api_key=s.binance_test_api_key,
        api_secret=s.binance_test_api_secret,
        sandbox=True,
    )
    exchange._futures.has["fetchCurrencies"] = False
    exchange._futures.options["fetchMarkets"] = ["linear"]
    await exchange._futures.load_markets()
    yield exchange
    await exchange._futures.close()


class TestBinanceFuturesTestnet:
    async def test_connect(self, ex):
        assert len(ex._futures.markets) > 0

    async def test_ticker(self, ex):
        data = await ex._futures.fetch_ticker("BTC/USDT:USDT")
        assert data["last"] > 0

    async def test_candles(self, ex):
        data = await ex._futures.fetch_ohlcv("BTC/USDT:USDT", "1m", limit=5)
        assert len(data) > 0
        assert data[-1][4] > 0

    async def test_positions(self, ex):
        positions = await ex.fetch_positions()
        assert isinstance(positions, list)

    async def test_symbols(self, ex):
        symbols = await ex.get_available_symbols(MarketType.FUTURES)
        assert any("BTC" in s for s in symbols)

    async def test_leverage(self, ex):
        await ex.set_leverage("BTC/USDT:USDT", 10)
