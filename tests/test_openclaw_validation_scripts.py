from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENDPOINT_SCRIPT = ROOT / "scripts" / "validate_openclaw_endpoint.py"
HUB_SCRIPT = ROOT / "scripts" / "validate_openclaw_hub_integration.py"


def _load_script(path: Path) -> dict:
    return runpy.run_path(str(path), run_name="__test__")


def test_validate_openclaw_endpoint_success(monkeypatch, capsys):
    module = _load_script(ENDPOINT_SCRIPT)

    payload = {
        "regime_commentary": {"regime": "risk_on", "confidence": 0.7, "why": ["test"]},
        "idea_briefs": [],
        "alt_data": {
            "long_short_ratio": 1.0,
            "liquidations_24h_usd": 0.0,
            "open_interest_24h_usd": 0.0,
            "sentiment_score": 50,
        },
        "failure_triage": [],
        "experiments": [],
    }

    def fake_build_request(*, url: str, token: str, timeout: float):
        return {"status": 200, "body": json.dumps(payload)}

    module["main"].__globals__["_build_request"] = fake_build_request
    monkeypatch.setattr(sys, "argv", ["validate_openclaw_endpoint.py"])
    code = module["main"]()

    out = capsys.readouterr().out
    assert code == 0
    assert "[OK] OpenClaw payload is valid." in out


def test_validate_openclaw_endpoint_non_200(monkeypatch, capsys):
    module = _load_script(ENDPOINT_SCRIPT)

    def fake_build_request(*, url: str, token: str, timeout: float):
        return {"status": 503, "body": "{}"}

    module["main"].__globals__["_build_request"] = fake_build_request
    monkeypatch.setattr(sys, "argv", ["validate_openclaw_endpoint.py"])
    code = module["main"]()

    out = capsys.readouterr().out
    assert code == 1
    assert "Endpoint returned HTTP 503" in out


def test_validate_openclaw_hub_integration_success(monkeypatch, capsys):
    module = _load_script(HUB_SCRIPT)

    def fake_get_json(url: str, token: str, timeout: float):
        if url.endswith("/api/modules"):
            return 200, [
                {
                    "name": "openclaw",
                    "enabled": True,
                    "stats": {"connected": True, "regime": "risk_on", "confidence": 0.8},
                }
            ]
        return 200, {
            "openclaw_regime": "risk_on",
            "openclaw_regime_confidence": 0.8,
            "openclaw_sentiment_score": 52,
            "openclaw_idea_briefs": [],
            "openclaw_failure_triage": [],
        }

    module["main"].__globals__["_get_json"] = fake_get_json
    monkeypatch.setattr(sys, "argv", ["validate_openclaw_hub_integration.py"])
    code = module["main"]()

    out = capsys.readouterr().out
    assert code == 0
    assert "OpenClaw module and intel surface are visible" in out


def test_validate_openclaw_hub_integration_missing_module(monkeypatch, capsys):
    module = _load_script(HUB_SCRIPT)

    def fake_get_json(url: str, token: str, timeout: float):
        if url.endswith("/api/modules"):
            return 200, [{"name": "intel"}]
        return 200, {}

    module["main"].__globals__["_get_json"] = fake_get_json
    monkeypatch.setattr(sys, "argv", ["validate_openclaw_hub_integration.py"])
    code = module["main"]()

    out = capsys.readouterr().out
    assert code == 1
    assert "OpenClaw module not found" in out
