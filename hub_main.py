#!/usr/bin/env python3
"""Hub entry point — the central brain of the trading system.

NOT a TradingBot.  This is a FastAPI app that runs:
  - MonitorService (intel polling, signal generation)
  - AnalyticsService (strategy scoring)
  - Web dashboard + /internal endpoints for bots

All services share a single HubState object (in-memory IPC).
No exchange connection, no risk manager, no order manager.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import uvicorn
from loguru import logger

from config.settings import get_settings
from hub.state import HubState
from notifications.notifier import NotificationType, Notifier
from services.analytics_service import AnalyticsService
from services.monitor import MonitorService
from services.openclaw_advisor_service import OpenClawAdvisorService
from web.server import (
    _bot_reports,
    app,
    set_hub_state,
    set_monitor_service,
    set_openclaw_advisor_service,
    setup_log_capture,
)

_hub_state: HubState | None = None
_monitor: MonitorService | None = None
_analytics: AnalyticsService | None = None
_openclaw_advisor: OpenClawAdvisorService | None = None
_notifier: Notifier | None = None
_background_tasks: list[asyncio.Task] = []  # type: ignore[type-arg]


async def _daily_report_loop() -> None:
    """Send one consolidated compound report at midnight UTC."""
    sent_today: str = ""
    while True:
        try:
            now = datetime.now(UTC)
            today_str = now.strftime("%Y-%m-%d")

            if now.hour == 0 and now.minute < 5 and sent_today != today_str:
                await asyncio.sleep(30)
                await _send_compound_daily_report()
                sent_today = today_str

            await asyncio.sleep(30)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Daily report loop error: {}", e)
            await asyncio.sleep(60)


async def _send_compound_daily_report() -> None:
    """Build and send a single compound daily report across all bots."""
    if not _notifier:
        return

    reports = list(_bot_reports.values())
    if not reports:
        logger.info("No bot reports available for daily summary")
        return

    now = datetime.now(UTC)
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append("     TRADE BORG — DAILY COMPOUND REPORT")
    lines.append(f"     {now.strftime('%Y-%m-%d')}")
    lines.append("=" * 60)
    lines.append("")

    total_balance = 0.0
    total_pnl = 0.0
    total_trades = 0
    total_positions = 0
    bot_sections: list[str] = []

    for rpt in sorted(reports, key=lambda r: r.get("bot_id", "")):
        bid = rpt.get("bot_id", "unknown")
        daily = rpt.get("daily_report", {})
        status = rpt.get("status", {})

        bal = status.get("balance", 0.0)
        pnl = status.get("daily_pnl", 0.0)
        pnl_pct = status.get("daily_pnl_pct", 0.0)
        tier = status.get("tier", "?")
        history = daily.get("history") or []
        trades = history[-1].get("trades", 0) if history else 0
        positions = len(rpt.get("positions", []))

        total_balance += bal
        total_pnl += pnl
        total_positions += positions
        total_trades += trades

        strats = ", ".join(s.get("name", "") for s in rpt.get("strategies", []))
        section = (
            f"  [{bid.upper()}] {tier.upper()}\n"
            f"    Balance: ${bal:,.2f}  |  PnL: {pnl:+,.2f} ({pnl_pct:+.1f}%)\n"
            f"    Trades: {trades}  |  Positions: {positions}\n"
            f"    Strategies: {strats or 'none'}"
        )
        bot_sections.append(section)

    total_pnl_pct = (total_pnl / (total_balance - total_pnl) * 100) if (total_balance - total_pnl) > 0 else 0.0

    lines.append(f"  TOTAL BALANCE:   ${total_balance:>12,.2f}")
    lines.append(f"  TOTAL PnL:       {total_pnl:>+12,.2f} ({total_pnl_pct:+.1f}%)")
    lines.append(f"  TOTAL TRADES:    {total_trades:>12d}")
    lines.append(f"  OPEN POSITIONS:  {total_positions:>12d}")
    lines.append(f"  ACTIVE BOTS:     {len(reports):>12d}")
    lines.append("")
    lines.append("-" * 60)
    lines.append("  PER-BOT BREAKDOWN")
    lines.append("-" * 60)
    lines.append("")
    lines.extend(bot_sections)
    lines.append("")

    first_compound = ""
    for rpt in reports:
        cr = rpt.get("daily_report", {}).get("compound_report", "")
        if cr:
            first_compound = cr
            break

    if first_compound:
        lines.append("-" * 60)
        lines.append("  COMPOUND GROWTH (first bot with history)")
        lines.append("-" * 60)
        lines.append(first_compound)

    lines.append("")
    lines.append("=" * 60)

    body = "\n".join(lines)
    subject = f"Daily Compound Report — PnL: {total_pnl:+,.2f} ({total_pnl_pct:+.1f}%) — {len(reports)} bots"

    logger.info("Sending compound daily report: {}", subject)
    await _notifier.send(NotificationType.DAILY_SUMMARY, subject, body)


async def _start_services(state: HubState) -> None:
    """Launch monitor, analytics, and daily report as background asyncio tasks."""
    global _monitor, _analytics, _openclaw_advisor, _notifier

    settings = get_settings()

    _monitor = MonitorService(settings=settings, state=state)
    set_monitor_service(_monitor)
    _analytics = AnalyticsService(refresh_interval=300, state=state)
    _openclaw_advisor = OpenClawAdvisorService(settings=settings, state=state)
    set_openclaw_advisor_service(_openclaw_advisor)
    _notifier = Notifier(settings)
    await _notifier.start()

    monitor_task = asyncio.create_task(_monitor.start(), name="monitor")
    analytics_task = asyncio.create_task(_analytics.start(), name="analytics")
    daily_task = asyncio.create_task(_daily_report_loop(), name="daily_report")
    openclaw_advisor_task = asyncio.create_task(_openclaw_advisor.start(), name="openclaw_advisor")
    _background_tasks.extend([monitor_task, analytics_task, daily_task, openclaw_advisor_task])

    logger.info("Hub services started: monitor + analytics + daily report + openclaw advisor")


async def _stop_services() -> None:
    if _monitor:
        await _monitor.stop()
    if _analytics:
        await _analytics.stop()
    if _openclaw_advisor:
        await _openclaw_advisor.stop()
    if _notifier:
        await _notifier.stop()

    for task in _background_tasks:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
    _background_tasks.clear()
    logger.info("Hub services stopped")


@asynccontextmanager
async def lifespan(_app: object) -> AsyncIterator[None]:
    """FastAPI lifespan: start services on boot, stop on shutdown."""
    global _hub_state

    _hub_state = HubState()
    set_hub_state(_hub_state)
    setup_log_capture()

    await _start_services(_hub_state)
    logger.info("Hub is live")

    yield

    await _stop_services()
    logger.info("Hub shutdown complete")


app.router.lifespan_context = lifespan


def main() -> None:
    settings = get_settings()

    logger.remove()
    logger.add(sys.stderr, level=settings.log_level)
    logger.add("logs/hub_{time}.log", rotation="1 day", retention="14 days", level="DEBUG")

    logger.info("=" * 60)
    logger.info("TRADE BORG HUB — Central Brain")
    logger.info("Port: {}", settings.dashboard_port)
    logger.info("Mode: {}", settings.trading_mode)
    logger.info("=" * 60)

    port = settings.dashboard_port

    def _handle_signal(sig_num: int, _frame: object) -> None:
        logger.info("Received signal {}, shutting down hub...", sig_num)
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
