from __future__ import annotations

import importlib.util
import time
from pathlib import Path

import pytest


def _load_bridge_module():
    root = Path(__file__).resolve().parents[1]
    bridge_path = root / "scripts" / "openclaw_intel_bridge.py"
    spec = importlib.util.spec_from_file_location("openclaw_intel_bridge", bridge_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_should_call_paid_skips_when_local_output_is_strong(monkeypatch):
    bridge = _load_bridge_module()
    monkeypatch.setattr(bridge, "ESCALATE_CONFIDENCE_LT", 0.60)
    monkeypatch.setattr(bridge, "ESCALATE_ON_HIGH_TRIAGE", True)

    local_payload = {
        "regime_commentary": {"confidence": 0.82},
        "failure_triage": [{"severity": "low"}],
    }
    should_call_paid, reason = bridge._should_call_paid(local_payload=local_payload, fallback={})
    assert should_call_paid is False
    assert reason == "local_ok_skip_paid"


def test_should_call_paid_on_low_confidence(monkeypatch):
    bridge = _load_bridge_module()
    monkeypatch.setattr(bridge, "ESCALATE_CONFIDENCE_LT", 0.60)
    monkeypatch.setattr(bridge, "ESCALATE_ON_HIGH_TRIAGE", True)

    local_payload = {
        "regime_commentary": {"confidence": 0.42},
        "failure_triage": [],
    }
    should_call_paid, reason = bridge._should_call_paid(local_payload=local_payload, fallback={})
    assert should_call_paid is True
    assert reason == "local_low_confidence"


def test_should_call_paid_on_high_triage(monkeypatch):
    bridge = _load_bridge_module()
    monkeypatch.setattr(bridge, "ESCALATE_CONFIDENCE_LT", 0.60)
    monkeypatch.setattr(bridge, "ESCALATE_ON_HIGH_TRIAGE", True)

    local_payload = {
        "regime_commentary": {"confidence": 0.9},
        "failure_triage": [{"severity": "high", "component": "intel", "issue": "stale feed"}],
    }
    should_call_paid, reason = bridge._should_call_paid(local_payload=local_payload, fallback={})
    assert should_call_paid is True
    assert reason == "local_high_triage"


def test_budget_controller_enforces_paid_cooldown(tmp_path):
    bridge = _load_bridge_module()
    budget_path = tmp_path / "openclaw_budget_state.json"
    ctl = bridge.BudgetController(
        path=budget_path,
        daily_cap_usd=10.0,
        sonnet_cap=5,
        min_paid_interval_seconds=600,
    )

    ok_before, reason_before = ctl.can_afford("haiku", 1000, 1000)
    assert ok_before is True
    assert reason_before == "ok"

    ctl.record("haiku", 1000, 1000)

    ok_after, reason_after = ctl.can_afford("haiku", 1000, 1000)
    assert ok_after is False
    assert reason_after == "paid_cooldown_active"

    ok_bypass, reason_bypass = ctl.can_afford("haiku", 1000, 1000, bypass_cooldown=True)
    assert ok_bypass is True
    assert reason_bypass == "ok"


@pytest.mark.asyncio
async def test_run_with_deadline_succeeds_when_budget_available():
    bridge = _load_bridge_module()

    async def _fast() -> str:
        return "ok"

    out = await bridge._run_with_deadline(_fast(), time.monotonic() + 1.0)
    assert out == "ok"


@pytest.mark.asyncio
async def test_run_with_deadline_raises_when_budget_exhausted():
    bridge = _load_bridge_module()

    async def _slow() -> str:
        await bridge.asyncio.sleep(0.05)
        return "slow"

    with pytest.raises(TimeoutError):
        await bridge._run_with_deadline(_slow(), time.monotonic() + 0.01)
