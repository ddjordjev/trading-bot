"""Tests for config/settings.py."""

from __future__ import annotations

import pytest
from pydantic import ValidationError


class TestSettings:
    @pytest.fixture(autouse=True)
    def required_runtime_env(self, monkeypatch):
        monkeypatch.setenv("TRADING_MODE", "paper_live")
        monkeypatch.setenv("EXCHANGE", "binance_testnet")
        monkeypatch.setenv("SESSION_BUDGET", "100")

    @pytest.fixture()
    def settings(self, monkeypatch):
        monkeypatch.setenv("TRADING_MODE", "paper_local")
        monkeypatch.setenv("EXCHANGE", "bybit_testnet")
        monkeypatch.setenv("BYBIT_API_KEY", "test-key")
        monkeypatch.setenv("BYBIT_API_SECRET", "test-secret")
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
        assert settings.openclaw_configured is True

    def test_openclaw_poll_interval_validation(self, monkeypatch):
        monkeypatch.setenv("OPENCLAW_POLL_INTERVAL", "5")
        from config.settings import Settings

        with pytest.raises(ValidationError):
            Settings()

    def test_openclaw_timeout_validation(self, monkeypatch):
        monkeypatch.setenv("OPENCLAW_TIMEOUT_SECONDS", "1")
        from config.settings import Settings

        with pytest.raises(ValidationError):
            Settings()

    def test_is_live(self, monkeypatch):
        monkeypatch.setenv("TRADING_MODE", "live")
        monkeypatch.setenv("EXCHANGE", "bybit")
        from config.settings import Settings

        s = Settings()
        assert s.is_paper() is False

    def test_startup_guard_accepts_sandbox(self, monkeypatch):
        monkeypatch.setenv("EXCHANGE", "binance_testnet")
        monkeypatch.setenv("BINANCE_API_KEY", "tk")
        monkeypatch.setenv("BINANCE_API_SECRET", "ts")
        from config.settings import Settings

        s = Settings()
        s.validate_startup_mode_guard()

    def test_startup_guard_rejects_live_with_testnet_url(self, monkeypatch):
        monkeypatch.setenv("TRADING_MODE", "live")
        monkeypatch.setenv("EXCHANGE", "bybit")
        monkeypatch.setenv("BYBIT_API_KEY", "pk")
        monkeypatch.setenv("BYBIT_API_SECRET", "ps")
        monkeypatch.setenv("EXCHANGE_PLATFORM_URL", "https://testnet.bybit.com/trade/usdt")
        from config.settings import Settings

        s = Settings()
        with pytest.raises(ValueError, match="Production mode"):
            s.validate_startup_mode_guard()

    def test_startup_guard_rejects_paper_live_with_prod_url(self, monkeypatch):
        monkeypatch.setenv("EXCHANGE", "binance_testnet")
        monkeypatch.setenv("BINANCE_API_KEY", "tk")
        monkeypatch.setenv("BINANCE_API_SECRET", "ts")
        monkeypatch.setenv("EXCHANGE_PLATFORM_URL", "https://www.binance.com/en/futures")
        from config.settings import Settings

        s = Settings()
        with pytest.raises(ValueError, match="Sandbox mode"):
            s.validate_startup_mode_guard()

    def test_paper_live_keeps_live_like_position_risk(self, monkeypatch):
        monkeypatch.setenv("EXCHANGE", "binance_testnet")
        monkeypatch.setenv("BINANCE_API_KEY", "tk")
        monkeypatch.setenv("BINANCE_API_SECRET", "ts")
        monkeypatch.setenv("MAX_POSITION_SIZE_PCT", "5.0")
        monkeypatch.setenv("RISK_ENV_MULTIPLIER", "1.0")
        from config.settings import Settings

        s = Settings()
        assert s.effective_max_position_size_pct == 5.0
