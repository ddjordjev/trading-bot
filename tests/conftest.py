"""Shared fixtures and configuration for tests."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch, tmp_path):
    """Ensure each test gets isolated settings without touching real .env files."""
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("EXCHANGE", "mexc")
    monkeypatch.delenv("MEXC_API_KEY", raising=False)
    monkeypatch.delenv("MEXC_API_SECRET", raising=False)
