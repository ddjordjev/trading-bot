"""Tests for web/auth.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from web.auth import verify_token, verify_ws_token


class TestVerifyToken:
    @pytest.mark.asyncio
    async def test_no_auth_required(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_TOKEN", "")
        with patch("web.auth.get_settings") as mock:
            mock.return_value.dashboard_token = ""
            result = await verify_token(creds=None)
            assert result == "no-auth"

    @pytest.mark.asyncio
    async def test_valid_token(self, monkeypatch):
        with patch("web.auth.get_settings") as mock:
            mock.return_value.dashboard_token = "secret123"
            creds = MagicMock()
            creds.credentials = "secret123"
            result = await verify_token(creds=creds)
            assert result == "secret123"

    @pytest.mark.asyncio
    async def test_invalid_token(self, monkeypatch):
        with patch("web.auth.get_settings") as mock:
            mock.return_value.dashboard_token = "secret123"
            creds = MagicMock()
            creds.credentials = "wrong"
            with pytest.raises(HTTPException) as exc_info:
                await verify_token(creds=creds)
            assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_no_creds_with_token_required(self, monkeypatch):
        with patch("web.auth.get_settings") as mock:
            mock.return_value.dashboard_token = "secret123"
            with pytest.raises(HTTPException):
                await verify_token(creds=None)


class TestVerifyWsToken:
    @pytest.mark.asyncio
    async def test_no_auth_required(self):
        with patch("web.auth.get_settings") as mock:
            mock.return_value.dashboard_token = ""
            ws = MagicMock()
            result = await verify_ws_token(ws)
            assert result is True

    @pytest.mark.asyncio
    async def test_valid_ws_token(self):
        with patch("web.auth.get_settings") as mock:
            mock.return_value.dashboard_token = "secret123"
            ws = MagicMock()
            ws.query_params = {"token": "secret123"}
            result = await verify_ws_token(ws)
            assert result is True

    @pytest.mark.asyncio
    async def test_invalid_ws_token(self):
        with patch("web.auth.get_settings") as mock:
            mock.return_value.dashboard_token = "secret123"
            ws = MagicMock()
            ws.query_params = {"token": "wrong"}
            ws.close = AsyncMock()
            result = await verify_ws_token(ws)
            assert result is False
            ws.close.assert_awaited_once()
