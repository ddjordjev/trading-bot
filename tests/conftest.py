"""Shared fixtures and configuration for tests."""

from __future__ import annotations

import os

import pytest

# Test-only baseline env so required Settings fields are always present
# even for imports that happen before pytest fixtures run.
os.environ.setdefault("TRADING_MODE", "paper_local")
os.environ.setdefault("EXCHANGE", "bybit")
os.environ.setdefault("SESSION_BUDGET", "100")
os.environ.setdefault("HUB_DB_BACKEND", "postgres")


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch, tmp_path):
    """Ensure each test gets isolated settings without touching real .env files."""
    from config.settings import get_settings

    monkeypatch.setenv("TRADING_MODE", "paper_local")
    monkeypatch.setenv("EXCHANGE", "bybit")
    monkeypatch.setenv("SESSION_BUDGET", "100")
    monkeypatch.setenv("HUB_DB_BACKEND", "postgres")
    monkeypatch.delenv("BYBIT_API_KEY", raising=False)
    monkeypatch.delenv("BYBIT_API_SECRET", raising=False)
    get_settings.cache_clear()
