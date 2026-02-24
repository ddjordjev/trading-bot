"""Global market schedule with DST-aware hours, weekends, and holidays.

Stores market sessions in local time with proper IANA timezones so that
DST transitions are handled automatically by `zoneinfo`. Holiday data
is fetched from Financial Modeling Prep (free tier) on startup and
refreshed daily.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import aiohttp
from loguru import logger


@dataclass
class MarketSession:
    """A single exchange session defined in local time."""

    name: str
    exchange_code: str
    timezone: ZoneInfo
    open_time: time
    close_time: time
    weekend_days: tuple[int, ...] = (6, 7)  # Sat=6, Sun=7 (isoweekday)
    holidays: set[date] = field(default_factory=set)
    early_closes: dict[date, time] = field(default_factory=dict)

    def _local_now(self) -> datetime:
        return datetime.now(UTC).astimezone(self.timezone)

    @staticmethod
    def _as_aware_utc(dt: datetime | None) -> datetime | None:
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)

    def is_weekend(self, dt: datetime | None = None) -> bool:
        dt = self._as_aware_utc(dt)
        local = dt.astimezone(self.timezone) if dt else self._local_now()
        return local.isoweekday() in self.weekend_days

    def is_holiday(self, dt: datetime | None = None) -> bool:
        dt = self._as_aware_utc(dt)
        local = dt.astimezone(self.timezone) if dt else self._local_now()
        return local.date() in self.holidays

    def is_open(self, dt: datetime | None = None) -> bool:
        dt = self._as_aware_utc(dt)
        local = dt.astimezone(self.timezone) if dt else self._local_now()
        if self.is_weekend(dt) or self.is_holiday(dt):
            return False
        close = self.early_closes.get(local.date(), self.close_time)
        return self.open_time <= local.time() < close

    def is_in_open_window(self, window_minutes: int = 120, dt: datetime | None = None) -> bool:
        """True if we're within `window_minutes` of market open."""
        dt = self._as_aware_utc(dt)
        local = dt.astimezone(self.timezone) if dt else self._local_now()
        if self.is_weekend(dt) or self.is_holiday(dt):
            return False
        open_dt = local.replace(hour=self.open_time.hour, minute=self.open_time.minute, second=0, microsecond=0, fold=0)
        window_end = open_dt + timedelta(minutes=window_minutes)
        return open_dt <= local < window_end

    def next_open(self, dt: datetime | None = None) -> datetime:
        """Next market open as a UTC datetime."""
        dt = self._as_aware_utc(dt)
        local = dt.astimezone(self.timezone) if dt else self._local_now()
        candidate = local.replace(
            hour=self.open_time.hour, minute=self.open_time.minute, second=0, microsecond=0, fold=0
        )
        if candidate <= local:
            candidate += timedelta(days=1)
        for _ in range(10):
            if candidate.isoweekday() not in self.weekend_days and candidate.date() not in self.holidays:
                return candidate.astimezone(UTC)
            candidate += timedelta(days=1)
        return candidate.astimezone(UTC)

    def next_close(self, dt: datetime | None = None) -> datetime:
        """Next market close as a UTC datetime (accounts for early closes)."""
        dt = self._as_aware_utc(dt)
        local = dt.astimezone(self.timezone) if dt else self._local_now()
        close = self.early_closes.get(local.date(), self.close_time)
        candidate = local.replace(hour=close.hour, minute=close.minute, second=0, microsecond=0, fold=0)
        if candidate <= local:
            candidate += timedelta(days=1)
            close = self.early_closes.get(candidate.date(), self.close_time)
            candidate = candidate.replace(hour=close.hour, minute=close.minute, fold=0)
        for _ in range(10):
            if candidate.isoweekday() not in self.weekend_days and candidate.date() not in self.holidays:
                return candidate.astimezone(UTC)
            candidate += timedelta(days=1)
        return candidate.astimezone(UTC)

    def time_to_open(self, dt: datetime | None = None) -> timedelta:
        dt = self._as_aware_utc(dt)
        return self.next_open(dt) - (dt or datetime.now(UTC))

    def time_to_close(self, dt: datetime | None = None) -> timedelta:
        dt = self._as_aware_utc(dt)
        if self.is_open(dt):
            return self.next_close(dt) - (dt or datetime.now(UTC))
        return timedelta(0)


# ── Default Sessions ─────────────────────────────────────────────────────────

_US = MarketSession(
    name="US",
    exchange_code="NYSE",
    timezone=ZoneInfo("America/New_York"),
    open_time=time(9, 30),
    close_time=time(16, 0),
)

_ASIA_TOKYO = MarketSession(
    name="ASIA",
    exchange_code="TSE",
    timezone=ZoneInfo("Asia/Tokyo"),
    open_time=time(9, 0),
    close_time=time(15, 0),
)

_EUROPE = MarketSession(
    name="EUROPE",
    exchange_code="LSE",
    timezone=ZoneInfo("Europe/London"),
    open_time=time(8, 0),
    close_time=time(16, 30),
)

_ASIA_HK = MarketSession(
    name="ASIA_HK",
    exchange_code="HKEX",
    timezone=ZoneInfo("Asia/Hong_Kong"),
    open_time=time(9, 30),
    close_time=time(16, 0),
)


# ── Holiday Fetcher ──────────────────────────────────────────────────────────

