from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from enum import Enum

import aiosmtplib
from loguru import logger

from config.settings import Settings


class NotificationType(str, Enum):
    LIQUIDATION = "liquidation"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    ORDER_FILLED = "order_filled"
    DAILY_SUMMARY = "daily_summary"
    SPIKE_DETECTED = "spike_detected"
    NEWS_ALERT = "news_alert"
    WHALE_POSITION = "whale_position"  # always on: $100K+ position at 20%+ profit


class Notifier:
    """Email notification system with configurable alert types."""

    def __init__(self, settings: Settings):
        self.smtp_host = settings.smtp_host
        self.smtp_port = settings.smtp_port
        self.smtp_user = settings.smtp_user
        self.smtp_password = settings.smtp_password
        self.notify_email = settings.notify_email
        self.enabled_types = set(settings.notification_list)
        self._queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self._running = False
        self._background_tasks: list = []

    ALWAYS_ON = {NotificationType.LIQUIDATION, NotificationType.WHALE_POSITION}

    def is_enabled(self, ntype: NotificationType) -> bool:
        if ntype in self.ALWAYS_ON:
            return True
        return ntype.value in self.enabled_types

    async def start(self) -> None:
        self._running = True
        self._background_tasks.append(asyncio.create_task(self._process_queue()))
        logger.info("Notification system started (enabled: {})", ", ".join(self.enabled_types))

    async def stop(self) -> None:
        self._running = False

    async def send(self, ntype: NotificationType, subject: str, body: str) -> None:
        if not self.is_enabled(ntype):
            return

        if not self.smtp_user or not self.notify_email:
            logger.warning("Email not configured, logging notification instead: {}", subject)
            logger.info("NOTIFICATION [{}]: {} - {}", ntype.value, subject, body)
            return

        await self._queue.put((subject, body))

    async def alert_liquidation(self, symbol: str, pnl: float, balance: float) -> None:
        await self.send(
            NotificationType.LIQUIDATION,
            f"LIQUIDATION ALERT - {symbol}",
            f"Position {symbol} is at risk of liquidation!\n\n"
            f"Unrealized PnL: {pnl:.2f} USDT\n"
            f"Remaining balance: {balance:.2f} USDT\n"
            f"Time: {datetime.now(UTC).isoformat()}",
        )

    async def alert_stop_loss(self, symbol: str, entry: float, exit_price: float, pnl: float) -> None:
        await self.send(
            NotificationType.STOP_LOSS,
            f"Stop Loss Hit - {symbol}",
            f"Stop loss triggered for {symbol}\n\nEntry: {entry:.6f}\nExit: {exit_price:.6f}\nPnL: {pnl:.2f} USDT",
        )

    async def alert_spike(self, symbol: str, change_pct: float, direction: str, price: float) -> None:
        await self.send(
            NotificationType.SPIKE_DETECTED,
            f"Spike Detected - {symbol} {direction.upper()} {abs(change_pct):.1f}%",
            f"Price spike on {symbol}\n\n"
            f"Direction: {direction}\nChange: {change_pct:.2f}%\n"
            f"Current price: {price:.6f}\n"
            f"Time: {datetime.now(UTC).isoformat()}",
        )

    async def alert_news(self, headline: str, symbols: list[str], source: str) -> None:
        await self.send(
            NotificationType.NEWS_ALERT,
            f"News Alert - {', '.join(symbols)}",
            f"Relevant news detected:\n\n{headline}\n\nSource: {source}\nRelated symbols: {', '.join(symbols)}",
        )

    async def alert_whale_position(
        self,
        symbol: str,
        notional: float,
        profit_pct: float,
        profit_usd: float,
        entry_price: float,
        current_price: float,
        leverage: int,
        adds: int,
        dashboard_url: str = "",
    ) -> None:
        """Alert when a position hits $100K+ notional AND 20%+ profit.

        This is always enabled -- the user explicitly asked to be notified
        so they can decide the next course of action on the dashboard.
        """
        await self.send(
            NotificationType.WHALE_POSITION,
            f"WHALE POSITION - {symbol} +{profit_pct:.1f}% (${notional:,.0f})",
            (
                f"WHALE POSITION ALERT\n"
                f"{'=' * 50}\n\n"
                f"  Symbol:          {symbol}\n"
                f"  Notional value:  ${notional:,.0f}\n"
                f"  Profit:          +{profit_pct:.1f}% (${profit_usd:,.2f})\n"
                f"  Entry price:     {entry_price:.6f}\n"
                f"  Current price:   {current_price:.6f}\n"
                f"  Leverage:        {leverage}x\n"
                f"  DCA adds:        {adds}\n"
                f"  Time:            {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
                f"This position has grown to full size and is running well.\n"
                f"You should decide what to do next:\n"
                f"  - Take partial profit\n"
                f"  - Tighten the stop-loss\n"
                f"  - Let it ride with trailing stop\n"
                f"  - Close it entirely\n\n"
                + (f"Dashboard: {dashboard_url}\n\n" if dashboard_url else "")
                + "The bot will NOT auto-close this. It's your call.\n"
            ),
        )

    async def send_daily_summary(
        self,
        balance: float,
        pnl: float,
        pnl_pct: float,
        trades: int,
        open_positions: int,
        compound_report: str,
        target_hit: bool,
    ) -> None:
        status = "TARGET HIT" if target_hit else "TARGET MISSED"
        subject = f"Daily Report - PnL: {pnl:+.2f} ({pnl_pct:+.1f}%) - {status}"

        body = (
            f"DAILY TRADING REPORT\n"
            f"{'=' * 40}\n\n"
            f"  Date:            {datetime.now(UTC).strftime('%Y-%m-%d')}\n"
            f"  Balance:         {balance:,.2f} USDT\n"
            f"  Daily PnL:       {pnl:+,.2f} USDT ({pnl_pct:+.1f}%)\n"
            f"  Trades today:    {trades}\n"
            f"  Open positions:  {open_positions}\n"
            f"  Status:          {status}\n"
            f"\n\n{compound_report}"
        )

        await self.send(NotificationType.DAILY_SUMMARY, subject, body)

    async def _process_queue(self) -> None:
        while self._running:
            try:
                subject, body = await asyncio.wait_for(self._queue.get(), timeout=5.0)
                await self._send_email(subject, body)
            except TimeoutError:
                continue
            except Exception as e:
                logger.error("Failed to send notification: {}", e)

    async def _send_email(self, subject: str, body: str) -> None:
        msg = MIMEMultipart()
        msg["From"] = self.smtp_user
        msg["To"] = self.notify_email
        msg["Subject"] = f"[Trading Bot] {subject}"
        msg.attach(MIMEText(body, "plain"))

        try:
            await aiosmtplib.send(
                msg,
                hostname=self.smtp_host,
                port=self.smtp_port,
                username=self.smtp_user,
                password=self.smtp_password,
                start_tls=True,
            )
            logger.info("Email sent: {}", subject)
        except Exception as e:
            logger.error("Email send failed: {}", e)
