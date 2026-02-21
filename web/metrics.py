"""Prometheus metrics for the trading bot.

Three layers — industry standard for any Python service:
  1. System:  CPU / memory / disk of the host machine
  2. Process: RSS, CPU, threads, FDs, GC of this Python process
  3. Application: tick duration, exchange calls, strategy analysis, trading state

All system/process gauges are refreshed lazily at scrape time.
Application histograms are recorded in real time via `timed()`.
"""

from __future__ import annotations

import contextlib
import gc
import os
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from functools import wraps
from typing import TYPE_CHECKING, Any, TypeVar

import psutil
from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

if TYPE_CHECKING:
    from bot import TradingBot

F = TypeVar("F", bound=Callable[..., Any])

registry = CollectorRegistry()
_process = psutil.Process(os.getpid())

# Prime psutil's internal "last call" baseline so the first real scrape
# returns a meaningful delta instead of 0.0.
psutil.cpu_percent(interval=None)
_process.cpu_percent(interval=None)

# ── System metrics ────────────────────────────────────────────────────────────

_sys_cpu_pct = Gauge("system_cpu_percent", "System-wide CPU usage %", registry=registry)
_sys_mem_total = Gauge("system_memory_total_bytes", "Total physical memory", registry=registry)
_sys_mem_used = Gauge("system_memory_used_bytes", "Used physical memory", registry=registry)
_sys_mem_pct = Gauge("system_memory_percent", "Memory usage %", registry=registry)
_sys_mem_available = Gauge("system_memory_available_bytes", "Available memory", registry=registry)
_sys_swap_used = Gauge("system_swap_used_bytes", "Swap used", registry=registry)
_sys_swap_pct = Gauge("system_swap_percent", "Swap usage %", registry=registry)
_sys_disk_total = Gauge("system_disk_total_bytes", "Disk total (root partition)", registry=registry)
_sys_disk_used = Gauge("system_disk_used_bytes", "Disk used", registry=registry)
_sys_disk_pct = Gauge("system_disk_percent", "Disk usage %", registry=registry)
_sys_disk_free = Gauge("system_disk_free_bytes", "Disk free", registry=registry)
_sys_net_sent = Gauge("system_net_bytes_sent", "Network bytes sent", registry=registry)
_sys_net_recv = Gauge("system_net_bytes_recv", "Network bytes received", registry=registry)
_sys_load_1m = Gauge("system_load_1m", "1-minute load average", registry=registry)
_sys_load_5m = Gauge("system_load_5m", "5-minute load average", registry=registry)
_sys_load_15m = Gauge("system_load_15m", "15-minute load average", registry=registry)

# ── Process metrics ───────────────────────────────────────────────────────────

_proc_cpu_pct = Gauge("process_cpu_percent", "Bot process CPU %", registry=registry)
_proc_rss = Gauge("process_memory_rss_bytes", "Resident set size", registry=registry)
_proc_vms = Gauge("process_memory_vms_bytes", "Virtual memory size", registry=registry)
_proc_mem_pct = Gauge("process_memory_percent", "Process memory % of total", registry=registry)
_proc_threads = Gauge("process_threads", "Thread count", registry=registry)
_proc_fds = Gauge("process_open_fds", "Open file descriptors", registry=registry)
_proc_uptime = Gauge("process_uptime_seconds", "Process uptime", registry=registry)

_gc_collections = Gauge("python_gc_collections_total", "GC collections", ["generation"], registry=registry)
_gc_objects = Gauge("python_gc_objects", "Tracked GC objects", registry=registry)

# ── Application: method timing ────────────────────────────────────────────────

DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

method_duration = Histogram(
    "bot_method_duration_seconds",
    "Execution time of instrumented methods",
    ["method"],
    buckets=DURATION_BUCKETS,
    registry=registry,
)

method_errors = Counter(
    "bot_method_errors_total",
    "Errors in instrumented methods",
    ["method"],
    registry=registry,
)

# ── Application: tick-level metrics ───────────────────────────────────────────

TICK_BUCKETS = (0.5, 1.0, 2.5, 5.0, 10.0, 15.0, 20.0, 30.0, 45.0, 60.0, 90.0, 120.0)

_tick_duration = Histogram(
    "bot_tick_duration_seconds",
    "Full tick loop execution time",
    buckets=TICK_BUCKETS,
    registry=registry,
)
_tick_total = Counter("bot_ticks_total", "Total ticks executed", registry=registry)

# ── Application: trading state ────────────────────────────────────────────────

