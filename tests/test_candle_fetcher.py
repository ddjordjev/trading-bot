from __future__ import annotations

from datetime import UTC

import pytest

from hub import candle_fetcher as cf


class _FakeExchange:
    def __init__(self, _params):
        self.markets = {}
        self.sandbox = False

    def set_sandbox_mode(self, enabled: bool) -> None:
        self.sandbox = bool(enabled)

    async def load_markets(self) -> None:
        self.markets = {
            "BTC/USDT": {"active": True, "future": True, "swap": False, "spot": True},
            "ETH/USDT": {"active": False, "future": True, "swap": False, "spot": False},
        }

    async def fetch_ohlcv(self, symbol: str, _timeframe: str, limit: int = 200):
        if symbol == "BAD/USDT":
            raise Exception("bad symbol")
        assert limit >= 1
        return [
            [1700000000000, "100", "105", "95", "102", "1234"],  # valid
            [1700000001000, None, "105", "95", "102", "1234"],  # invalid and skipped
        ]

    async def fetch_ticker(self, symbol: str):
        if symbol == "BAD/USDT":
            raise Exception("symbol not found")
        return {
            "symbol": symbol,
            "bid": "100.5",
            "ask": "101.5",
            "last": "101.0",
            "quoteVolume": "123456.7",
            "percentage": "2.5",
        }

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_fetch_candles_parses_and_filters_rows(monkeypatch):
    monkeypatch.setattr(cf.ccxt, "binance", _FakeExchange)
    fetcher = cf.CandleFetcher(exchange_id="binance", sandbox=True, market_type="futures")

    candles = await fetcher.fetch_candles("BTC/USDT", timeframe="1m", limit=2)
    assert len(candles) == 1
    c0 = candles[0]
    assert c0.open == 100.0
    assert c0.close == 102.0
    assert c0.timestamp.tzinfo == UTC
    assert fetcher.has_symbol("BTC/USDT") is True

    await fetcher.close()


@pytest.mark.asyncio
async def test_fetch_candles_marks_unavailable_symbol(monkeypatch):
    monkeypatch.setattr(cf.ccxt, "binance", _FakeExchange)
    fetcher = cf.CandleFetcher(exchange_id="binance", sandbox=False, market_type="futures")

    candles = await fetcher.fetch_candles("BAD/USDT")
    assert candles == []
    assert fetcher.has_symbol("BAD/USDT") is False

    await fetcher.close()


@pytest.mark.asyncio
async def test_fetch_ticker_maps_values(monkeypatch):
    monkeypatch.setattr(cf.ccxt, "binance", _FakeExchange)
    fetcher = cf.CandleFetcher(exchange_id="binance", sandbox=False, market_type="spot")

    ticker = await fetcher.fetch_ticker("BTC/USDT")
    assert ticker is not None
    assert ticker.symbol == "BTC/USDT"
    assert ticker.bid == 100.5
    assert ticker.ask == 101.5
    assert ticker.last == 101.0
    assert ticker.volume_24h == 123456.7
    assert ticker.change_pct_24h == 2.5
    assert fetcher.has_symbol("ETH/USDT") is False  # inactive market

    await fetcher.close()


def test_safe_float_guards_bad_values():
    assert cf._safe_float(None) is None
    assert cf._safe_float("x") is None
    assert cf._safe_float(float("inf")) is None
    assert cf._safe_float("1.25") == 1.25