FMP_HOLIDAYS_URL = "https://financialmodelingprep.com/stable/holidays-by-exchange"


async def _fetch_holidays_fmp(
    exchange: str,
    api_key: str,
    year: int | None = None,
) -> list[date]:
    """Fetch holiday dates from Financial Modeling Prep."""
    if not api_key:
        return []
    target_year = year or datetime.now(UTC).year
    url = f"{FMP_HOLIDAYS_URL}?exchange={exchange}&apikey={api_key}"
    holidays: list[date] = []
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp,
        ):
            if resp.status != 200:
                logger.warning("FMP holidays API returned {} for {}", resp.status, exchange)
                return []
            data = await resp.json()
            if not isinstance(data, list):
                return []
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                raw = entry.get("date", "")
                with contextlib.suppress(ValueError):
                    d = date.fromisoformat(raw)
                    if d.year == target_year:
                        holidays.append(d)
    except Exception as e:
        logger.warning("FMP holidays fetch failed for {}: {}", exchange, e)
    return holidays


# ── MarketSchedule Singleton ─────────────────────────────────────────────────


class MarketSchedule:
    """Global, DST-aware market schedule.

    Usage:
        schedule = get_market_schedule()
        if schedule.is_open("US"):
            ...
        next_us_open = schedule.next_open("US")
    """

    def __init__(self) -> None:
        self._sessions: dict[str, MarketSession] = {
            "US": _US,
            "ASIA": _ASIA_TOKYO,
            "EUROPE": _EUROPE,
            "ASIA_HK": _ASIA_HK,
        }
        self._fmp_api_key = ""
        self._last_holiday_refresh: datetime | None = None

    def configure(self, fmp_api_key: str = "") -> None:
        self._fmp_api_key = fmp_api_key

    @property
    def sessions(self) -> dict[str, MarketSession]:
        return dict(self._sessions)

    def get_session(self, market: str) -> MarketSession | None:
        return self._sessions.get(market.upper())

    def is_open(self, market: str, dt: datetime | None = None) -> bool:
        s = self._sessions.get(market.upper())
        return s.is_open(dt) if s else False

    def is_in_open_window(self, market: str, window_minutes: int = 120, dt: datetime | None = None) -> bool:
        s = self._sessions.get(market.upper())
        return s.is_in_open_window(window_minutes, dt) if s else False

    def is_weekend(self, market: str, dt: datetime | None = None) -> bool:
        s = self._sessions.get(market.upper())
        return s.is_weekend(dt) if s else False

    def is_holiday(self, market: str, dt: datetime | None = None) -> bool:
        s = self._sessions.get(market.upper())
        return s.is_holiday(dt) if s else False

    def next_open(self, market: str, dt: datetime | None = None) -> datetime | None:
        s = self._sessions.get(market.upper())
        return s.next_open(dt) if s else None

    def next_close(self, market: str, dt: datetime | None = None) -> datetime | None:
        s = self._sessions.get(market.upper())
        return s.next_close(dt) if s else None

    def current_open_markets(self, dt: datetime | None = None) -> list[str]:
        return [name for name, s in self._sessions.items() if s.is_open(dt)]

    def current_open_windows(self, window_minutes: int = 120, dt: datetime | None = None) -> list[str]:
        return [name for name, s in self._sessions.items() if s.is_in_open_window(window_minutes, dt)]

    def summary(self) -> str:
        lines = []
        for name, s in self._sessions.items():
            status = "OPEN" if s.is_open() else "CLOSED"
            local = s._local_now()
            holiday_count = len(s.holidays)
            lines.append(f"{name} ({s.exchange_code}): {status} | local={local:%H:%M} | holidays={holiday_count}")
        return " | ".join(lines)

    # ── Holiday Refresh ──────────────────────────────────────────────────

    async def refresh_holidays(self, force: bool = False) -> None:
        now = datetime.now(UTC)
        if not force and self._last_holiday_refresh and (now - self._last_holiday_refresh).total_seconds() < 86400:
            return

        year = now.year
        tasks = []
        for s in self._sessions.values():
            tasks.append(_fetch_holidays_fmp(s.exchange_code, self._fmp_api_key, year))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for session, result in zip(self._sessions.values(), results, strict=False):
            if isinstance(result, list):
                session.holidays = set(result)
                logger.info("Loaded {} holidays for {} ({})", len(result), session.name, session.exchange_code)
            elif isinstance(result, Exception):
                logger.warning("Holiday fetch failed for {}: {}", session.name, result)

        self._last_holiday_refresh = now

    def set_holidays(self, market: str, holidays: set[date]) -> None:
        s = self._sessions.get(market.upper())
        if s:
            s.holidays = holidays

    def set_early_closes(self, market: str, early_closes: dict[date, time]) -> None:
        s = self._sessions.get(market.upper())
        if s:
            s.early_closes = early_closes


# ── Global Singleton ─────────────────────────────────────────────────────────

_schedule: MarketSchedule | None = None


@lru_cache
def get_market_schedule() -> MarketSchedule:
    global _schedule
    if _schedule is None:
        _schedule = MarketSchedule()
    return _schedule


def reset_market_schedule() -> None:
    """For testing — clears the cached singleton."""
    global _schedule
    _schedule = None
    get_market_schedule.cache_clear()
