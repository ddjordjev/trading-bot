from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from intel.openclaw import OpenClawClient


class _FakeResponse:
    def __init__(self, status: int, payload: dict):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def json(self):
        return self._payload

    async def read(self):
        return json.dumps(self._payload).encode("utf-8")


class _FakeSession:
    def __init__(self, status: int, payload: dict):
        self._status = status
        self._payload = payload
        self.last_headers: dict[str, str] | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def get(self, _url: str, *, headers: dict[str, str]):
        self.last_headers = headers
        return _FakeResponse(self._status, self._payload)


class _LargeBodyResponse(_FakeResponse):
    async def read(self):
        return b"{" + (b" " * (OpenClawClient._MAX_RESPONSE_BYTES + 10)) + b"}"


class _LargeBodySession(_FakeSession):
    def get(self, _url: str, *, headers: dict[str, str]):
        self.last_headers = headers
        return _LargeBodyResponse(self._status, self._payload)


@pytest.mark.asyncio
async def test_fetch_once_parses_valid_payload_and_sets_latest():
    payload = {
        "as_of": "2026-02-24T00:00:00Z",
        "regime_commentary": {"regime": "risk_on", "confidence": 0.7, "why": ["fear reset"]},
        "alt_data": {
            "long_short_ratio": 0.9,
            "liquidations_24h_usd": 120_000_000,
            "open_interest_24h_usd": 88_000_000_000,
            "sentiment_score": 11,
        },
        "idea_briefs": [{"symbol": "SOL/USDT", "side": "long", "confidence": 0.66}],
    }
    fake_session = _FakeSession(200, payload)

    client = OpenClawClient(enabled=True, base_url="http://127.0.0.1:18080/intel")
    with patch("intel.openclaw.aiohttp.ClientSession", return_value=fake_session):
        result = await client.fetch_once()

    assert result is not None
    assert result.regime_commentary.regime == "risk_on"
    assert result.alt_data.sentiment_score == 11
    assert client.latest is not None
    assert client.latest.idea_briefs[0].symbol == "SOL/USDT"


@pytest.mark.asyncio
async def test_fetch_once_uses_bearer_token_when_configured():
    payload = {"regime_commentary": {"regime": "unknown"}}
    fake_session = _FakeSession(200, payload)
    client = OpenClawClient(enabled=True, base_url="http://127.0.0.1:18080/intel", token="secret")

    with patch("intel.openclaw.aiohttp.ClientSession", return_value=fake_session):
        await client.fetch_once()

    assert fake_session.last_headers is not None
    assert fake_session.last_headers.get("Authorization") == "Bearer secret"


@pytest.mark.asyncio
async def test_fetch_once_returns_none_on_invalid_payload():
    payload = {"alt_data": {"sentiment_score": "not-an-int"}}
    fake_session = _FakeSession(200, payload)
    client = OpenClawClient(enabled=True, base_url="http://127.0.0.1:18080/intel")

    with patch("intel.openclaw.aiohttp.ClientSession", return_value=fake_session):
        result = await client.fetch_once()

    assert result is None
    assert client.latest is None


@pytest.mark.asyncio
async def test_fetch_once_clears_latest_on_non_200_after_success():
    ok_payload = {"regime_commentary": {"regime": "risk_on", "confidence": 0.5}}
    client = OpenClawClient(enabled=True, base_url="http://127.0.0.1:18080/intel")

    with patch("intel.openclaw.aiohttp.ClientSession", return_value=_FakeSession(200, ok_payload)):
        first = await client.fetch_once()
    assert first is not None
    assert client.latest is not None

    with patch("intel.openclaw.aiohttp.ClientSession", return_value=_FakeSession(503, {})):
        second = await client.fetch_once()
    assert second is None
    assert client.latest is None


@pytest.mark.asyncio
async def test_fetch_once_rejects_oversized_payload():
    client = OpenClawClient(enabled=True, base_url="http://127.0.0.1:18080/intel")
    with patch("intel.openclaw.aiohttp.ClientSession", return_value=_LargeBodySession(200, {})):
        result = await client.fetch_once()
    assert result is None
    assert client.latest is None
