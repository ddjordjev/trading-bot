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
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import uvicorn
from loguru import logger

from config.bot_profiles import PROFILES_BY_ID
from config.settings import get_settings
from db.hub_repository import make_hub_repository
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
_INSIGHTS_MIN_EVIDENCE_TRADES = 15
_INSIGHTS_MIN_ABS_PNL_USD = 2.0
_SWING_STRATEGIES = {
    "swing_opportunity",
    "grid",
    "major_swing",
    "capitulation_dip_buy",
    "greed_reversal_plan",
    "eth_rotation_play",
}


def _summarize_reported_strategies(strategies: list[dict[str, object]], max_items: int = 8) -> str:
    """Summarize noisy per-symbol strategy rows into compact counts."""
    counts: Counter[str] = Counter()
    for row in strategies:
        name = str(row.get("name", "") or "").strip()
        if name:
            counts[name] += 1
    if not counts:
        return "none"
    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    shown = items[: max(1, max_items)]
    out = ", ".join(f"{name} ({count})" for name, count in shown)
    if len(items) > len(shown):
        out = f"{out}, +{len(items) - len(shown)} more"
    return out


def _strategy_set_jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _is_noise_strategy(strategy: str) -> bool:
    s = strategy.strip().lower()
    return s in {"manual_override", "manual_claim", "risk_manager", "unknown", "stop"}


def _build_bot_similarity_lines(
    bot_ids: list[str],
    config_sets: dict[str, set[str]],
    realized_sets: dict[str, set[str]],
) -> list[str]:
    lines: list[str] = []
    for i, left in enumerate(bot_ids):
        for right in bot_ids[i + 1 :]:
            cfg = _strategy_set_jaccard(config_sets.get(left, set()), config_sets.get(right, set()))
            rz = _strategy_set_jaccard(realized_sets.get(left, set()), realized_sets.get(right, set()))
            if cfg >= 0.80 or rz >= 0.80:
                lines.append(f"{left} ~ {right} (config={cfg:.2f}, realized={rz:.2f})")
    return lines[:8]


