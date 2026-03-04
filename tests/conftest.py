"""Shared fixtures and configuration for tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch, tmp_path):
    """Ensure each test gets isolated settings without touching real .env files."""
    from config.settings import get_settings

    monkeypatch.setenv("TRADING_MODE", "paper_local")
    monkeypatch.setenv("EXCHANGE", "bybit")
    monkeypatch.setenv("HUB_DB_BACKEND", "sqlite")
    monkeypatch.delenv("BYBIT_API_KEY", raising=False)
    monkeypatch.delenv("BYBIT_API_SECRET", raising=False)
    get_settings.cache_clear()