_balance = Gauge("trading_bot_balance", "Current account balance (USD)", registry=registry)
_daily_pnl = Gauge("trading_bot_daily_pnl_usd", "Today's PnL in USD", registry=registry)
_daily_pnl_pct = Gauge("trading_bot_daily_pnl_pct", "Today's PnL %", registry=registry)
_daily_loss_pct = Gauge("trading_bot_daily_loss_pct", "Unrealized daily loss %", registry=registry)
_open_positions = Gauge("trading_bot_open_positions", "Number of open positions", registry=registry)
_trades_today = Gauge("trading_bot_trades_today", "Trades executed today", registry=registry)
_win_rate = Gauge("trading_bot_win_rate", "Win rate today (%)", registry=registry)
_fear_greed = Gauge("trading_bot_fear_greed", "Fear & Greed index (0-100)", registry=registry)
_strategies_count = Gauge("trading_bot_strategies", "Registered strategies", registry=registry)
_event_loop_lag = Gauge("bot_event_loop_lag_seconds", "Async event loop lag", registry=registry)

# Pre-create generation labels
for _gen in ("0", "1", "2"):
    _gc_collections.labels(generation=_gen)


# ── Instrumentation helpers ───────────────────────────────────────────────────


def timed(method_name: str | None = None) -> Callable[[F], F]:
    """Decorator that records execution time as a Prometheus histogram.

    Works on both sync and async functions.
    Usage:
        @timed("exchange.fetch_balance")
        async def fetch_balance(self): ...
    """

    def decorator(fn: F) -> F:
        label = method_name or f"{fn.__module__}.{fn.__qualname__}"

        if _is_coroutine_function(fn):

            @wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                start = time.perf_counter()
                try:
                    return await fn(*args, **kwargs)
                except Exception:
                    method_errors.labels(method=label).inc()
                    raise
                finally:
                    method_duration.labels(method=label).observe(time.perf_counter() - start)

            return async_wrapper  # type: ignore[return-value]
        else:

            @wraps(fn)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                start = time.perf_counter()
                try:
                    return fn(*args, **kwargs)
                except Exception:
                    method_errors.labels(method=label).inc()
                    raise
                finally:
                    method_duration.labels(method=label).observe(time.perf_counter() - start)

            return sync_wrapper  # type: ignore[return-value]

    return decorator


@contextmanager
def timed_block(name: str) -> Generator[None, None, None]:
    """Context manager for timing arbitrary blocks of code."""
    start = time.perf_counter()
    try:
        yield
    except Exception:
        method_errors.labels(method=name).inc()
        raise
    finally:
        method_duration.labels(method=name).observe(time.perf_counter() - start)


def record_tick(duration: float) -> None:
    """Called at the end of each tick loop."""
    _tick_duration.observe(duration)
    _tick_total.inc()


def record_event_loop_lag(lag: float) -> None:
    _event_loop_lag.set(lag)


def _is_coroutine_function(fn: Callable[..., Any]) -> bool:
    import asyncio
    import inspect

    return asyncio.iscoroutinefunction(fn) or inspect.iscoroutinefunction(fn)


# ── Scrape-time collection ────────────────────────────────────────────────────


def _read_cgroup_cpu() -> float | None:
    """Read container CPU usage from cgroup v2 (or v1 fallback).

    Returns a percentage (0-100) relative to assigned CPU quota, or None
    if not running inside a cgroup-limited container.
    """
    try:
        # cgroup v2
        with open("/sys/fs/cgroup/cpu.stat") as f:
            for line in f:
                if line.startswith("usage_usec"):
                    return float(line.split()[1])
    except FileNotFoundError:
        pass
    try:
        # cgroup v1
        with open("/proc/self/cgroup") as _, open("/sys/fs/cgroup/cpuacct/cpuacct.usage") as f:
            return float(f.read().strip()) / 1000  # ns → us
    except FileNotFoundError:
        pass
    return None


_last_cgroup_cpu_us: float | None = _read_cgroup_cpu()
_last_cgroup_time: float = time.monotonic()


def _cgroup_cpu_pct() -> float | None:
    """Compute container CPU % from cgroup delta between scrapes."""
    global _last_cgroup_cpu_us, _last_cgroup_time
    current = _read_cgroup_cpu()
    if current is None or _last_cgroup_cpu_us is None:
        _last_cgroup_cpu_us = current
        _last_cgroup_time = time.monotonic()
        return None
    now = time.monotonic()
    elapsed_us = (now - _last_cgroup_time) * 1_000_000
    if elapsed_us < 1000:
        return None
    cpu_pct = (current - _last_cgroup_cpu_us) / elapsed_us * 100
    _last_cgroup_cpu_us = current
    _last_cgroup_time = now
    return max(0.0, min(cpu_pct, 100.0))