def _build_daily_performance_insights(active_bot_ids: set[str], lookback_days: int = 30) -> list[str]:
    """Build compact strategy efficacy + bot-overlap insights from hub DB."""
    db = None

    def _row_value(row: object, key: str, default: float | int = 0) -> float | int:
        if row is None:
            return default
        try:
            return row[key]  # type: ignore[index]
        except Exception:
            return default

    try:
        db = make_hub_repository()
        db.connect()
        conn = db.conn
        if conn is None:
            return ["insights unavailable (db connection missing)"]

        since_iso = (datetime.now(UTC) - timedelta(days=max(1, lookback_days))).replace(microsecond=0).isoformat()

        strategy_rows = conn.execute(
            """
            SELECT strategy,
                   COUNT(*) AS trades,
                   SUM(CASE WHEN is_winner=1 THEN 1 ELSE 0 END) AS winners,
                   COALESCE(SUM(pnl_usd), 0) AS total_pnl
            FROM trades
            WHERE action='close'
              AND recovery_close=0
              AND strategy <> ''
              AND closed_at >= ?
            GROUP BY strategy
            """,
            (since_iso,),
        ).fetchall()

        non_noise = [r for r in strategy_rows if not _is_noise_strategy(str(r["strategy"] or ""))]
        stable = [r for r in non_noise if int(r["trades"] or 0) >= _INSIGHTS_MIN_EVIDENCE_TRADES]
        winners = sorted(stable, key=lambda r: float(r["total_pnl"] or 0.0), reverse=True)
        losers = sorted(stable, key=lambda r: float(r["total_pnl"] or 0.0))

        lines: list[str] = [
            f"lookback={lookback_days}d",
            f"evidence gate: >= {_INSIGHTS_MIN_EVIDENCE_TRADES} trades and |pnl| >= {_INSIGHTS_MIN_ABS_PNL_USD:.2f}",
        ]
        if winners:
            top = [r for r in winners if float(r["total_pnl"] or 0.0) >= _INSIGHTS_MIN_ABS_PNL_USD][:3]
            if not top:
                lines.append("working: no strategy clears positive pnl significance gate")
            else:
                lines.append(
                    "working: "
                    + "; ".join(
                        f"{r['strategy']!s} pnl={float(r['total_pnl'] or 0.0):+.2f} ({int(r['trades'] or 0)} trades)"
                        for r in top
                    )
                )
        else:
            lines.append("working: insufficient non-noise trade sample")

        bad = [r for r in losers if float(r["total_pnl"] or 0.0) <= -_INSIGHTS_MIN_ABS_PNL_USD]
        if bad:
            bottom = bad[:3]
            lines.append(
                "not working: "
                + "; ".join(
                    f"{r['strategy']!s} pnl={float(r['total_pnl'] or 0.0):+.2f} ({int(r['trades'] or 0)} trades)"
                    for r in bottom
                )
            )
        else:
            lines.append("not working: no strategy clears negative pnl significance gate")

        realized_rows = conn.execute(
            """
            SELECT bot_id, strategy
            FROM trades
            WHERE action='close'
              AND recovery_close=0
              AND strategy <> ''
              AND closed_at >= ?
            GROUP BY bot_id, strategy
            """,
            (since_iso,),
        ).fetchall()

        realized_sets: dict[str, set[str]] = {}
        for row in realized_rows:
            bot_id = str(row["bot_id"] or "").strip()
            strategy = str(row["strategy"] or "").strip()
            if not bot_id or not strategy or _is_noise_strategy(strategy):
                continue
            realized_sets.setdefault(bot_id, set()).add(strategy)

        config_sets: dict[str, set[str]] = {}
        active_ids = sorted(active_bot_ids)
        for bot_id in active_ids:
            prof = PROFILES_BY_ID.get(bot_id)
            if not prof:
                continue
            config_sets[bot_id] = {s for s in prof.strategies if s and not _is_noise_strategy(s)}

        overlaps = _build_bot_similarity_lines(active_ids, config_sets, realized_sets)
        if overlaps:
            lines.append("overlap: " + " | ".join(overlaps))
        else:
            lines.append("overlap: no near-duplicate active bots detected")

        swing_placeholders = ",".join("?" for _ in _SWING_STRATEGIES)
        swing_sql = f"""
            SELECT
                COUNT(*) AS trades,
                COALESCE(SUM(pnl_usd), 0) AS total_pnl
            FROM trades
            WHERE action='close'
              AND recovery_close=0
              AND strategy IN ({swing_placeholders})
              AND closed_at >= ?
        """
        swing_row = conn.execute(swing_sql, [*_SWING_STRATEGIES, since_iso]).fetchone()
        swing_trades = int(_row_value(swing_row, "trades", 0) or 0)
        swing_pnl = float(_row_value(swing_row, "total_pnl", 0.0) or 0.0)
        lines.append(f"swing realized: trades={swing_trades}, pnl={swing_pnl:+.2f}")
        if swing_trades < _INSIGHTS_MIN_EVIDENCE_TRADES:
            lines.append("swing coverage: low realized sample; check queue mix and symbol-level dedupe effects")

        swing_mix_row = conn.execute(
            """
            SELECT
                COUNT(*) AS trades,
                COALESCE(SUM(CASE WHEN strategy IN ('manual_override','manual_claim','risk_manager','unknown','stop') THEN 1 ELSE 0 END), 0) AS noise_trades
            FROM trades
            WHERE action='close'
              AND recovery_close=0
              AND bot_id='swing'
              AND closed_at >= %s
            """,
            (since_iso,),
        ).fetchone()
        swing_total = int(_row_value(swing_mix_row, "trades", 0) or 0)
        swing_noise = int(_row_value(swing_mix_row, "noise_trades", 0) or 0)
        if swing_total > 0:
            noise_pct = swing_noise * 100.0 / swing_total
            lines.append(f"swing bot trade mix: noise={noise_pct:.1f}% ({swing_noise}/{swing_total})")
            if noise_pct >= 70.0:
                lines.append("swing bot quality: high noise share; likely few true swing closes in current window")
        return lines
    except Exception as e:
        return [f"insights unavailable: {e!r}"]
    finally:
        if db is not None:
            with contextlib.suppress(Exception):
                db.close()


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
        trades = history[-1].get("trades", 0) if (isinstance(history, list) and len(history) > 0) else 0
        positions = len(rpt.get("positions", []))

        total_balance += bal
        total_pnl += pnl
        total_positions += positions
        total_trades += trades

        strats = _summarize_reported_strategies(rpt.get("strategies", []))
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

    lines.append("-" * 60)
    lines.append("  PERFORMANCE INSIGHTS")
    lines.append("-" * 60)
    for insight in _build_daily_performance_insights({str(r.get("bot_id", "") or "") for r in reports}):
        lines.append(f"  - {insight}")
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
    openclaw_advisor_task = asyncio.create_task(_openclaw_advisor.start(), name="openclaw_advisor")
    _background_tasks.extend([monitor_task, analytics_task, openclaw_advisor_task])

    if _notifier.is_enabled(NotificationType.DAILY_SUMMARY):
        daily_task = asyncio.create_task(_daily_report_loop(), name="daily_report")
        _background_tasks.append(daily_task)
        logger.info("Hub services started: monitor + analytics + daily report + openclaw advisor")
    else:
        logger.info("Hub services started: monitor + analytics + openclaw advisor (daily report disabled)")


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
    logger.add(sys.stderr, level=settings.log_level, diagnose=False)
    logger.add(
        "logs/hub_{time}.log",
        rotation="100 MB",
        retention="7 days",
        level=settings.log_level,
        diagnose=False,
    )

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
