"""Prometheus metrics for the trading bot.

Three layers — industry standard for any Python service:
  1. System:  CPU / memory / disk of the host machine
  2. Process: RSS, CPU, threads, FDs, GC of this Python process
  3. Application: tick duration, exchange calls, strategy analysis, trading state

All system/process gauges are refreshed lazily at scrape time.
Application histograms are recorded in real time via `timed()`.
"""

from __future__ import annotations

import gc
import os
import time
from contextlib import contextmanager
from functools import wraps
from typing import TYPE_CHECKING

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

registry = CollectorRegistry()
_process = psutil.Process(os.getpid())

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

_tick_duration = Histogram(
    "bot_tick_duration_seconds",
    "Full tick loop execution time",
    buckets=DURATION_BUCKETS,
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


def timed(method_name: str | None = None):
    """Decorator that records execution time as a Prometheus histogram.

    Works on both sync and async functions.
    Usage:
        @timed("exchange.fetch_balance")
        async def fetch_balance(self): ...
    """

    def decorator(fn):
        label = method_name or f"{fn.__module__}.{fn.__qualname__}"

        if _is_coroutine_function(fn):

            @wraps(fn)
            async def async_wrapper(*args, **kwargs):
                start = time.perf_counter()
                try:
                    return await fn(*args, **kwargs)
                except Exception:
                    method_errors.labels(method=label).inc()
                    raise
                finally:
                    method_duration.labels(method=label).observe(time.perf_counter() - start)

            return async_wrapper
        else:

            @wraps(fn)
            def sync_wrapper(*args, **kwargs):
                start = time.perf_counter()
                try:
                    return fn(*args, **kwargs)
                except Exception:
                    method_errors.labels(method=label).inc()
                    raise
                finally:
                    method_duration.labels(method=label).observe(time.perf_counter() - start)

            return sync_wrapper

    return decorator


@contextmanager
def timed_block(name: str):
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


def _is_coroutine_function(fn) -> bool:
    import asyncio
    import inspect

    return asyncio.iscoroutinefunction(fn) or inspect.iscoroutinefunction(fn)


# ── Scrape-time collection ────────────────────────────────────────────────────


def _collect_system() -> None:
    try:
        _sys_cpu_pct.set(psutil.cpu_percent(interval=None))

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

        try:
            _proc_fds.set(_process.num_fds())
        except AttributeError:
            pass

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
    _open_positions.set(len(bot.orders.trailing.active_stops))
    _trades_today.set(bot.risk._total_trades_today)
    _win_rate.set(bot.risk.win_rate_today)
    _strategies_count.set(len(bot._strategies) + len(bot._dynamic_strategies))

    if bot.intel:
        _fear_greed.set(bot.intel.condition.fear_greed)
    else:
        _fear_greed.set(50)


def collect_metrics(bot: TradingBot | None, uptime: float) -> bytes:
    """Refresh all gauges and return Prometheus text exposition."""
    _collect_system()
    _collect_process()
    _collect_trading(bot)
    return generate_latest(registry)
