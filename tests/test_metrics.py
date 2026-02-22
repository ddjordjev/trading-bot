"""Tests for web/metrics.py — instrumentation helpers, collection functions."""

from __future__ import annotations

import asyncio

import pytest

from web.metrics import (
    _collect_process,
    _collect_system,
    _is_coroutine_function,
    collect_metrics,
    record_event_loop_lag,
    record_tick,
    timed,
    timed_block,
)

# ── timed decorator ──────────────────────────────────────────────────────────


class TestTimedSync:
    def test_records_duration(self) -> None:
        @timed("test.sync_fn")
        def add(a: int, b: int) -> int:
            return a + b

        assert add(2, 3) == 5

    def test_records_error(self) -> None:
        @timed("test.sync_error")
        def boom() -> None:
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            boom()

    def test_auto_label(self) -> None:
        @timed()
        def my_func() -> str:
            return "ok"

        assert my_func() == "ok"


class TestTimedAsync:
    def test_records_duration(self) -> None:
        @timed("test.async_fn")
        async def fetch() -> int:
            return 42

        result = asyncio.get_event_loop().run_until_complete(fetch())
        assert result == 42

    def test_records_error(self) -> None:
        @timed("test.async_error")
        async def fail() -> None:
            raise RuntimeError("async boom")

        with pytest.raises(RuntimeError, match="async boom"):
            asyncio.get_event_loop().run_until_complete(fail())


# ── timed_block ──────────────────────────────────────────────────────────────


class TestTimedBlock:
    def test_normal_block(self) -> None:
        with timed_block("test.block"):
            x = 1 + 1
        assert x == 2

    def test_error_block(self) -> None:
        with pytest.raises(ValueError, match="block boom"):
            with timed_block("test.block_error"):
                raise ValueError("block boom")


# ── helpers ──────────────────────────────────────────────────────────────────


class TestHelpers:
    def test_record_tick(self) -> None:
        record_tick(0.5)

    def test_record_event_loop_lag(self) -> None:
        record_event_loop_lag(0.002)

    def test_is_coroutine_function_sync(self) -> None:
        def sync_fn() -> None:
            pass

        assert _is_coroutine_function(sync_fn) is False

    def test_is_coroutine_function_async(self) -> None:
        async def async_fn() -> None:
            pass

        assert _is_coroutine_function(async_fn) is True


# ── collection functions ─────────────────────────────────────────────────────


class TestCollect:
    def test_collect_system(self) -> None:
        _collect_system()

    def test_collect_process(self) -> None:
        _collect_process()

    def test_collect_metrics_returns_bytes(self) -> None:
        result = collect_metrics(None, 100.0)
        assert isinstance(result, bytes)
        assert b"system_cpu_percent" in result

    def test_collect_metrics_hub_mode(self) -> None:
        result = collect_metrics(None, 60.0)
        assert isinstance(result, bytes)
        assert b"system_cpu_percent" in result
