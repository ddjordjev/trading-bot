"""Tests for notifications/notifier.py."""

from __future__ import annotations

import pytest

from notifications.notifier import NotificationType, Notifier


@pytest.fixture()
def settings(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper_local")
    monkeypatch.setenv("EXCHANGE", "bybit_testnet")
    monkeypatch.setenv("SEND_DAILY_REPORT", "false")
    monkeypatch.setenv("NOTIFICATIONS_ENABLED", "stop_loss,daily_summary")
    monkeypatch.setenv("SMTP_USER", "")
    monkeypatch.setenv("NOTIFY_EMAIL", "")
    from config.settings import Settings

    return Settings()


@pytest.fixture()
def notifier(settings):
    return Notifier(settings)


class TestNotificationType:
    def test_always_on_types(self):
        assert NotificationType.LIQUIDATION in Notifier.ALWAYS_ON
        assert NotificationType.WHALE_POSITION in Notifier.ALWAYS_ON
        assert NotificationType.EXCHANGE_ACCESS_LOST in Notifier.ALWAYS_ON


class TestNotifier:
    def test_is_enabled_always_on(self, notifier):
        assert notifier.is_enabled(NotificationType.LIQUIDATION) is True
        assert notifier.is_enabled(NotificationType.WHALE_POSITION) is True
        assert notifier.is_enabled(NotificationType.EXCHANGE_ACCESS_LOST) is True

    def test_is_enabled_configured(self, notifier):
        assert notifier.is_enabled(NotificationType.STOP_LOSS) is True
        assert notifier.is_enabled(NotificationType.DAILY_SUMMARY) is False

    def test_daily_summary_enabled_in_live_prod(self, monkeypatch):
        monkeypatch.setenv("TRADING_MODE", "live")
        monkeypatch.setenv("EXCHANGE", "bybit")
        monkeypatch.setenv("SEND_DAILY_REPORT", "true")
        monkeypatch.setenv("NOTIFICATIONS_ENABLED", "daily_summary")
        monkeypatch.setenv("SMTP_USER", "")
        monkeypatch.setenv("NOTIFY_EMAIL", "")
        from config.settings import Settings

        n = Notifier(Settings())
        assert n.is_enabled(NotificationType.DAILY_SUMMARY) is True

    def test_is_disabled(self, notifier):
        assert notifier.is_enabled(NotificationType.NEWS_ALERT) is False
        assert notifier.is_enabled(NotificationType.ORDER_FILLED) is False

    @pytest.mark.asyncio
    async def test_send_disabled_type_noops(self, notifier):
        await notifier.send(NotificationType.NEWS_ALERT, "test", "body")
        assert notifier._queue.empty()

    @pytest.mark.asyncio
    async def test_send_logs_when_no_smtp(self, notifier):
        await notifier.send(NotificationType.LIQUIDATION, "test", "body")
        assert notifier._queue.empty()

    @pytest.mark.asyncio
    async def test_send_queues_when_smtp_configured(self, monkeypatch):
        monkeypatch.setenv("TRADING_MODE", "paper_local")
        monkeypatch.setenv("EXCHANGE", "bybit")
        monkeypatch.setenv("SMTP_USER", "user@test.com")
        monkeypatch.setenv("NOTIFY_EMAIL", "admin@test.com")
        monkeypatch.setenv("NOTIFICATIONS_ENABLED", "liquidation")
        from config.settings import Settings

        s = Settings()
        n = Notifier(s)
        await n.send(NotificationType.LIQUIDATION, "alert", "body")
        assert not n._queue.empty()

    @pytest.mark.asyncio
    async def test_alert_liquidation(self, notifier):
        await notifier.alert_liquidation("BTC/USDT", -500, 200)

    @pytest.mark.asyncio
    async def test_alert_stop_loss(self, notifier):
        await notifier.alert_stop_loss("BTC/USDT", 100, 95, -50)

    @pytest.mark.asyncio
    async def test_alert_spike(self, notifier):
        await notifier.alert_spike("BTC/USDT", 5.0, "up", 110.0)

    @pytest.mark.asyncio
    async def test_alert_news(self, notifier):
        await notifier.alert_news("BTC surges!", ["BTC/USDT"], "coindesk")

    @pytest.mark.asyncio
    async def test_alert_whale_position(self, notifier):
        await notifier.alert_whale_position(
            symbol="BTC/USDT",
            notional=150000,
            profit_pct=25.0,
            profit_usd=30000,
            entry_price=50000,
            current_price=62500,
            leverage=10,
            adds=5,
            dashboard_url="http://localhost:9035",
        )

    @pytest.mark.asyncio
    async def test_alert_whale_position_no_dashboard_url(self, notifier):
        await notifier.alert_whale_position(
            symbol="BTC/USDT",
            notional=150000,
            profit_pct=25.0,
            profit_usd=30000,
            entry_price=50000,
            current_price=62500,
            leverage=10,
            adds=5,
        )

    @pytest.mark.asyncio
    async def test_send_daily_summary(self, notifier):
        await notifier.send_daily_summary(
            balance=10500,
            pnl=500,
            pnl_pct=5.0,
            trades=12,
            open_positions=2,
            compound_report="Day 1: +5%",
            target_hit=True,
        )

    @pytest.mark.asyncio
    async def test_start_and_stop(self, notifier):
        await notifier.start()
        assert notifier._running is True
        await notifier.stop()
        assert notifier._running is False
