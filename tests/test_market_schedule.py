"""Tests for core/market_schedule.py — DST-aware market hours, holidays, singleton."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from core.market_schedule import (
    MarketSchedule,
    MarketSession,
    _fetch_holidays_fmp,
    get_market_schedule,
    reset_market_schedule,
)

# ── MarketSession ────────────────────────────────────────────────────────────


class TestMarketSession:
    @pytest.fixture
    def nyse(self):
        return MarketSession(
            name="US",
            exchange_code="NYSE",
            timezone=ZoneInfo("America/New_York"),
            open_time=time(9, 30),
            close_time=time(16, 0),
        )

    def test_is_open_during_trading_hours(self, nyse):
        # Wednesday 2026-03-18 11:00 ET = within 9:30-16:00
        dt = datetime(2026, 3, 18, 11, 0, tzinfo=ZoneInfo("America/New_York"))
        assert nyse.is_open(dt) is True

    def test_is_closed_before_open(self, nyse):
        dt = datetime(2026, 3, 18, 8, 0, tzinfo=ZoneInfo("America/New_York"))
        assert nyse.is_open(dt) is False

    def test_is_closed_after_close(self, nyse):
        dt = datetime(2026, 3, 18, 17, 0, tzinfo=ZoneInfo("America/New_York"))
        assert nyse.is_open(dt) is False

    def test_is_closed_on_weekend(self, nyse):
        # Saturday 2026-03-21 12:00 ET
        dt = datetime(2026, 3, 21, 12, 0, tzinfo=ZoneInfo("America/New_York"))
        assert nyse.is_open(dt) is False
        assert nyse.is_weekend(dt) is True

    def test_is_closed_on_holiday(self, nyse):
        nyse.holidays = {date(2026, 12, 25)}
        dt = datetime(2026, 12, 25, 12, 0, tzinfo=ZoneInfo("America/New_York"))
        assert nyse.is_open(dt) is False
        assert nyse.is_holiday(dt) is True

    def test_early_close(self, nyse):
        # Day before Thanksgiving — early close at 13:00
        early_date = date(2026, 11, 27)
        nyse.early_closes = {early_date: time(13, 0)}
        # 12:30 ET — still open
        dt_open = datetime(2026, 11, 27, 12, 30, tzinfo=ZoneInfo("America/New_York"))
        assert nyse.is_open(dt_open) is True
        # 13:30 ET — closed early
        dt_closed = datetime(2026, 11, 27, 13, 30, tzinfo=ZoneInfo("America/New_York"))
        assert nyse.is_open(dt_closed) is False

    def test_dst_transition_spring_forward(self, nyse):
        # March 8, 2026 — US clocks spring forward (EST -> EDT)
        # 9:30 ET is 14:30 UTC in winter (EST) but 13:30 UTC in summer (EDT)
        # NYSE opens at 9:30 ET regardless of DST
        summer_open = datetime(2026, 6, 15, 10, 0, tzinfo=ZoneInfo("America/New_York"))
        winter_open = datetime(2026, 1, 15, 10, 0, tzinfo=ZoneInfo("America/New_York"))
        assert nyse.is_open(summer_open) is True
        assert nyse.is_open(winter_open) is True
        # The UTC hours differ, but local time is what matters
        assert summer_open.astimezone(UTC).hour != winter_open.astimezone(UTC).hour

    def test_is_in_open_window(self, nyse):
        # 10:00 ET = 30 min after open, within 120 min window
        dt = datetime(2026, 3, 18, 10, 0, tzinfo=ZoneInfo("America/New_York"))
        assert nyse.is_in_open_window(120, dt) is True
        # 13:00 ET = 3.5h after open, outside 120 min window
        dt_late = datetime(2026, 3, 18, 13, 0, tzinfo=ZoneInfo("America/New_York"))
        assert nyse.is_in_open_window(120, dt_late) is False

    def test_next_open_skips_weekend(self, nyse):
        # Friday 16:30 ET — next open should be Monday 9:30
        friday = datetime(2026, 3, 20, 16, 30, tzinfo=ZoneInfo("America/New_York"))
        next_open = nyse.next_open(friday)
        local_next = next_open.astimezone(ZoneInfo("America/New_York"))
        assert local_next.isoweekday() == 1  # Monday
        assert local_next.hour == 9
        assert local_next.minute == 30

    def test_next_open_skips_holiday(self, nyse):
        nyse.holidays = {date(2026, 3, 23)}  # Monday holiday
        friday = datetime(2026, 3, 20, 16, 30, tzinfo=ZoneInfo("America/New_York"))
        next_open = nyse.next_open(friday)
        local_next = next_open.astimezone(ZoneInfo("America/New_York"))
        assert local_next.date() == date(2026, 3, 24)  # Tuesday

    def test_next_close_when_open(self, nyse):
        dt = datetime(2026, 3, 18, 12, 0, tzinfo=ZoneInfo("America/New_York"))
        close = nyse.next_close(dt)
        local_close = close.astimezone(ZoneInfo("America/New_York"))
        assert local_close.hour == 16
        assert local_close.minute == 0

    def test_time_to_open(self, nyse):
        # 8:30 ET — 1 hour to open
        dt = datetime(2026, 3, 18, 8, 30, tzinfo=ZoneInfo("America/New_York"))
        ttopen = nyse.time_to_open(dt)
        assert 3500 < ttopen.total_seconds() < 3700  # ~1 hour

    def test_time_to_close_when_open(self, nyse):
        dt = datetime(2026, 3, 18, 15, 0, tzinfo=ZoneInfo("America/New_York"))
        ttclose = nyse.time_to_close(dt)
        assert 3500 < ttclose.total_seconds() < 3700  # ~1 hour


class TestTokyoSession:
    @pytest.fixture
    def tse(self):
        return MarketSession(
            name="ASIA",
            exchange_code="TSE",
            timezone=ZoneInfo("Asia/Tokyo"),
            open_time=time(9, 0),
            close_time=time(15, 0),
        )

    def test_is_open_jst(self, tse):
        dt = datetime(2026, 3, 18, 10, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
        assert tse.is_open(dt) is True

    def test_no_dst_in_japan(self, tse):
        # Japan doesn't observe DST — UTC offset is always +9
        summer = datetime(2026, 7, 15, 10, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
        winter = datetime(2026, 1, 15, 10, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
        assert summer.astimezone(UTC).hour == winter.astimezone(UTC).hour
        assert tse.is_open(summer) is True
        assert tse.is_open(winter) is True


# ── MarketSchedule ───────────────────────────────────────────────────────────


class TestMarketSchedule:
    @pytest.fixture(autouse=True)
    def clean_singleton(self):
        reset_market_schedule()
        yield
        reset_market_schedule()

    def test_default_sessions(self):
        sched = MarketSchedule()
        assert "US" in sched.sessions
        assert "ASIA" in sched.sessions
        assert "EUROPE" in sched.sessions
        assert "ASIA_HK" in sched.sessions

    def test_is_open_delegates_to_session(self):
        sched = MarketSchedule()
        dt = datetime(2026, 3, 18, 12, 0, tzinfo=ZoneInfo("America/New_York"))
        assert sched.is_open("US", dt) is True
        assert sched.is_open("NONEXISTENT", dt) is False

    def test_is_weekend(self):
        sched = MarketSchedule()
        saturday = datetime(2026, 3, 21, 12, 0, tzinfo=ZoneInfo("America/New_York"))
        assert sched.is_weekend("US", saturday) is True

    def test_current_open_markets(self):
        sched = MarketSchedule()
        # Wed 12:00 ET — US is open
        dt = datetime(2026, 3, 18, 12, 0, tzinfo=ZoneInfo("America/New_York"))
        markets = sched.current_open_markets(dt)
        assert "US" in markets

    def test_set_holidays(self):
        sched = MarketSchedule()
        sched.set_holidays("US", {date(2026, 7, 3)})
        dt = datetime(2026, 7, 3, 12, 0, tzinfo=ZoneInfo("America/New_York"))
        assert sched.is_holiday("US", dt) is True
        assert sched.is_open("US", dt) is False

    def test_set_early_closes(self):
        sched = MarketSchedule()
        sched.set_early_closes("US", {date(2026, 11, 27): time(13, 0)})
        dt = datetime(2026, 11, 27, 14, 0, tzinfo=ZoneInfo("America/New_York"))
        assert sched.is_open("US", dt) is False

    def test_next_open(self):
        sched = MarketSchedule()
        friday = datetime(2026, 3, 20, 17, 0, tzinfo=ZoneInfo("America/New_York"))
        nxt = sched.next_open("US", friday)
        assert nxt is not None
        local = nxt.astimezone(ZoneInfo("America/New_York"))
        assert local.isoweekday() == 1

    def test_next_open_unknown_market(self):
        sched = MarketSchedule()
        assert sched.next_open("MARS") is None

    def test_summary_returns_string(self):
        sched = MarketSchedule()
        s = sched.summary()
        assert "US" in s
        assert "ASIA" in s

    def test_configure(self):
        sched = MarketSchedule()
        sched.configure(fmp_api_key="test-key")
        assert sched._fmp_api_key == "test-key"


# ── Singleton ────────────────────────────────────────────────────────────────


class TestSingleton:
    @pytest.fixture(autouse=True)
    def clean(self):
        reset_market_schedule()
        yield
        reset_market_schedule()

    def test_get_returns_same_instance(self):
        a = get_market_schedule()
        b = get_market_schedule()
        assert a is b

    def test_reset_clears_instance(self):
        a = get_market_schedule()
        reset_market_schedule()
        b = get_market_schedule()
        assert a is not b

    def test_singleton_is_accessible_globally(self):
        sched = get_market_schedule()
        sched.set_holidays("US", {date(2026, 1, 1)})
        same = get_market_schedule()
        assert date(2026, 1, 1) in same.get_session("US").holidays


# ── Holiday Fetching ─────────────────────────────────────────────────────────


class TestHolidayFetching:
    @pytest.mark.asyncio
    async def test_fetch_holidays_fmp_success(self):
        mock_data = [
            {"date": "2026-01-01", "name": "New Year"},
            {"date": "2026-07-03", "name": "Independence Day"},
            {"date": "2025-12-25", "name": "Christmas 2025"},
        ]
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=mock_data)

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(
            return_value=MagicMock(
                __aenter__=AsyncMock(return_value=mock_resp),
                __aexit__=AsyncMock(return_value=False),
            )
        )

        with patch("core.market_schedule.aiohttp.ClientSession", return_value=mock_session):
            holidays = await _fetch_holidays_fmp("NYSE", "test-key", 2026)

        assert date(2026, 1, 1) in holidays
        assert date(2026, 7, 3) in holidays
        assert date(2025, 12, 25) not in holidays  # wrong year filtered out

    @pytest.mark.asyncio
    async def test_fetch_holidays_fmp_no_api_key(self):
        holidays = await _fetch_holidays_fmp("NYSE", "", 2026)
        assert holidays == []

    @pytest.mark.asyncio
    async def test_fetch_holidays_fmp_api_error(self):
        mock_resp = AsyncMock()
        mock_resp.status = 500

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(
            return_value=MagicMock(
                __aenter__=AsyncMock(return_value=mock_resp),
                __aexit__=AsyncMock(return_value=False),
            )
        )

        with patch("core.market_schedule.aiohttp.ClientSession", return_value=mock_session):
            holidays = await _fetch_holidays_fmp("NYSE", "test-key", 2026)
        assert holidays == []

    @pytest.mark.asyncio
    async def test_refresh_holidays_populates_sessions(self):
        sched = MarketSchedule()
        sched.configure(fmp_api_key="test-key")

        with patch("core.market_schedule._fetch_holidays_fmp", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = [date(2026, 1, 1), date(2026, 7, 3)]
            await sched.refresh_holidays(force=True)

        us = sched.get_session("US")
        assert date(2026, 1, 1) in us.holidays

    @pytest.mark.asyncio
    async def test_refresh_holidays_skips_if_recent(self):
        sched = MarketSchedule()
        sched.configure(fmp_api_key="test-key")
        sched._last_holiday_refresh = datetime.now(UTC)

        with patch("core.market_schedule._fetch_holidays_fmp", new_callable=AsyncMock) as mock_fetch:
            await sched.refresh_holidays(force=False)
            mock_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_refresh_holidays_force_ignores_cache(self):
        sched = MarketSchedule()
        sched.configure(fmp_api_key="test-key")
        sched._last_holiday_refresh = datetime.now(UTC)

        with patch("core.market_schedule._fetch_holidays_fmp", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = []
            await sched.refresh_holidays(force=True)
            assert mock_fetch.call_count >= 1
