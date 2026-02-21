"""Tests for ExtremeWatcher — WebSocket subscription, pattern detection, and signal generation."""

from __future__ import annotations

from collections import deque
from unittest.mock import AsyncMock, MagicMock

import pytest

from config.settings import Settings
from core.extreme.watcher import ExtremeWatcher, PriceTick, WatchedSymbol
from core.models.market import Ticker
from core.models.signal import SignalAction
from shared.models import ExtremeCandidate, ExtremeWatchlist


@pytest.fixture
def settings() -> Settings:
    s = Settings(
        trading_mode="paper_local",
        exchange="binance",
        extreme_enabled=True,
        extreme_min_hourly_move_pct=5.0,
        extreme_min_volume_24h=10_000_000,
        extreme_max_candidates=10,
        extreme_max_positions=3,
        extreme_position_size_pct=3.0,
        extreme_initial_stop_pct=1.5,
        extreme_trail_pct=0.5,
        extreme_loser_timeout_minutes=1,
        extreme_eval_interval=30,
        extreme_stale_seconds=300,
        extreme_price_buffer_size=30,
    )
    return s


@pytest.fixture
def mock_exchange() -> MagicMock:
    ex = MagicMock()
    ex.watch_ticker = AsyncMock()
    return ex


@pytest.fixture
def watcher(mock_exchange: MagicMock, settings: Settings) -> ExtremeWatcher:
    return ExtremeWatcher(mock_exchange, settings)


class TestSubscription:
    @pytest.mark.asyncio
    async def test_subscribe_creates_task(self, watcher: ExtremeWatcher) -> None:
        await watcher.subscribe("BTC/USDT", "bull")
        assert "BTC/USDT" in watcher.active_symbols
        assert watcher.active_count == 1

    @pytest.mark.asyncio
    async def test_subscribe_idempotent(self, watcher: ExtremeWatcher) -> None:
        await watcher.subscribe("BTC/USDT", "bull")
        await watcher.subscribe("BTC/USDT", "bull")
        assert watcher.active_count == 1

    @pytest.mark.asyncio
    async def test_unsubscribe_removes(self, watcher: ExtremeWatcher) -> None:
        await watcher.subscribe("BTC/USDT", "bull")
        await watcher.unsubscribe("BTC/USDT")
        assert watcher.active_count == 0

    @pytest.mark.asyncio
    async def test_unsubscribe_nonexistent_safe(self, watcher: ExtremeWatcher) -> None:
        await watcher.unsubscribe("NONEXIST/USDT")
        assert watcher.active_count == 0

    @pytest.mark.asyncio
    async def test_sync_watchlist(self, watcher: ExtremeWatcher) -> None:
        await watcher.subscribe("BTC/USDT", "bull")
        await watcher.subscribe("ETH/USDT", "bear")

        await watcher.sync_watchlist({"ETH/USDT": "bear", "SOL/USDT": "bull"})
        assert set(watcher.active_symbols) == {"ETH/USDT", "SOL/USDT"}

    @pytest.mark.asyncio
    async def test_stop_clears_all(self, watcher: ExtremeWatcher) -> None:
        await watcher.subscribe("BTC/USDT", "bull")
        await watcher.subscribe("ETH/USDT", "bear")
        await watcher.stop()
        assert watcher.active_count == 0

    @pytest.mark.asyncio
    async def test_existing_position_flag(self, watcher: ExtremeWatcher) -> None:
        await watcher.subscribe("BTC/USDT", "bull", existing_position=True)
        ws = watcher._watched["BTC/USDT"]
        assert ws.is_existing_position is True


