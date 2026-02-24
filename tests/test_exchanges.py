"""Tests for core/exchange adapters: Binance, Bybit, MEXC (ccxt wrappers)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.exchange.base import parse_order_status, ts_to_dt
from core.models import (
    Candle,
    MarketType,
    OrderBook,
    OrderSide,
    OrderStatus,
    OrderType,
    Ticker,
)

# ── Base helpers ───────────────────────────────────────────────────────


def _ts_ms(dt: datetime | None = None) -> int:
    if dt is None:
        dt = datetime.now(UTC)
    return int(dt.timestamp() * 1000)


def _raw_ticker(symbol: str = "BTC/USDT", last: float = 100.0) -> dict:
    return {
        "symbol": symbol,
        "bid": last - 0.5,
        "ask": last + 0.5,
        "last": last,
        "quoteVolume": 1e6,
        "percentage": 2.0,
        "timestamp": _ts_ms(),
    }


def _raw_ohlcv(limit: int = 3) -> list:
    base = _ts_ms()
    return [[base - (i + 1) * 60_000, 99.0 + i, 100.0 + i, 98.0 + i, 99.5 + i, 1000.0] for i in range(limit)]


def _raw_order_book() -> dict:
    return {
        "bids": [[99.0, 1.0], [98.0, 2.0]],
        "asks": [[101.0, 1.5], [102.0, 2.5]],
        "timestamp": _ts_ms(),
    }


# ── parse_order_status / ts_to_dt (shared) ──────────────────────────────


class TestParseOrderStatus:
    def test_open(self):
        assert parse_order_status("open") == OrderStatus.OPEN

    def test_closed(self):
        assert parse_order_status("closed") == OrderStatus.FILLED

    def test_canceled(self):
        assert parse_order_status("canceled") == OrderStatus.CANCELLED

    def test_cancelled(self):
        assert parse_order_status("cancelled") == OrderStatus.CANCELLED

    def test_rejected(self):
        assert parse_order_status("rejected") == OrderStatus.FAILED

    def test_unknown_defaults_pending(self):
        assert parse_order_status("unknown") == OrderStatus.PENDING


class TestTsToDt:
    def test_none_returns_now(self):
        dt = ts_to_dt(None)
        assert dt.tzinfo is not None

    def test_ms_timestamp(self):
        dt = ts_to_dt(1_700_000_000_000)  # 2023-11-14-ish
        assert dt.year == 2023
        assert dt.tzinfo is not None


# ── BinanceExchange ───────────────────────────────────────────────────


class TestBinanceExchange:
    @pytest.fixture
    def binance(self):
        with patch("core.exchange.binance.ccxt") as m_ccxt:
            m_ccxt.binance.side_effect = lambda *a, **kw: MagicMock(
                load_markets=AsyncMock(),
                close=AsyncMock(),
                fetch_ticker=AsyncMock(return_value=_raw_ticker()),
                fetch_tickers=AsyncMock(return_value={"BTC/USDT": _raw_ticker()}),
                fetch_ohlcv=AsyncMock(return_value=_raw_ohlcv()),
                fetch_order_book=AsyncMock(return_value=_raw_order_book()),
                fetch_balance=AsyncMock(return_value={"USDT": {"free": 10_000.0, "used": 0.0}}),
                fetch_positions=AsyncMock(return_value=[]),
                create_order=AsyncMock(
                    return_value={
                        "id": "ord-1",
                        "symbol": "BTC/USDT",
                        "side": "buy",
                        "type": "market",
                        "status": "closed",
                        "filled": 1.0,
                        "average": 100.0,
                        "amount": 1.0,
                    }
                ),
                cancel_order=AsyncMock(return_value={"id": "ord-1", "amount": 1.0}),
                fetch_order=AsyncMock(
                    return_value={
                        "id": "ord-1",
                        "symbol": "BTC/USDT",
                        "side": "buy",
                        "type": "market",
                        "status": "closed",
                        "filled": 1.0,
                        "average": 100.0,
                        "amount": 1.0,
                    }
                ),
                fetch_open_orders=AsyncMock(return_value=[]),
                set_leverage=AsyncMock(),
                markets={"BTC/USDT": {}, "ETH/USDT": {}},
            )
            from core.exchange.binance import BinanceExchange

            return BinanceExchange(api_key="k", api_secret="s", sandbox=True)

    def test_name(self, binance):
        assert binance.name == "binance"

    def test_supported_market_types(self, binance):
        assert binance.SUPPORTED_MARKET_TYPES == ("spot", "futures")
        assert binance.supports("spot") is True
        assert binance.supports("futures") is True
        assert binance.supports("option") is False

    def test_has_testnet(self, binance):
        assert binance.HAS_TESTNET is True

    @pytest.mark.asyncio
    async def test_connect(self, binance):
        await binance.connect()
        binance._spot.load_markets.assert_awaited_once()
        binance._futures.load_markets.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_disconnect(self, binance):
        await binance.connect()
        await binance.disconnect()
        binance._spot.close.assert_awaited_once()
        binance._futures.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fetch_ticker(self, binance):
        ticker = await binance.fetch_ticker("BTC/USDT")
        assert isinstance(ticker, Ticker)
        assert ticker.symbol == "BTC/USDT"
        assert ticker.last == 100.0
        assert ticker.bid == 99.5
        assert ticker.ask == 100.5
        assert ticker.volume_24h == 1e6
        assert ticker.change_pct_24h == 2.0

    @pytest.mark.asyncio
    async def test_fetch_ticker_handles_missing_fields(self, binance):
        binance._spot.fetch_ticker = AsyncMock(return_value={"symbol": "BTC/USDT"})
        ticker = await binance.fetch_ticker("BTC/USDT")
        assert ticker.bid == 0
        assert ticker.last == 0

    @pytest.mark.asyncio
    async def test_fetch_candles(self, binance):
        candles = await binance.fetch_candles("BTC/USDT", "1m", limit=10)
        assert len(candles) == 3
        assert all(isinstance(c, Candle) for c in candles)
        assert candles[0].open == 99.0
        assert candles[0].high == 100.0
        assert candles[0].close == 99.5
        assert candles[0].volume == 1000.0

    @pytest.mark.asyncio
    async def test_fetch_order_book(self, binance):
        ob = await binance.fetch_order_book("BTC/USDT", limit=20)
        assert isinstance(ob, OrderBook)
        assert ob.symbol == "BTC/USDT"
        assert len(ob.bids) == 2
        assert ob.bids[0] == (99.0, 1.0)
        assert len(ob.asks) == 2

    @pytest.mark.asyncio
    async def test_fetch_balance(self, binance):
        bal = await binance.fetch_balance()
        assert "USDT" in bal
        assert bal["USDT"] == 20_000.0

    @pytest.mark.asyncio
    async def test_fetch_positions_empty(self, binance):
        positions = await binance.fetch_positions()
        assert positions == []

    @pytest.mark.asyncio
    async def test_fetch_positions_parses_ccxt(self, binance):
        binance._futures.fetch_positions = AsyncMock(
            return_value=[
                {
                    "symbol": "BTC/USDT",
                    "side": "long",
                    "contracts": 0.1,
                    "entryPrice": 50_000.0,
                    "markPrice": 52_000.0,
                    "leverage": 10,
                    "unrealizedPnl": 200.0,
                },
            ]
        )
        positions = await binance.fetch_positions()
        assert len(positions) == 1
        assert positions[0].symbol == "BTC/USDT"
        assert positions[0].side == OrderSide.BUY
        assert positions[0].amount == 0.1
        assert positions[0].entry_price == 50_000.0
        assert positions[0].current_price == 52_000.0
        assert positions[0].leverage == 10
        assert positions[0].market_type == "futures"
        assert positions[0].unrealized_pnl == 200.0

    @pytest.mark.asyncio
    async def test_fetch_positions_infers_leverage_from_margin_percentage(self, binance):
        binance._futures.fetch_positions = AsyncMock(
            return_value=[
                {
                    "symbol": "BTC/USDT:USDT",
                    "side": "long",
                    "contracts": 0.1,
                    "entryPrice": 50_000.0,
                    "markPrice": 52_000.0,
                    "leverage": None,
                    "initialMarginPercentage": 0.33333334,
                    "unrealizedPnl": 200.0,
                },
            ]
        )
        positions = await binance.fetch_positions()
        assert len(positions) == 1
        assert positions[0].symbol == "BTC/USDT"
        assert positions[0].leverage == 3

    @pytest.mark.asyncio
    async def test_fetch_positions_skips_zero_contracts(self, binance):
        binance._futures.fetch_positions = AsyncMock(
            return_value=[
                {"symbol": "BTC/USDT", "side": "long", "contracts": 0, "entryPrice": 0, "markPrice": 0},
            ]
        )
        positions = await binance.fetch_positions()
        assert positions == []

    @pytest.mark.asyncio
    async def test_fetch_positions_exception_returns_empty(self, binance):
        binance._futures.fetch_positions = AsyncMock(side_effect=Exception("API error"))
        positions = await binance.fetch_positions()
        assert positions == []

    @pytest.mark.asyncio
    async def test_place_order_spot(self, binance):
        order = await binance.place_order(
            "BTC/USDT",
            OrderSide.BUY,
            OrderType.MARKET,
            1.0,
            market_type=MarketType.SPOT,
        )
        assert order.symbol == "BTC/USDT"
        assert order.side == OrderSide.BUY
        assert order.status == OrderStatus.FILLED
        assert order.filled == 1.0
        assert order.market_type == "spot"
        binance._spot.create_order.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_place_order_futures_sets_leverage(self, binance):
        await binance.place_order(
            "BTC/USDT",
            OrderSide.BUY,
            OrderType.MARKET,
            0.1,
            leverage=10,
            market_type=MarketType.FUTURES,
        )
        binance._futures.set_leverage.assert_awaited_once_with(10, "BTC/USDT")
        binance._futures.create_order.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cancel_order(self, binance):
        order = await binance.cancel_order("ord-1", "BTC/USDT")
        assert order.id == "ord-1"
        assert order.status == OrderStatus.CANCELLED
        binance._spot.cancel_order.assert_awaited_once_with("ord-1", "BTC/USDT")

    @pytest.mark.asyncio
    async def test_get_available_symbols_spot(self, binance):
        await binance.connect()
        syms = await binance.get_available_symbols(MarketType.SPOT)
        assert "BTC/USDT" in syms
        assert "ETH/USDT" in syms

    @pytest.mark.asyncio
    async def test_get_available_symbols_futures(self, binance):
        await binance.connect()
        syms = await binance.get_available_symbols(MarketType.FUTURES)
        assert isinstance(syms, list)


# ── BybitExchange ───────────────────────────────────────────────────────


class TestBybitExchange:
    @pytest.fixture
    def bybit(self):
        with patch("core.exchange.bybit.ccxt") as m_ccxt:
            m_ccxt.bybit.side_effect = lambda *a, **kw: MagicMock(
                load_markets=AsyncMock(),
                close=AsyncMock(),
                fetch_ticker=AsyncMock(return_value=_raw_ticker()),
                fetch_tickers=AsyncMock(return_value={"BTC/USDT": _raw_ticker()}),
                fetch_ohlcv=AsyncMock(return_value=_raw_ohlcv()),
                fetch_order_book=AsyncMock(return_value=_raw_order_book()),
                fetch_balance=AsyncMock(return_value={"USDT": {"free": 5_000.0, "used": 0.0}}),
                fetch_positions=AsyncMock(return_value=[]),
                create_order=AsyncMock(
                    return_value={
                        "id": "bybit-1",
                        "symbol": "BTC/USDT",
                        "side": "sell",
                        "type": "market",
                        "status": "closed",
                        "filled": 0.5,
                        "average": 51_000.0,
                        "amount": 0.5,
                    }
                ),
                cancel_order=AsyncMock(return_value={"id": "bybit-1", "amount": 0.5}),
                fetch_order=AsyncMock(
                    return_value={
                        "id": "bybit-1",
                        "symbol": "BTC/USDT",
                        "side": "sell",
                        "type": "limit",
                        "status": "open",
                        "filled": 0.0,
                        "average": 0.0,
                        "amount": 0.5,
                    }
                ),
                fetch_open_orders=AsyncMock(return_value=[]),
                set_leverage=AsyncMock(),
                markets={"BTC/USDT": {}, "ETH/USDT": {}},
            )
            from core.exchange.bybit import BybitExchange

            return BybitExchange(api_key="k", api_secret="s", sandbox=True)

    def test_name(self, bybit):
        assert bybit.name == "bybit"

    def test_supported_market_types(self, bybit):
        assert bybit.supports("spot") is True
        assert bybit.supports("futures") is True

    @pytest.mark.asyncio
    async def test_connect_disconnect(self, bybit):
        await bybit.connect()
        bybit._spot.load_markets.assert_awaited_once()
        await bybit.disconnect()
        bybit._spot.close.assert_awaited_once()
        bybit._futures.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fetch_ticker(self, bybit):
        ticker = await bybit.fetch_ticker("BTC/USDT")
        assert ticker.symbol == "BTC/USDT"
        assert ticker.last == 100.0

    @pytest.mark.asyncio
    async def test_fetch_positions_short_side(self, bybit):
        bybit._futures.fetch_positions = AsyncMock(
            return_value=[
                {
                    "symbol": "ETH/USDT",
                    "side": "short",
                    "contracts": 1.0,
                    "entryPrice": 3000.0,
                    "markPrice": 2900.0,
                    "leverage": 5,
                    "unrealizedPnl": 100.0,
                },
            ]
        )
        positions = await bybit.fetch_positions()
        assert len(positions) == 1
        assert positions[0].side == OrderSide.SELL
        assert positions[0].symbol == "ETH/USDT"

    @pytest.mark.asyncio
    async def test_fetch_positions_infers_leverage_from_notional_and_margin(self, bybit):
        bybit._futures.fetch_positions = AsyncMock(
            return_value=[
                {
                    "symbol": "ETH/USDT:USDT",
                    "side": "short",
                    "contracts": 1.0,
                    "entryPrice": 3000.0,
                    "markPrice": 2900.0,
                    "leverage": None,
                    "initialMargin": 100.0,
                    "notional": -300.0,
                    "unrealizedPnl": 100.0,
                },
            ]
        )
        positions = await bybit.fetch_positions()
        assert len(positions) == 1
        assert positions[0].symbol == "ETH/USDT"
        assert positions[0].leverage == 3

    @pytest.mark.asyncio
    async def test_place_order_parsing(self, bybit):
        order = await bybit.place_order(
            "BTC/USDT",
            OrderSide.SELL,
            OrderType.MARKET,
            0.5,
            market_type=MarketType.SPOT,
        )
        assert order.id == "bybit-1"
        assert order.side == OrderSide.SELL
        assert order.filled == 0.5
        assert order.average_price == 51_000.0
        assert parse_order_status("closed") == OrderStatus.FILLED

    @pytest.mark.asyncio
    async def test_fetch_order_parsing(self, bybit):
        order = await bybit.fetch_order("bybit-1", "BTC/USDT")
        assert order.side == OrderSide.SELL
        assert order.order_type == OrderType.LIMIT
        assert order.status == OrderStatus.OPEN


# ── MexcExchange ───────────────────────────────────────────────────────


class TestMexcExchange:
    @pytest.fixture
    def mexc(self):
        with patch("core.exchange.mexc.ccxt") as m_ccxt:
            m_ccxt.mexc.side_effect = lambda *a, **kw: MagicMock(
                load_markets=AsyncMock(),
                close=AsyncMock(),
                fetch_ticker=AsyncMock(return_value=_raw_ticker()),
                fetch_tickers=AsyncMock(return_value={"BTC/USDT": _raw_ticker()}),
                fetch_ohlcv=AsyncMock(return_value=_raw_ohlcv()),
                fetch_order_book=AsyncMock(return_value=_raw_order_book()),
                fetch_balance=AsyncMock(return_value={"USDT": {"free": 3_000.0, "used": 0.0}}),
                create_order=AsyncMock(
                    return_value={
                        "id": "mexc-1",
                        "symbol": "BTC/USDT",
                        "side": "buy",
                        "type": "market",
                        "status": "closed",
                        "filled": 0.01,
                        "average": 99.0,
                        "amount": 0.01,
                    }
                ),
                cancel_order=AsyncMock(return_value={"id": "mexc-1", "amount": 0.01}),
                fetch_order=AsyncMock(
                    return_value={
                        "id": "mexc-1",
                        "symbol": "BTC/USDT",
                        "side": "buy",
                        "type": "market",
                        "status": "closed",
                        "filled": 0.01,
                        "average": 99.0,
                        "amount": 0.01,
                    }
                ),
                fetch_open_orders=AsyncMock(return_value=[]),
                markets={"BTC/USDT": {}, "DOGE/USDT": {}},
            )
            from core.exchange.mexc import MexcExchange

            return MexcExchange(api_key="k", api_secret="s", sandbox=True)

    def test_name(self, mexc):
        assert mexc.name == "mexc"

    def test_supported_market_types_spot_only(self, mexc):
        assert mexc.SUPPORTED_MARKET_TYPES == ("spot",)
        assert mexc.supports("spot") is True
        assert mexc.supports("futures") is False

    def test_has_no_testnet(self, mexc):
        assert mexc.HAS_TESTNET is False

    @pytest.mark.asyncio
    async def test_connect_disconnect(self, mexc):
        await mexc.connect()
        mexc._spot.load_markets.assert_awaited_once()
        await mexc.disconnect()
        mexc._spot.close.assert_awaited_once()
        assert not hasattr(mexc, "_futures")

    @pytest.mark.asyncio
    async def test_fetch_positions_always_empty(self, mexc):
        positions = await mexc.fetch_positions()
        assert positions == []
        mexc._spot.fetch_positions.assert_not_called()

    @pytest.mark.asyncio
    async def test_place_order_spot_success(self, mexc):
        order = await mexc.place_order(
            "BTC/USDT",
            OrderSide.BUY,
            OrderType.MARKET,
            0.01,
            market_type=MarketType.SPOT,
        )
        assert order.id == "mexc-1"
        assert order.status == OrderStatus.FILLED
        assert order.market_type == "spot"
        mexc._spot.create_order.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_place_order_futures_returns_failed(self, mexc):
        order = await mexc.place_order(
            "BTC/USDT",
            OrderSide.BUY,
            OrderType.MARKET,
            0.1,
            leverage=10,
            market_type=MarketType.FUTURES,
        )
        assert order.status == OrderStatus.FAILED
        assert order.id == ""
        assert order.market_type == "spot"
        mexc._spot.create_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_available_symbols_futures_returns_empty(self, mexc):
        await mexc.connect()
        syms = await mexc.get_available_symbols(MarketType.FUTURES)
        assert syms == []

    @pytest.mark.asyncio
    async def test_get_available_symbols_spot(self, mexc):
        await mexc.connect()
        syms = await mexc.get_available_symbols(MarketType.SPOT)
        assert "BTC/USDT" in syms
        assert "DOGE/USDT" in syms

    @pytest.mark.asyncio
    async def test_set_leverage_no_op(self, mexc):
        await mexc.set_leverage("BTC/USDT", 20)
        # No exception; MEXC spot has no leverage

    @pytest.mark.asyncio
    async def test_fetch_candles_parsing(self, mexc):
        candles = await mexc.fetch_candles("DOGE/USDT", "5m", limit=5)
        assert len(candles) == 3
        assert all(isinstance(c, Candle) for c in candles)


# ── Error handling (all adapters) ────────────────────────────────────


class TestExchangeErrorHandling:
    @pytest.mark.asyncio
    async def test_binance_fetch_ticker_fails(self):
        with patch("core.exchange.binance.ccxt") as m_ccxt:
            spot = MagicMock()
            spot.load_markets = AsyncMock()
            spot.close = AsyncMock()
            spot.fetch_ticker = AsyncMock(side_effect=Exception("Rate limit"))
            futures = MagicMock()
            futures.load_markets = AsyncMock()
            futures.close = AsyncMock()
            m_ccxt.binance.side_effect = lambda *a, **kw: (
                spot if (a[0] if a else kw).get("options", {}).get("defaultType") == "spot" else futures
            )
            from core.exchange.binance import BinanceExchange

            exchange = BinanceExchange(sandbox=True)
            with pytest.raises(Exception, match="Rate limit"):
                await exchange.fetch_ticker("BTC/USDT")

    @pytest.mark.asyncio
    async def test_bybit_fetch_positions_api_error_returns_empty(self):
        with patch("core.exchange.bybit.ccxt") as m_ccxt:
            spot = MagicMock(load_markets=AsyncMock(), close=AsyncMock())
            futures = MagicMock(load_markets=AsyncMock(), close=AsyncMock())
            futures.fetch_positions = AsyncMock(side_effect=Exception("Connection error"))

            def _bybit(*args, **kw):
                opts = args[0] if args else kw
                return spot if opts.get("options", {}).get("defaultType") == "spot" else futures

            m_ccxt.bybit.side_effect = _bybit
            from core.exchange.bybit import BybitExchange

            exchange = BybitExchange(sandbox=True)
            positions = await exchange.fetch_positions()
            assert positions == []

    @pytest.mark.asyncio
    async def test_mexc_fetch_balance_filters_zero_free(self):
        with patch("core.exchange.mexc.ccxt") as m_ccxt:
            spot = MagicMock(
                load_markets=AsyncMock(),
                close=AsyncMock(),
                fetch_balance=AsyncMock(
                    return_value={
                        "USDT": {"free": 0.0, "used": 100.0},
                        "BTC": {"free": 0.5, "used": 0.0},
                    }
                ),
            )
            m_ccxt.mexc.return_value = spot
            from core.exchange.mexc import MexcExchange

            exchange = MexcExchange(sandbox=True)
            bal = await exchange.fetch_balance()
            assert "USDT" not in bal
            assert bal["BTC"] == 0.5
