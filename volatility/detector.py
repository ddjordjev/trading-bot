from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import Optional

from loguru import logger
from pydantic import BaseModel, Field

from config.settings import Settings
from core.models import Ticker


class SpikeEvent(BaseModel):
    symbol: str
    direction: str  # "up" or "down"
    change_pct: float
    price: float
    volume_24h: float
    window_seconds: int
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    confirmed_by_news: bool = False
    news_headline: str = ""


class _PriceSnapshot(BaseModel):
    price: float
    timestamp: datetime
    volume: float


class VolatilityDetector:
    """Monitors price feeds for sudden spikes and unusual moves."""

    def __init__(self, settings: Settings):
        self.spike_threshold_pct = settings.spike_threshold_pct
        self.lookback_seconds = settings.volatility_lookback_minutes * 60
        self._price_buffers: dict[str, deque[_PriceSnapshot]] = {}
        self._recent_spikes: list[SpikeEvent] = []
        self._cooldown: dict[str, datetime] = {}
        self._cooldown_seconds = 60

    def update(self, ticker: Ticker) -> Optional[SpikeEvent]:
        """Feed a ticker update. Returns a SpikeEvent if a spike is detected."""
        symbol = ticker.symbol

        if symbol not in self._price_buffers:
            self._price_buffers[symbol] = deque(maxlen=1000)

        snap = _PriceSnapshot(price=ticker.last, timestamp=ticker.timestamp, volume=ticker.volume_24h)
        buf = self._price_buffers[symbol]
        buf.append(snap)

        self._trim_buffer(buf)

        if len(buf) < 2:
            return None

        if self._in_cooldown(symbol):
            return None

        oldest = buf[0]
        if oldest.price == 0:
            return None

        change_pct = (ticker.last - oldest.price) / oldest.price * 100

        if abs(change_pct) >= self.spike_threshold_pct:
            direction = "up" if change_pct > 0 else "down"
            elapsed = (ticker.timestamp - oldest.timestamp).total_seconds()

            event = SpikeEvent(
                symbol=symbol,
                direction=direction,
                change_pct=change_pct,
                price=ticker.last,
                volume_24h=ticker.volume_24h,
                window_seconds=int(elapsed),
            )

            self._recent_spikes.append(event)
            self._cooldown[symbol] = datetime.now(timezone.utc)

            logger.warning("SPIKE DETECTED: {} {:.2f}% {} in {}s (price: {:.6f})",
                           symbol, change_pct, direction, int(elapsed), ticker.last)
            return event

        return None

    def get_recent_spikes(self, last_n: int = 20) -> list[SpikeEvent]:
        return self._recent_spikes[-last_n:]

    def is_volatile(self, symbol: str, threshold_pct: Optional[float] = None) -> bool:
        """Check if a symbol is currently in a volatile state."""
        threshold = threshold_pct or self.spike_threshold_pct / 2
        buf = self._price_buffers.get(symbol)
        if not buf or len(buf) < 2:
            return False

        prices = [s.price for s in buf]
        min_p = min(prices)
        max_p = max(prices)
        if min_p == 0:
            return False
        return (max_p - min_p) / min_p * 100 >= threshold

    def _trim_buffer(self, buf: deque[_PriceSnapshot]) -> None:
        now = datetime.now(timezone.utc)
        while buf and (now - buf[0].timestamp).total_seconds() > self.lookback_seconds:
            buf.popleft()

    def _in_cooldown(self, symbol: str) -> bool:
        last = self._cooldown.get(symbol)
        if not last:
            return False
        return (datetime.now(timezone.utc) - last).total_seconds() < self._cooldown_seconds
