from __future__ import annotations

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