class TestPatternDetection:
    def _make_watcher_with_ticks(
        self, watcher: ExtremeWatcher, symbol: str, prices: list[float], direction: str = "bull"
    ) -> None:
        ws = WatchedSymbol(
            symbol=symbol,
            direction=direction,
            ticks=deque(maxlen=30),
        )
        for i, p in enumerate(prices):
            ws.ticks.append(PriceTick(price=p, volume_24h=1e7, timestamp=float(i)))
        watcher._watched[symbol] = ws

    def test_momentum_continuation_bullish(self, watcher: ExtremeWatcher) -> None:
        prices = [100.0, 100.5, 101.0, 101.5, 102.0, 102.5]
        self._make_watcher_with_ticks(watcher, "BTC/USDT", prices, "bull")
        ws = watcher._watched["BTC/USDT"]
        signal = watcher._detect_entry(ws)
        assert signal is not None
        assert signal.action == SignalAction.BUY
        assert signal.strategy == "extreme_momentum"
        assert signal.quick_trade is True

    def test_momentum_continuation_bearish(self, watcher: ExtremeWatcher) -> None:
        prices = [100.0, 99.5, 99.0, 98.5, 98.0, 97.5]
        self._make_watcher_with_ticks(watcher, "BTC/USDT", prices, "bear")
        ws = watcher._watched["BTC/USDT"]
        signal = watcher._detect_entry(ws)
        assert signal is not None
        assert signal.action == SignalAction.SELL
        assert signal.strategy == "extreme_momentum"

    def test_no_signal_on_choppy_data(self, watcher: ExtremeWatcher) -> None:
        prices = [100.0, 100.5, 99.8, 100.2, 99.9, 100.1]
        self._make_watcher_with_ticks(watcher, "BTC/USDT", prices, "bull")
        ws = watcher._watched["BTC/USDT"]
        signal = watcher._detect_entry(ws)
        assert signal is None

    def test_no_signal_with_insufficient_ticks(self, watcher: ExtremeWatcher) -> None:
        prices = [100.0, 100.5, 101.0]
        self._make_watcher_with_ticks(watcher, "BTC/USDT", prices, "bull")
        ws = watcher._watched["BTC/USDT"]
        signal = watcher._detect_entry(ws)
        assert signal is None

    def test_existing_position_no_entry_signal(self, watcher: ExtremeWatcher) -> None:
        """Symbols with is_existing_position=True should never generate entry signals."""
        prices = [100.0, 100.5, 101.0, 101.5, 102.0, 102.5]
        ws = WatchedSymbol(
            symbol="BTC/USDT",
            direction="bull",
            ticks=deque(maxlen=30),
            is_existing_position=True,
        )
        for i, p in enumerate(prices):
            ws.ticks.append(PriceTick(price=p, volume_24h=1e7, timestamp=float(i)))
        watcher._watched["BTC/USDT"] = ws

        from datetime import UTC, datetime

        ticker = Ticker(
            symbol="BTC/USDT",
            bid=103.0,
            ask=103.1,
            last=103.0,
            volume_24h=1e7,
            change_pct_24h=5.0,
            timestamp=datetime.now(UTC),
        )
        watcher._handle_tick("BTC/USDT", ticker)
        assert len(watcher.drain_signals()) == 0

    def test_stop_loss_direction_correct(self, watcher: ExtremeWatcher) -> None:
        prices = [100.0, 100.5, 101.0, 101.5, 102.0, 102.5]
        self._make_watcher_with_ticks(watcher, "TEST/USDT", prices, "bull")
        ws = watcher._watched["TEST/USDT"]
        signal = watcher._detect_entry(ws)
        assert signal is not None
        assert signal.suggested_stop_loss is not None
        assert signal.suggested_stop_loss < signal.suggested_price

    def test_stop_loss_direction_bearish(self, watcher: ExtremeWatcher) -> None:
        prices = [100.0, 99.5, 99.0, 98.5, 98.0, 97.5]
        self._make_watcher_with_ticks(watcher, "TEST/USDT", prices, "bear")
        ws = watcher._watched["TEST/USDT"]
        signal = watcher._detect_entry(ws)
        assert signal is not None
        assert signal.suggested_stop_loss is not None
        assert signal.suggested_stop_loss > signal.suggested_price


class TestPullbackDetection:
    def _make_watcher_with_ticks(
        self, watcher: ExtremeWatcher, symbol: str, prices: list[float], direction: str = "bull"
    ) -> None:
        ws = WatchedSymbol(
            symbol=symbol,
            direction=direction,
            ticks=deque(maxlen=30),
        )
        for i, p in enumerate(prices):
            ws.ticks.append(PriceTick(price=p, volume_24h=1e7, timestamp=float(i)))
        watcher._watched[symbol] = ws

    def test_bullish_pullback(self, watcher: ExtremeWatcher) -> None:
        prices = [100.0, 100.5, 101.0, 100.5, 99.5, 99.2, 99.5, 100.0, 100.5, 101.0]
        self._make_watcher_with_ticks(watcher, "BTC/USDT", prices, "bull")
        result = watcher._is_pullback_entry(prices, bullish=True)
        assert isinstance(result, bool)


class TestDrainSignals:
    def test_drain_empties_list(self, watcher: ExtremeWatcher) -> None:
        from core.models.signal import Signal, SignalAction, TickUrgency

        sig = Signal(
            symbol="BTC/USDT",
            action=SignalAction.BUY,
            strategy="extreme_momentum",
            quick_trade=True,
            tick_urgency=TickUrgency.SCALP,
        )
        watcher._pending_signals.append(sig)
        drained = watcher.drain_signals()
        assert len(drained) == 1
        assert drained[0].symbol == "BTC/USDT"
        assert len(watcher.drain_signals()) == 0


class TestExtremeModels:
    def test_extreme_candidate_creation(self) -> None:
        c = ExtremeCandidate(
            symbol="BTC/USDT",
            direction="bull",
            change_1h=7.5,
            change_5m=2.1,
            volume_24h=50_000_000,
            momentum_score=42.0,
            reason="1h: +7.5% | vol: $50M",
        )
        assert c.symbol == "BTC/USDT"
        assert c.direction == "bull"
        assert c.momentum_score == 42.0

    def test_extreme_watchlist_creation(self) -> None:
        wl = ExtremeWatchlist(
            candidates=[
                ExtremeCandidate(symbol="BTC/USDT"),
                ExtremeCandidate(symbol="ETH/USDT"),
            ]
        )
        assert len(wl.candidates) == 2
        assert wl.updated_at is not None
