from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.openclaw_advisor_service import OpenClawAdvisorService


def _settings() -> MagicMock:
    s = MagicMock()
    s.openclaw_daily_review_enabled = True
    s.openclaw_enabled = True
    s.openclaw_configured = True
    s.openclaw_daily_review_interval_hours = 24
    s.openclaw_daily_review_force_paid = True
    s.openclaw_url = "http://openclaw-bridge:18080/intel"
    s.openclaw_token = ""
    s.openclaw_timeout_seconds = 8
    return s


@pytest.mark.asyncio
async def test_run_if_due_when_no_previous_report_triggers():
    svc = OpenClawAdvisorService(settings=_settings(), state=MagicMock())
    svc.db = MagicMock()
    svc.db.get_latest_openclaw_report_completed_at.return_value = ""
    svc._run_once = AsyncMock(return_value={"ok": True})
    await svc._run_if_due("startup")
    svc._run_once.assert_awaited_once_with(run_kind="startup")


@pytest.mark.asyncio
async def test_run_if_due_skips_when_report_recent():
    svc = OpenClawAdvisorService(settings=_settings(), state=MagicMock())
    svc.db = MagicMock()
    recent = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    svc.db.get_latest_openclaw_report_completed_at.return_value = recent
    svc._run_once = AsyncMock(return_value={"ok": True})
    await svc._run_if_due("scheduled")
    svc._run_once.assert_not_called()


def test_daily_review_url_uses_dedicated_endpoint():
    svc = OpenClawAdvisorService(settings=_settings(), state=MagicMock())
    assert svc._daily_review_url().endswith("/daily-review")


def test_resolve_lane_used_marks_paid_when_model_present():
    payload = {"meta": {"lane_used": "fallback", "paid_model_used": "claude-haiku-4-5"}}
    assert OpenClawAdvisorService._resolve_lane_used(payload) == "paid"


class _FakeResponse:
    def __init__(self, status: int, payload: dict | None = None, text: str = ""):
        self.status = status
        self._payload = payload or {}
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def json(self):
        return self._payload

    async def text(self):
        return self._text


class _FakeSession:
    def __init__(self, response: _FakeResponse):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def post(self, _url: str, *, headers: dict[str, str], json: dict):
        return self._response


@pytest.mark.asyncio
async def test_run_once_stores_no_credits_polite_response_and_does_not_fail():
    svc = OpenClawAdvisorService(settings=_settings(), state=MagicMock())
    svc.db = MagicMock()
    svc.state.read_analytics.return_value = MagicMock(weights=[], patterns=[], suggestions=[])
    svc.db.get_latest_openclaw_daily_report.return_value = {}
    svc.db.get_openclaw_daily_trade_rollup.return_value = []
    svc.db.get_openclaw_strategy_rollup.return_value = []
    svc.db.get_openclaw_symbol_rollup.return_value = []
    svc.db.get_openclaw_suggestion_context.return_value = []
    svc.db.insert_openclaw_daily_report.return_value = 42

    fake_session = _FakeSession(_FakeResponse(429, text="credits exhausted"))
    with patch("services.openclaw_advisor_service.aiohttp.ClientSession", return_value=fake_session):
        result = await svc._run_once(run_kind="manual")

    assert result["ok"] is True
    assert result["status"] == "ok"
    assert result["report_id"] == 42

    _, kwargs = svc.db.insert_openclaw_daily_report.call_args
    assert kwargs["status"] == "ok"
    assert kwargs["response_payload"]["summary"] == "no more credits"
    assert kwargs["response_payload"]["suggestions"] == []
