#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from typing import Any

import aiohttp
import uvicorn
from fastapi import FastAPI


BRIDGE_HOST = os.getenv("OPENCLAW_BRIDGE_HOST", "0.0.0.0")
BRIDGE_PORT = int(os.getenv("OPENCLAW_BRIDGE_PORT", "18080"))
HUB_URL = os.getenv("OPENCLAW_BRIDGE_HUB_URL", "http://localhost:9035")
HUB_TOKEN = os.getenv("OPENCLAW_BRIDGE_HUB_TOKEN", "")
OPENCLAW_TIMEOUT = float(os.getenv("OPENCLAW_BRIDGE_TIMEOUT_SECONDS", "8"))

app = FastAPI(title="OpenClaw Intel Bridge")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _openclaw_call(method: str) -> Any:
    cmd = ["openclaw", "gateway", "call", method, "--json"]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
    if res.returncode != 0:
        return None
    text = (res.stdout or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


async def _hub_get(path: str) -> Any:
    headers: dict[str, str] = {"Accept": "application/json"}
    if HUB_TOKEN:
        headers["Authorization"] = f"Bearer {HUB_TOKEN}"
    timeout = aiohttp.ClientTimeout(total=OPENCLAW_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(f"{HUB_URL}{path}", headers=headers) as resp:
            if resp.status != 200:
                return None
            return await resp.json()


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _build_regime(intel: dict[str, Any]) -> dict[str, Any]:
    regime = str(intel.get("regime", "unknown") or "unknown")
    fear = _to_int(intel.get("fear_greed", 50), 50)
    liq = _to_float(intel.get("liquidation_24h", 0.0), 0.0)
    mass_liq = bool(intel.get("mass_liquidation", False))
    confidence = 0.55
    if mass_liq or fear <= 10 or fear >= 90:
        confidence = 0.72
    why = [
        f"hub regime={regime}",
        f"fear_greed={fear}",
        f"liquidations_24h_usd={liq:.0f}",
    ]
    return {"regime": regime, "confidence": confidence, "why": why}


def _build_idea_briefs(trending: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ideas: list[dict[str, Any]] = []
    for coin in trending[:5]:
        sym = str(coin.get("symbol", "")).upper()
        ch_1h = _to_float(coin.get("change_1h", 0.0))
        ch_24h = _to_float(coin.get("change_24h", 0.0))
        vol = _to_float(coin.get("volume_24h", 0.0))
        side = "long" if ch_24h >= 0 else "short"
        confidence = min(0.9, 0.45 + min(0.35, (abs(ch_1h) + abs(ch_24h)) / 120.0))
        ideas.append(
            {
                "symbol": sym,
                "side": side,
                "timeframe": "intraday",
                "confidence": round(confidence, 2),
                "thesis": "high-momentum continuation candidate",
                "evidence": [
                    f"change_1h={ch_1h:+.2f}%",
                    f"change_24h={ch_24h:+.2f}%",
                    f"volume_24h={vol:.0f}",
                ],
                "risk_notes": ["advisory only", "hub risk filters still apply"],
            }
        )
    return ideas[:3]


def _build_triage(health: dict[str, Any] | None, intel_age: float, queue_len: int) -> list[dict[str, Any]]:
    triage: list[dict[str, Any]] = []
    if not health or health.get("status") != "ok":
        triage.append(
            {
                "severity": "high",
                "component": "hub",
                "issue": "health endpoint not ok",
                "likely_root_cause": "hub unavailable or startup failure",
                "suggested_checks": ["docker compose ps", "bot-hub logs", "/health response"],
            }
        )
    if intel_age > 240:
        triage.append(
            {
                "severity": "high",
                "component": "intel",
                "issue": f"stale intel_age={intel_age:.0f}s",
                "likely_root_cause": "monitor loop degraded or source errors",
                "suggested_checks": ["monitor logs", "source_timestamps", "exception traces"],
            }
        )
    if queue_len == 0 and intel_age <= 240:
        triage.append(
            {
                "severity": "medium",
                "component": "queue",
                "issue": "queue empty while intel fresh",
                "likely_root_cause": "signal generation too strict or filtered",
                "suggested_checks": ["signal generator logs", "route_to_bots filters", "exchange symbol availability"],
            }
        )
    return triage[:5]


@app.get("/health")
async def health() -> dict[str, Any]:
    oc_health = _openclaw_call("health")
    gateway_ok = bool(oc_health)
    return {"status": "ok", "bridge": "openclaw_intel", "gateway_ok": gateway_ok}


@app.get("/intel")
async def intel() -> dict[str, Any]:
    oc_status = _openclaw_call("status") or {}
    oc_presence = _openclaw_call("system-presence") or []
    hub_health = await _hub_get("/health")
    hub_intel_payload = await _hub_get("/internal/intel") or {}
    hub_intel = hub_intel_payload.get("intel", {}) if isinstance(hub_intel_payload, dict) else {}
    intel_age = (
        _to_float(hub_intel_payload.get("intel_age", 999999.0), 999999.0)
        if isinstance(hub_intel_payload, dict)
        else 999999.0
    )
    trending = await _hub_get("/api/trending")
    queue = await _hub_get("/api/trade-queue")
    trending_list = trending if isinstance(trending, list) else []
    queue_len = len(queue) if isinstance(queue, list) else 0

    fear_greed = _to_int(hub_intel.get("fear_greed", 50), 50)
    liquidation_24h = _to_float(hub_intel.get("liquidation_24h", 0.0), 0.0)
    overleveraged = str(hub_intel.get("overleveraged_side", "") or "")
    long_short_ratio = 1.2 if overleveraged == "longs" else (0.8 if overleveraged == "shorts" else 1.0)

    return {
        "as_of": _now_iso(),
        "regime_commentary": _build_regime(hub_intel),
        "idea_briefs": _build_idea_briefs(trending_list),
        "alt_data": {
            "long_short_ratio": long_short_ratio,
            "liquidations_24h_usd": liquidation_24h,
            "open_interest_24h_usd": 0.0,
            "sentiment_score": fear_greed,
        },
        "failure_triage": _build_triage(hub_health if isinstance(hub_health, dict) else None, intel_age, queue_len),
        "experiments": [
            {
                "name": "tighten momentum entry when fear_greed > 75",
                "safety": "paper_only",
                "expected_effect": "reduce drawdown during greed spikes",
                "rollback_rule": "disable if 7d expectancy drops below baseline",
            }
        ],
        "bridge_meta": {
            "openclaw_status_present": bool(oc_status),
            "openclaw_presence_nodes": len(oc_presence) if isinstance(oc_presence, list) else 0,
            "hub_url": HUB_URL,
            "queue_len": queue_len,
            "intel_age": intel_age,
        },
    }


if __name__ == "__main__":
    uvicorn.run(app, host=BRIDGE_HOST, port=BRIDGE_PORT, log_level="info")
