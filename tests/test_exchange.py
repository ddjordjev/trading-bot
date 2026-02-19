"""Tests for core/exchange (factory, paper, base)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from core.exchange.base import BaseExchange
from core.exchange.paper import PaperExchange
from core.models import (
    MarketType,
    OrderBook,
    OrderSide,
    OrderStatus,
    OrderType,
    Ticker,
)

# ── Helpers ─────────────────────────────────────────────────────────


def _make_ticker(last: float = 100.0) -> Ticker:
    return Ticker(
        symbol="BTC/USDT",
        bid=last - 0.5,
        ask=last + 0.5,
        last=last,
        volume_24h=1e6,
        change_pct_24h=1.0,
        timestamp=datetime.now(UTC),
    )


def _mock_real_exchange(ticker_last: float = 100.0) -> AsyncMock:
    real = AsyncMock(spec=BaseExchange)
    real.name = "mexc"
    real.connect = AsyncMock()
    real.disconnect = AsyncMock()
    real.fetch_ticker = AsyncMock(return_value=_make_ticker(ticker_last))
    real.fetch_tickers = AsyncMock(return_value=[_make_ticker(ticker_last)])
    real.fetch_candles = AsyncMock(return_value=[])
    real.fetch_order_book = AsyncMock(
        return_value=OrderBook(symbol="BTC/USDT", bids=[], asks=[], timestamp=datetime.now(UTC))
    )
    real.get_available_symbols = AsyncMock(return_value=["BTC/USDT"])
    real.watch_ticker = AsyncMock()
    real.watch_candles = AsyncMock()
    return real


# ── PaperExchange ───────────────────────────────────────────────────


class TestPaperExchange:
    @pytest.fixture()
    def paper(self):
        return PaperExchange(_mock_real_exchange(100.0), starting_balance=10000.0)

    @pytest.mark.asyncio
    async def test_name(self, paper):
        assert paper.name == "paper_mexc"

    @pytest.mark.asyncio
    async def test_connect(self, paper):
        await paper.connect()
        paper._real.connect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_disconnect(self, paper):
        await paper.disconnect()
        paper._real.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fetch_balance(self, paper):
        bal = await paper.fetch_balance()
        assert bal["USDT"] == 10000.0

    @pytest.mark.asyncio
    async def test_place_market_buy_spot(self, paper):
        order = await paper.place_order("BTC/USDT", OrderSide.BUY, OrderType.MARKET, 1.0)
        assert order.status == OrderStatus.FILLED
        assert order.filled == 1.0
        bal = await paper.fetch_balance()
        assert bal["USDT"] < 10000.0

    @pytest.mark.asyncio
    async def test_place_buy_insufficient_balance(self, paper):
        order = await paper.place_order("BTC/USDT", OrderSide.BUY, OrderType.MARKET, 200.0)
        assert order.status == OrderStatus.FAILED

    @pytest.mark.asyncio
    async def test_place_futures_order(self, paper):
        order = await paper.place_order(
            "BTC/USDT", OrderSide.BUY, OrderType.MARKET, 1.0, leverage=10, market_type=MarketType.FUTURES
        )
        assert order.status == OrderStatus.FILLED
        positions = await paper.fetch_positions()
        assert len(positions) == 1
        assert positions[0].leverage == 10

    @pytest.mark.asyncio
    async def test_close_futures_position(self, paper):
        await paper.place_order(
            "BTC/USDT", OrderSide.BUY, OrderType.MARKET, 1.0, leverage=10, market_type=MarketType.FUTURES
        )
        paper._real.fetch_ticker = AsyncMock(return_value=_make_ticker(110.0))
        order = await paper.place_order(
            "BTC/USDT", OrderSide.SELL, OrderType.MARKET, 1.0, leverage=10, market_type=MarketType.FUTURES
        )
        assert order.status == OrderStatus.FILLED
        positions = await paper.fetch_positions()
        assert len(positions) == 0

    @pytest.mark.asyncio
    async def test_spot_sell(self, paper):
        # Must own base asset before selling
        await paper.place_order("BTC/USDT", OrderSide.BUY, OrderType.MARKET, 1.0)
        bal_before = (await paper.fetch_balance())["USDT"]
        order = await paper.place_order("BTC/USDT", OrderSide.SELL, OrderType.MARKET, 1.0)
        assert order.status == OrderStatus.FILLED
        bal_after = (await paper.fetch_balance())["USDT"]
        assert bal_after > bal_before

    @pytest.mark.asyncio
    async def test_spot_sell_without_holdings(self, paper):
        order = await paper.place_order("BTC/USDT", OrderSide.SELL, OrderType.MARKET, 1.0)
        assert order.status == OrderStatus.FAILED

    @pytest.mark.asyncio
    async def test_cancel_order(self, paper):
        order = await paper.place_order("BTC/USDT", OrderSide.BUY, OrderType.MARKET, 0.1)
        cancelled = await paper.cancel_order(order.id, "BTC/USDT")
        assert cancelled.status == OrderStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_order(self, paper):
        cancelled = await paper.cancel_order("nonexistent", "BTC/USDT")
        assert cancelled.status == OrderStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_fetch_order(self, paper):
        order = await paper.place_order("BTC/USDT", OrderSide.BUY, OrderType.MARKET, 0.1)
        fetched = await paper.fetch_order(order.id, "BTC/USDT")
        assert fetched.id == order.id

    @pytest.mark.asyncio
    async def test_fetch_nonexistent_order(self, paper):
        fetched = await paper.fetch_order("nonexistent", "BTC/USDT")
        assert fetched.status == OrderStatus.FAILED

    @pytest.mark.asyncio
    async def test_fetch_open_orders(self, paper):
        orders = await paper.fetch_open_orders()
        assert orders == []

    @pytest.mark.asyncio
    async def test_set_leverage(self, paper):
        await paper.set_leverage("BTC/USDT", 20)
        assert paper._leverage_map["BTC/USDT"] == 20

    @pytest.mark.asyncio
    async def test_get_available_symbols(self, paper):
        syms = await paper.get_available_symbols()
        assert syms == ["BTC/USDT"]

    @pytest.mark.asyncio
    async def test_fetch_positions_by_symbol(self, paper):
        await paper.place_order(
            "BTC/USDT", OrderSide.BUY, OrderType.MARKET, 1.0, leverage=10, market_type=MarketType.FUTURES
        )
        positions = await paper.fetch_positions("ETH/USDT")
        assert positions == []
        positions = await paper.fetch_positions("BTC/USDT")
        assert len(positions) == 1

    @pytest.mark.asyncio
    async def test_passthrough_market_data(self, paper):
        await paper.fetch_tickers()
        paper._real.fetch_tickers.assert_awaited_once()
        await paper.fetch_candles("BTC/USDT")
        paper._real.fetch_candles.assert_awaited_once()
        await paper.fetch_order_book("BTC/USDT")
        paper._real.fetch_order_book.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_spot_buy_tracks_base_asset(self, paper):
        await paper.place_order("BTC/USDT", OrderSide.BUY, OrderType.MARKET, 0.5)
        assert paper._balances.get("BTC", 0) == 0.5
        assert paper._balances["USDT"] < 10000.0

    @pytest.mark.asyncio
    async def test_spot_sell_deducts_base_asset(self, paper):
        await paper.place_order("BTC/USDT", OrderSide.BUY, OrderType.MARKET, 2.0)
        btc_before = paper._balances["BTC"]
        await paper.place_order("BTC/USDT", OrderSide.SELL, OrderType.MARKET, 1.0)
        assert paper._balances["BTC"] == btc_before - 1.0

    @pytest.mark.asyncio
    async def test_parse_base_asset(self):
        assert PaperExchange._parse_base_asset("BTC/USDT") == "BTC"
        assert PaperExchange._parse_base_asset("ETH/USDT") == "ETH"
        assert PaperExchange._parse_base_asset("DOGE") == "DOGE"

    @pytest.mark.asyncio
    async def test_place_order_ticker_failure(self):
        real = _mock_real_exchange()
        real.fetch_ticker = AsyncMock(side_effect=Exception("network error"))
        paper = PaperExchange(real, starting_balance=1000.0)
        order = await paper.place_order("BTC/USDT", OrderSide.BUY, OrderType.MARKET, 1.0)
        assert order.status == OrderStatus.FAILED

    @pytest.mark.asyncio
    async def test_futures_sell_reserves_margin(self, paper):
        bal_before = paper._balances["USDT"]
        await paper.place_order(
            "BTC/USDT", OrderSide.SELL, OrderType.MARKET, 1.0, leverage=10, market_type=MarketType.FUTURES
        )
        expected_margin = (1.0 * 100.0) / 10
        assert paper._balances["USDT"] == bal_before - expected_margin

    @pytest.mark.asyncio
    async def test_spot_buy_insufficient_balance(self):
        real = _mock_real_exchange(50000.0)
        paper = PaperExchange(real, starting_balance=10.0)
        order = await paper.place_order("BTC/USDT", OrderSide.BUY, OrderType.MARKET, 1.0)
        assert order.status == OrderStatus.FAILED
        assert paper._balances["USDT"] == 10.0

    @pytest.mark.asyncio
    async def test_spot_sell_partial_holdings(self):
        real = _mock_real_exchange(100.0)
        paper = PaperExchange(real, starting_balance=10000.0)
        await paper.place_order("BTC/USDT", OrderSide.BUY, OrderType.MARKET, 3.0)
        order = await paper.place_order("BTC/USDT", OrderSide.SELL, OrderType.MARKET, 5.0)
        assert order.status == OrderStatus.FAILED
        assert paper._balances.get("BTC", 0) == 3.0

    @pytest.mark.asyncio
    async def test_futures_dca_same_side(self):
        real = _mock_real_exchange(100.0)
        paper = PaperExchange(real, starting_balance=10000.0)
        await paper.place_order(
            "BTC/USDT", OrderSide.BUY, OrderType.MARKET, 1.0, leverage=10, market_type=MarketType.FUTURES
        )
        await paper.place_order(
            "BTC/USDT", OrderSide.BUY, OrderType.MARKET, 1.0, leverage=10, market_type=MarketType.FUTURES
        )
        positions = await paper.fetch_positions()
        assert len(positions) == 1
        assert positions[0].amount == 2.0


# ── Factory ─────────────────────────────────────────────────────────


class TestExchangeFactory:
    def test_paper_mode(self, monkeypatch):
        monkeypatch.setenv("TRADING_MODE", "paper_local")
        monkeypatch.setenv("EXCHANGE", "mexc")
        monkeypatch.setenv("MEXC_API_KEY", "k")
        monkeypatch.setenv("MEXC_API_SECRET", "s")
        from config.settings import Settings
        from core.exchange.factory import create_exchange

        exchange = create_exchange(Settings())
        assert isinstance(exchange, PaperExchange)

    def test_unsupported_exchange(self, monkeypatch):
        monkeypatch.setenv("TRADING_MODE", "paper_local")
        monkeypatch.setenv("EXCHANGE", "kraken")
        from config.settings import Settings
        from core.exchange.factory import create_exchange

        with pytest.raises(ValueError, match="Unsupported exchange"):
            create_exchange(Settings())
