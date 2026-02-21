"""ExtremeWatcher — WebSocket subscription manager for extreme movers.

Handles:
- Subscribe/unsubscribe to real-time ticker feeds for approved symbols
- Buffer recent price ticks per symbol for pattern detection
- Detect entry patterns (momentum continuation, pullback entry)
- Track existing positions promoted to WS monitoring for exit management
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from loguru import logger

from config.settings import Settings
from core.models.market import Ticker
from core.models.signal import Signal, SignalAction, TickUrgency

if TYPE_CHECKING:
    from collections.abc import Callable

    from core.exchange.base import BaseExchange


@dataclass
class PriceTick:
    price: float
    volume_24h: float
    timestamp: float  # monotonic


@dataclass
class WatchedSymbol:
    symbol: str
    direction: str  # "bull" or "bear"
    ticks: deque[PriceTick] = field(default_factory=lambda: deque(maxlen=30))
    task: asyncio.Task[None] | None = None
    subscribed_at: float = field(default_factory=time.monotonic)
    is_existing_position: bool = False  # True = exit management only, no new entry


class ExtremeWatcher:
    """Manages WebSocket subscriptions for extreme movers."""

    def __init__(self, exchange: BaseExchange, settings: Settings) -> None:
        self._exchange = exchange
        self._settings = settings
        self._watched: dict[str, WatchedSymbol] = {}
        self._pending_signals: list[Signal] = []
        self._lock = asyncio.Lock()

    @property
    def active_symbols(self) -> list[str]:
        return list(self._watched.keys())

    @property
    def active_count(self) -> int:
        return len(self._watched)

    def drain_signals(self) -> list[Signal]:
        """Return and clear any pending entry signals."""
        signals = self._pending_signals
        self._pending_signals = []
        return signals

    async def subscribe(self, symbol: str, direction: str, *, existing_position: bool = False) -> None:
        if symbol in self._watched:
            return

        ws = WatchedSymbol(
            symbol=symbol,
            direction=direction,
            ticks=deque(maxlen=self._settings.extreme_price_buffer_size),
            is_existing_position=existing_position,
        )
        self._watched[symbol] = ws

        async def _on_tick(ticker: Ticker) -> None:
            async with self._lock:
                self._handle_tick(symbol, ticker)

        ws.task = asyncio.create_task(self._ws_loop(symbol, _on_tick))
        tag = "exit-watch" if existing_position else "entry-hunt"
        logger.info("Extreme WS subscribe: {} ({}) [{}]", symbol, direction, tag)

    async def unsubscribe(self, symbol: str) -> None:
        ws = self._watched.pop(symbol, None)
        if ws and ws.task:
            ws.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await ws.task
        logger.info("Extreme WS unsubscribe: {}", symbol)

    async def sync_watchlist(
        self,
        approved: dict[str, str],
        existing_positions: set[str] | None = None,
    ) -> None:
        """Reconcile subscriptions. approved = {symbol: direction}.
        existing_positions = symbols that are open positions being promoted.
        """
        existing_positions = existing_positions or set()
        current = set(self._watched.keys())
        desired = set(approved.keys()) | existing_positions

        for sym in current - desired:
            await self.unsubscribe(sym)

        for sym in desired - current:
            direction = approved.get(sym, "bull")
            is_existing = sym in existing_positions
            await self.subscribe(sym, direction, existing_position=is_existing)

    async def stop(self) -> None:
        for sym in list(self._watched):
            await self.unsubscribe(sym)

    async def _ws_loop(self, symbol: str, callback: Callable[..., Any]) -> None:
        """Wrapper that calls exchange.watch_ticker in a managed loop."""
        try:
            await self._exchange.watch_ticker(symbol, callback)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Extreme WS error for {}: {}", symbol, e)

    def _handle_tick(self, symbol: str, ticker: Ticker) -> None:
        ws = self._watched.get(symbol)
        if not ws:
            return

        tick = PriceTick(
            price=ticker.last,
            volume_24h=ticker.volume_24h,
            timestamp=time.monotonic(),
        )
        ws.ticks.append(tick)

        if ws.is_existing_position:
            return

        if len(ws.ticks) < 5:
            return

        signal = self._detect_entry(ws)
        if signal:
            self._pending_signals.append(signal)

    def _detect_entry(self, ws: WatchedSymbol) -> Signal | None:
        """Run pattern detection on the buffered ticks."""
        ticks = list(ws.ticks)
        if len(ticks) < 5:
            return None

        prices = [t.price for t in ticks]
        latest = prices[-1]
        oldest = prices[0]
        if oldest == 0:
            return None

        move_pct = ((latest - oldest) / oldest) * 100
        is_bullish = ws.direction == "bull"

        # Use bot's preferred market type and leverage so orders hit the correct product
        market_type = "futures" if self._settings.futures_allowed else "spot"
        leverage = self._settings.default_leverage

        # Momentum continuation: price consistently moving in the extreme direction
        if self._is_momentum_continuation(prices, is_bullish):
            stop_pct = self._settings.extreme_initial_stop_pct / 100
            stop = latest * (1 - stop_pct) if is_bullish else latest * (1 + stop_pct)

            return Signal(
                symbol=ws.symbol,
                action=SignalAction.BUY if is_bullish else SignalAction.SELL,
                strength=min(abs(move_pct) / 10, 1.0),
                strategy="extreme_momentum",
                reason=f"Extreme momentum continuation {move_pct:+.2f}% over {len(ticks)} ticks",
                suggested_price=latest,
                suggested_stop_loss=stop,
                quick_trade=True,
                max_hold_minutes=self._settings.extreme_loser_timeout_minutes,
                tick_urgency=TickUrgency.SCALP,
                market_type=market_type,
                leverage=leverage,
            )

        # Pullback entry: dipped then resumed direction
        if self._is_pullback_entry(prices, is_bullish):
            stop_pct = self._settings.extreme_initial_stop_pct / 100
            stop = latest * (1 - stop_pct) if is_bullish else latest * (1 + stop_pct)

            return Signal(
                symbol=ws.symbol,
                action=SignalAction.BUY if is_bullish else SignalAction.SELL,
                strength=min(abs(move_pct) / 8, 1.0),
                strategy="extreme_pullback",
                reason=f"Extreme pullback entry after {move_pct:+.2f}% move",
                suggested_price=latest,
                suggested_stop_loss=stop,
                quick_trade=True,
                max_hold_minutes=self._settings.extreme_loser_timeout_minutes,
                tick_urgency=TickUrgency.SCALP,
                market_type=market_type,
                leverage=leverage,
            )

        return None

    def _is_momentum_continuation(self, prices: list[float], bullish: bool) -> bool:
        """At least 4 of last 5 ticks move in the expected direction."""
        if len(prices) < 5:
            return False

        recent = prices[-5:]
        moves_in_direction = 0
        for i in range(1, len(recent)):
            if (bullish and recent[i] > recent[i - 1]) or (not bullish and recent[i] < recent[i - 1]):
                moves_in_direction += 1

        return moves_in_direction >= 4

    def _is_pullback_entry(self, prices: list[float], bullish: bool) -> bool:
        """Price pulled back then resumed. Look for V or inverted-V in last 8-10 ticks."""
        if len(prices) < 8:
            return False

        recent = prices[-10:] if len(prices) >= 10 else prices[-8:]
        if bullish:
            trough_idx = min(range(len(recent)), key=lambda i: recent[i])
            if trough_idx < 2 or trough_idx > len(recent) - 3:
                return False
            pre_dip = recent[0]
            trough = recent[trough_idx]
            recovery = recent[-1]
            dip_pct = ((pre_dip - trough) / pre_dip) * 100 if pre_dip else 0
            recovery_pct = ((recovery - trough) / trough) * 100 if trough else 0
            return 0.3 <= dip_pct <= 2.0 and recovery_pct > dip_pct * 0.6
        else:
            peak_idx = max(range(len(recent)), key=lambda i: recent[i])
            if peak_idx < 2 or peak_idx > len(recent) - 3:
                return False
            pre_spike = recent[0]
            peak = recent[peak_idx]
            drop = recent[-1]
            spike_pct = ((peak - pre_spike) / pre_spike) * 100 if pre_spike else 0
            drop_pct = ((peak - drop) / peak) * 100 if peak else 0
            return 0.3 <= spike_pct <= 2.0 and drop_pct > spike_pct * 0.6