def _collect_system() -> None:
    try:
        cg = _cgroup_cpu_pct()
        _sys_cpu_pct.set(cg if cg is not None else psutil.cpu_percent(interval=None))

        mem = psutil.virtual_memory()
        _sys_mem_total.set(mem.total)
        _sys_mem_used.set(mem.used)
        _sys_mem_pct.set(mem.percent)
        _sys_mem_available.set(mem.available)

        swap = psutil.swap_memory()
        _sys_swap_used.set(swap.used)
        _sys_swap_pct.set(swap.percent)

        disk = psutil.disk_usage("/")
        _sys_disk_total.set(disk.total)
        _sys_disk_used.set(disk.used)
        _sys_disk_pct.set(disk.percent)
        _sys_disk_free.set(disk.free)

        net = psutil.net_io_counters()
        _sys_net_sent.set(net.bytes_sent)
        _sys_net_recv.set(net.bytes_recv)

        load = os.getloadavg()
        _sys_load_1m.set(load[0])
        _sys_load_5m.set(load[1])
        _sys_load_15m.set(load[2])
    except Exception:
        pass


def _collect_process() -> None:
    try:
        _proc_cpu_pct.set(_process.cpu_percent(interval=None))

        mem = _process.memory_info()
        _proc_rss.set(mem.rss)
        _proc_vms.set(mem.vms)
        _proc_mem_pct.set(_process.memory_percent())
        _proc_threads.set(_process.num_threads())

        with contextlib.suppress(AttributeError):
            _proc_fds.set(_process.num_fds())

        _proc_uptime.set(time.time() - _process.create_time())

        stats = gc.get_stats()
        for i, s in enumerate(stats):
            _gc_collections.labels(generation=str(i)).set(s["collections"])
        _gc_objects.set(len(gc.get_objects()))
    except Exception:
        pass


def _collect_trading(bot: TradingBot | None) -> None:
    if not bot:
        return
    _balance.set(bot.target._current_balance)
    _daily_pnl.set(bot.target.todays_pnl)
    _daily_pnl_pct.set(bot.target.todays_pnl_pct)
    _daily_loss_pct.set(bot.risk.daily_loss_pct)
    _open_positions.set(len(bot.orders.scaler.active_positions))
    _trades_today.set(bot.risk._total_trades_today)
    _win_rate.set(bot.risk.win_rate_today)
    _strategies_count.set(len(bot._strategies) + len(bot._dynamic_strategies))

    if bot.intel and bot.intel.condition:
        _fear_greed.set(bot.intel.condition.fear_greed)
    else:
        _fear_greed.set(50)


def collect_metrics(bot: TradingBot | None, uptime: float) -> bytes:
    """Refresh all gauges and return Prometheus text exposition."""
    _collect_system()
    _collect_process()
    _collect_trading(bot)
    return generate_latest(registry)


def get_metrics_json(bot: TradingBot | None, uptime: float) -> dict[str, Any]:
    """Return key metrics as JSON for the built-in monitoring dashboard."""
    _collect_system()
    _collect_process()
    _collect_trading(bot)

    def _val(g: Gauge) -> float:
        try:
            return float(g._value.get())
        except Exception:
            return 0.0

    return {
        "uptime_seconds": round(uptime, 1),
        "system": {
            "cpu_pct": round(_val(_sys_cpu_pct), 1),
            "mem_used_gb": round(_val(_sys_mem_used) / (1024**3), 2),
            "mem_total_gb": round(_val(_sys_mem_total) / (1024**3), 2),
            "mem_pct": round(_val(_sys_mem_pct), 1),
            "disk_used_gb": round(_val(_sys_disk_used) / (1024**3), 1),
            "disk_free_gb": round(_val(_sys_disk_free) / (1024**3), 1),
            "disk_pct": round(_val(_sys_disk_pct), 1),
        },
        "process": {
            "cpu_pct": round(_val(_proc_cpu_pct), 1),
            "rss_mb": round(_val(_proc_rss) / (1024**2), 1),
            "threads": int(_val(_proc_threads)),
            "uptime_hours": round(_val(_proc_uptime) / 3600, 1),
        },
    }
