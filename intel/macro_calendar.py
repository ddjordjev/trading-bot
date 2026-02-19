from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from enum import Enum

import aiohttp
from loguru import logger
from pydantic import BaseModel


class EventImpact(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"  # CPI, NFP
    CRITICAL = "critical"  # FOMC rate decisions


# Events that historically cause major crypto volatility
HIGH_IMPACT_KEYWORDS = {
    "critical": [
        "federal funds rate",
        "fomc",
        "interest rate decision",
        "monetary policy",
        "fed chair",
    ],
    "high": [
        "cpi",
        "consumer price index",
        "non-farm",
        "nonfarm",
        "nfp",
        "gdp",
        "unemployment",
        "ppi",
        "producer price",
        "retail sales",
        "pce",
        "core pce",
        "ism manufacturing",
        "ism services",
    ],
}


class MacroEvent(BaseModel):
    title: str
    date: datetime
    impact: EventImpact = EventImpact.LOW
    currency: str = "USD"
    forecast: str = ""
    previous: str = ""
    actual: str = ""

    @property
    def is_crypto_mover(self) -> bool:
        return self.impact in (EventImpact.HIGH, EventImpact.CRITICAL)

    @property
    def hours_until(self) -> float:
        delta = self.date - datetime.now(UTC)
        return delta.total_seconds() / 3600

    @property
    def is_imminent(self) -> bool:
        """Within 2 hours."""
        return 0 < self.hours_until <= 2

    @property
    def is_happening_now(self) -> bool:
        """Within 30 minutes of the event."""
        h = self.hours_until
        return -0.5 <= h <= 0.5


class MacroCalendar:
    """Monitors ForexFactory calendar for high-impact US economic events.

    Trading rules:
    - 2h before FOMC/CPI/NFP: REDUCE exposure (close weak positions, tighten stops)
    - During event: watch for spike entry (volatility scalp opportunity)
    - After event: trend follow the direction of the move

    Source: https://www.forexfactory.com/calendar

    Also recommends: https://www.investing.com/economic-calendar/
    """

    CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

    def __init__(self, poll_interval: int = 1800):
        self.poll_interval = poll_interval
        self._events: list[MacroEvent] = []
        self._running = False
        self._background_tasks: list = []

    async def start(self) -> None:
        self._running = True
        self._background_tasks.append(asyncio.create_task(self._poll_loop()))
        logger.info("Macro calendar started (poll={}s)", self.poll_interval)

    async def stop(self) -> None:
        self._running = False

    @property
    def upcoming_events(self) -> list[MacroEvent]:
        now = datetime.now(UTC)
        return [e for e in self._events if e.date > now - timedelta(hours=1)]

    @property
    def upcoming_high_impact(self) -> list[MacroEvent]:
        return [e for e in self.upcoming_events if e.is_crypto_mover]

    def has_imminent_event(self) -> bool:
        return any(e.is_imminent and e.is_crypto_mover for e in self._events)

    def has_event_now(self) -> bool:
        return any(e.is_happening_now and e.is_crypto_mover for e in self._events)

    def should_reduce_exposure(self) -> bool:
        """True if a high-impact event is within 2 hours -- tighten stops, reduce entries."""
        return self.has_imminent_event()

    def is_spike_opportunity(self) -> bool:
        """True during/right after a high-impact event -- spike scalp territory."""
        return self.has_event_now()

    def exposure_multiplier(self) -> float:
        """Reduce position sizing before high-impact events."""
        for e in self._events:
            if not e.is_crypto_mover:
                continue
            h = e.hours_until
            if h < 0:
                continue
            if e.impact == EventImpact.CRITICAL:
                if h <= 1:
                    return 0.3  # FOMC imminent: minimal exposure
                if h <= 2:
                    return 0.5
                if h <= 4:
                    return 0.7
            elif e.impact == EventImpact.HIGH:
                if h <= 1:
                    return 0.5
                if h <= 2:
                    return 0.7
        return 1.0

    def next_event_info(self) -> str | None:
        events = self.upcoming_high_impact
        if not events:
            return None
        e = events[0]
        return f"{e.title} in {e.hours_until:.1f}h ({e.impact.value})"

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._fetch()
            except Exception as e:
                logger.error("Macro calendar fetch error: {}", e)
            await asyncio.sleep(self.poll_interval)

    async def _fetch(self) -> None:
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(self.CALENDAR_URL, timeout=aiohttp.ClientTimeout(total=15)) as resp,
            ):
                if resp.status != 200:
                    logger.warning("ForexFactory calendar returned {}", resp.status)
                    return
                data = await resp.json()
        except Exception as e:
            logger.warning("ForexFactory fetch failed: {}", e)
            return

        events: list[MacroEvent] = []
        for item in data:
            try:
                title = item.get("title", "")
                country = item.get("country", "")
                if country != "USD":
                    continue

                date_str = item.get("date", "")
                if not date_str:
                    continue

                dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                impact = self._classify_impact(title, item.get("impact", ""))

                events.append(
                    MacroEvent(
                        title=title,
                        date=dt,
                        impact=impact,
                        currency=country,
                        forecast=str(item.get("forecast", "")),
                        previous=str(item.get("previous", "")),
                        actual=str(item.get("actual", "")),
                    )
                )
            except (ValueError, TypeError):
                continue

        self._events = sorted(events, key=lambda e: e.date)
        high = [e for e in events if e.is_crypto_mover]
        if high:
            logger.info("Macro calendar: {} events this week, {} high-impact", len(events), len(high))
            for e in high[:3]:
                logger.info("  {} | {} | in {:.0f}h", e.title, e.impact.value, e.hours_until)

    @staticmethod
    def _classify_impact(title: str, raw_impact: str) -> EventImpact:
        title_lower = title.lower()

        for kw in HIGH_IMPACT_KEYWORDS["critical"]:
            if kw in title_lower:
                return EventImpact.CRITICAL

        for kw in HIGH_IMPACT_KEYWORDS["high"]:
            if kw in title_lower:
                return EventImpact.HIGH

        if raw_impact:
            impact_map = {"low": EventImpact.LOW, "medium": EventImpact.MEDIUM, "high": EventImpact.HIGH}
            return impact_map.get(raw_impact.lower(), EventImpact.LOW)

        return EventImpact.LOW

    def summary(self) -> str:
        high = self.upcoming_high_impact
        if not high:
            return "Macro: no high-impact events upcoming"

        next_e = high[0]
        mult = self.exposure_multiplier()
        imminent = " ** IMMINENT **" if self.has_imminent_event() else ""
        return (
            f"Macro: {next_e.title} in {next_e.hours_until:.1f}h "
            f"({next_e.impact.value}) | exposure: {mult:.0%}{imminent}"
        )
