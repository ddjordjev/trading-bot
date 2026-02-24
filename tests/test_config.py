"""Tests for config/settings.py."""

from __future__ import annotations

import pytest


class TestSettings:
    @pytest.fixture()
    def settings(self, monkeypatch):
        monkeypatch.setenv("TRADING_MODE", "paper_local")
        monkeypatch.setenv("EXCHANGE", "mexc")
        monkeypatch.setenv("MEXC_API_KEY", "test-key")
        monkeypatch.setenv("MEXC_API_SECRET", "test-secret")
        monkeypatch.setenv("NOTIFICATIONS_ENABLED", "liquidation,stop_loss,daily_summary")
        monkeypatch.setenv("INTEL_SYMBOLS", "BTC,ETH,SOL")
        monkeypatch.setenv("NEWS_SOURCES", "coindesk,cointelegraph")
        monkeypatch.setenv("TV_INTERVALS", "1h,4h,1D")
        from config.settings import Settings

        return Settings()

    def test_is_paper(self, settings):
        assert settings.is_paper() is True

    def test_notification_list(self, settings):
        assert "liquidation" in settings.notification_list
        assert "stop_loss" in settings.notification_list
        assert len(settings.notification_list) == 3

    def test_intel_symbol_list(self, settings):
        assert settings.intel_symbol_list == ["BTC", "ETH", "SOL"]

    def test_news_source_list(self, settings):
        assert settings.news_source_list == ["coindesk", "cointelegraph"]

    def test_tv_interval_list(self, settings):
        assert settings.tv_interval_list == ["1h", "4h", "1D"]

    def test_defaults(self, settings):
        assert settings.default_leverage == 10
        assert settings.max_daily_loss_pct == 3.0
        assert settings.initial_risk_amount == 50.0
        assert settings.max_notional_position == 100_000.0
        assert settings.openclaw_enabled is True
        assert settings.openclaw_url.endswith("/intel")

    def test_is_live(self, monkeypatch):
        monkeypatch.setenv("TRADING_MODE", "live")
        monkeypatch.setenv("EXCHANGE", "mexc")
        from config.settings import Settings

        s = Settings()
        assert s.is_paper() is False
