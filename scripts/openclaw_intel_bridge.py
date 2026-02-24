#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp
import uvicorn
from fastapi import FastAPI

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from intel.openclaw import OpenClawSnapshot  # noqa: E402

BRIDGE_HOST = os.getenv("OPENCLAW_BRIDGE_HOST", "0.0.0.0")
BRIDGE_PORT = int(os.getenv("OPENCLAW_BRIDGE_PORT", "18080"))
HUB_URL = os.getenv("OPENCLAW_BRIDGE_HUB_URL", "http://localhost:9035")
HUB_TOKEN = os.getenv("OPENCLAW_BRIDGE_HUB_TOKEN", "")
OPENCLAW_TIMEOUT = float(os.getenv("OPENCLAW_BRIDGE_TIMEOUT_SECONDS", "8"))

LOCAL_ENABLED = os.getenv("OPENCLAW_BRIDGE_LOCAL_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
LOCAL_OLLAMA_URL = os.getenv("OPENCLAW_BRIDGE_LOCAL_OLLAMA_URL", "http://127.0.0.1:11434")
LOCAL_MODEL = os.getenv("OPENCLAW_BRIDGE_LOCAL_MODEL", "qwen2.5:7b-instruct")
LOCAL_TIMEOUT = float(os.getenv("OPENCLAW_BRIDGE_LOCAL_TIMEOUT_SECONDS", "20"))
LOCAL_RECENT_EXAMPLES = max(0, int(os.getenv("OPENCLAW_BRIDGE_LOCAL_RECENT_EXAMPLES", "2")))

PAID_ENABLED = os.getenv("OPENCLAW_BRIDGE_PAID_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
PAID_MODEL_HAIKU = os.getenv("OPENCLAW_BRIDGE_PAID_MODEL_HAIKU", "claude-haiku-4-5")
PAID_MODEL_SONNET = os.getenv("OPENCLAW_BRIDGE_PAID_MODEL_SONNET", "claude-sonnet-4-5")
PAID_TIMEOUT = float(os.getenv("OPENCLAW_BRIDGE_PAID_TIMEOUT_SECONDS", "20"))
PAID_MAX_TOKENS = max(128, int(os.getenv("OPENCLAW_BRIDGE_PAID_MAX_TOKENS", "900")))
PAID_TEMPERATURE = float(os.getenv("OPENCLAW_BRIDGE_PAID_TEMPERATURE", "0.2"))

ENABLE_SONNET_ESCALATION = os.getenv("OPENCLAW_BRIDGE_ENABLE_SONNET_ESCALATION", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
ESCALATE_CONFIDENCE_LT = float(os.getenv("OPENCLAW_BRIDGE_ESCALATE_CONFIDENCE_LT", "0.60"))
ESCALATE_ON_HIGH_TRIAGE = os.getenv("OPENCLAW_BRIDGE_ESCALATE_ON_HIGH_TRIAGE", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

DAILY_BUDGET_USD = float(os.getenv("OPENCLAW_BRIDGE_DAILY_BUDGET_USD", "1.67"))  # ≈$50/mo
DAILY_SONNET_CALL_CAP = max(0, int(os.getenv("OPENCLAW_BRIDGE_DAILY_SONNET_CALL_CAP", "0")))  # disabled by default
BUDGET_STATE_PATH = Path(os.getenv("OPENCLAW_BRIDGE_BUDGET_STATE_PATH", "data/openclaw_budget_state.json"))
DISTILL_PATH = Path(os.getenv("OPENCLAW_BRIDGE_DISTILL_PATH", "data/openclaw_distill.jsonl"))

app = FastAPI(title="OpenClaw Intel Bridge")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _today_key() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


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


def _estimate_tokens(text: str) -> int:
    # Simple, stable estimate for budget gating.
    return max(1, len(text) // 4)


def _extract_json(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        loaded = json.loads(raw)
        return loaded if isinstance(loaded, dict) else None
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            with_json = raw[start : end + 1]
            try:
                loaded = json.loads(with_json)
                return loaded if isinstance(loaded, dict) else None
            except json.JSONDecodeError:
                return None
    return None


def _openclaw_call(method: str) -> Any:
    cmd = ["openclaw", "gateway", "call", method, "--json"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
    except Exception:
        return None
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
    async with (
        aiohttp.ClientSession(timeout=timeout) as session,
        session.get(f"{HUB_URL}{path}", headers=headers) as resp,
    ):
        if resp.status != 200:
            return None
        return await resp.json()


def _build_regime(intel: dict[str, Any]) -> dict[str, Any]:
    regime = str(intel.get("regime", "unknown") or "unknown")
    fear = _to_int(intel.get("fear_greed", 50), 50)
    liq = _to_float(intel.get("liquidation_24h", 0.0), 0.0)
    mass_liq = bool(intel.get("mass_liquidation", False))
    confidence = 0.55
    if mass_liq or fear <= 10 or fear >= 90:
        confidence = 0.72
    why = [f"hub regime={regime}", f"fear_greed={fear}", f"liquidations_24h_usd={liq:.0f}"]
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
                "evidence": [f"change_1h={ch_1h:+.2f}%", f"change_24h={ch_24h:+.2f}%", f"volume_24h={vol:.0f}"],
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


def _fallback_advisory(
    hub_intel: dict[str, Any],
    trending: list[dict[str, Any]],
    health: dict[str, Any] | None,
    intel_age: float,
    queue_len: int,
) -> dict[str, Any]:
    fear_greed = _to_int(hub_intel.get("fear_greed", 50), 50)
    liquidation_24h = _to_float(hub_intel.get("liquidation_24h", 0.0), 0.0)
    overleveraged = str(hub_intel.get("overleveraged_side", "") or "")
    long_short_ratio = 1.2 if overleveraged == "longs" else (0.8 if overleveraged == "shorts" else 1.0)
    return {
        "as_of": _now_iso(),
        "regime_commentary": _build_regime(hub_intel),
        "idea_briefs": _build_idea_briefs(trending),
        "alt_data": {
            "long_short_ratio": long_short_ratio,
            "liquidations_24h_usd": liquidation_24h,
            "open_interest_24h_usd": 0.0,
            "sentiment_score": fear_greed,
        },
        "failure_triage": _build_triage(health, intel_age, queue_len),
        "experiments": [
            {
                "name": "tighten momentum entry when fear_greed > 75",
                "safety": "paper_only",
                "expected_effect": "reduce drawdown during greed spikes",
                "rollback_rule": "disable if 7d expectancy drops below baseline",
            }
        ],
    }


def _normalize_advisory(payload: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    merged = dict(fallback)
    merged.update(payload or {})
    merged["as_of"] = str(merged.get("as_of") or _now_iso())
    try:
        parsed = OpenClawSnapshot.model_validate(merged)
        return parsed.model_dump()
    except Exception:
        parsed = OpenClawSnapshot.model_validate(fallback)
        return parsed.model_dump()


def _build_compact_context(
    hub_intel: dict[str, Any], trending: list[dict[str, Any]], queue: list[dict[str, Any]], intel_age: float
) -> dict[str, Any]:
    return {
        "as_of": _now_iso(),
        "intel_age_seconds": round(intel_age, 1),
        "regime": str(hub_intel.get("regime", "unknown") or "unknown"),
        "fear_greed": _to_int(hub_intel.get("fear_greed", 50), 50),
        "liquidation_24h_usd": _to_float(hub_intel.get("liquidation_24h", 0.0), 0.0),
        "mass_liquidation": bool(hub_intel.get("mass_liquidation", False)),
        "whale_bias": str(hub_intel.get("whale_bias", "neutral") or "neutral"),
        "overleveraged_side": str(hub_intel.get("overleveraged_side", "") or ""),
        "preferred_direction": str(hub_intel.get("preferred_direction", "neutral") or "neutral"),
        "trending": [
            {
                "symbol": str(c.get("symbol", "")),
                "source": str(c.get("source", "")),
                "change_1h": _to_float(c.get("change_1h", 0.0), 0.0),
                "change_24h": _to_float(c.get("change_24h", 0.0), 0.0),
                "volume_24h": _to_float(c.get("volume_24h", 0.0), 0.0),
            }
            for c in trending[:8]
        ],
        "queue": [
            {
                "symbol": str(q.get("symbol", "")),
                "side": str(q.get("side", "")),
                "strategy": str(q.get("strategy", "")),
                "strength": _to_float(q.get("strength", 0.0), 0.0),
                "supported_exchanges": list(q.get("supported_exchanges", []) or []),
            }
            for q in queue[:6]
        ],
    }


def _advisory_prompt(context: dict[str, Any], recent_examples: list[dict[str, Any]] | None = None) -> str:
    examples = recent_examples or []
    return (
        "You are OpenClaw Advisory for Trade Borg.\n"
        "Return JSON only. Advisory-only, never execution commands.\n"
        "Schema keys exactly: as_of, regime_commentary, idea_briefs, alt_data, failure_triage, experiments.\n"
        "Limits: max 3 idea_briefs, concise fields, confidence [0,1], sentiment_score int [0,100].\n"
        "If uncertain use neutral/unknown with lower confidence.\n"
        f"Recent paid examples (for style only): {json.dumps(examples, ensure_ascii=True)}\n"
        f"Context: {json.dumps(context, ensure_ascii=True)}"
    )


class BudgetController:
    HAIKU_IN = 1.0 / 1_000_000
    HAIKU_OUT = 5.0 / 1_000_000
    SONNET_IN = 3.0 / 1_000_000
    SONNET_OUT = 15.0 / 1_000_000

    def __init__(self, path: Path, daily_cap_usd: float, sonnet_cap: int) -> None:
        self.path = path
        self.daily_cap_usd = max(0.01, daily_cap_usd)
        self.sonnet_cap = max(0, sonnet_cap)

    def _base_state(self) -> dict[str, Any]:
        return {"date": _today_key(), "usd_spent": 0.0, "sonnet_calls": 0, "requests": 0}

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._base_state()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return self._base_state()
            if str(data.get("date", "")) != _today_key():
                return self._base_state()
            return data
        except Exception:
            return self._base_state()

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def estimate_cost(self, model_tier: str, input_tokens: int, output_tokens: int) -> float:
        if model_tier == "sonnet":
            return (input_tokens * self.SONNET_IN) + (output_tokens * self.SONNET_OUT)
        return (input_tokens * self.HAIKU_IN) + (output_tokens * self.HAIKU_OUT)

    def can_afford(self, model_tier: str, estimated_input: int, estimated_output: int) -> tuple[bool, str]:
        state = self.load()
        if model_tier == "sonnet" and self.sonnet_cap > 0 and int(state.get("sonnet_calls", 0)) >= self.sonnet_cap:
            return False, "sonnet_daily_cap_reached"
        projected = float(state.get("usd_spent", 0.0)) + self.estimate_cost(
            model_tier, estimated_input, estimated_output
        )
        if projected > self.daily_cap_usd:
            return False, "daily_budget_exceeded"
        return True, "ok"

    def record(self, model_tier: str, input_tokens: int, output_tokens: int) -> dict[str, Any]:
        state = self.load()
        state["usd_spent"] = float(state.get("usd_spent", 0.0)) + self.estimate_cost(
            model_tier, input_tokens, output_tokens
        )
        state["requests"] = int(state.get("requests", 0)) + 1
        if model_tier == "sonnet":
            state["sonnet_calls"] = int(state.get("sonnet_calls", 0)) + 1
        self.save(state)
        return state


class LocalAdvisoryClient:
    def __init__(self, enabled: bool, base_url: str, model: str, timeout: float) -> None:
        self.enabled = enabled
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = max(2.0, timeout)

    async def run(self, context: dict[str, Any], examples: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        prompt = _advisory_prompt(context, recent_examples=examples)
        body = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "messages": [{"role": "user", "content": prompt}],
            "options": {"temperature": 0.2},
        }
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        try:
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.post(f"{self.base_url}/api/chat", json=body) as resp,
            ):
                if resp.status != 200:
                    return None
                raw = await resp.json()
        except Exception:
            return None
        text = str((((raw or {}).get("message") or {}).get("content")) or "")
        return _extract_json(text)


class AnthropicAdvisoryClient:
    def __init__(
        self,
        *,
        enabled: bool,
        api_key: str,
        haiku_model: str,
        sonnet_model: str,
        timeout: float,
        max_tokens: int,
        temperature: float,
    ) -> None:
        self.enabled = enabled and bool(api_key)
        self.api_key = api_key
        self.haiku_model = haiku_model
        self.sonnet_model = sonnet_model
        self.timeout = max(2.0, timeout)
        self.max_tokens = max(128, max_tokens)
        self.temperature = max(0.0, min(1.0, temperature))

    async def run(
        self, context: dict[str, Any], local_draft: dict[str, Any] | None, use_sonnet: bool
    ) -> tuple[dict[str, Any] | None, int, int, str]:
        if not self.enabled:
            return None, 0, 0, ""
        model = self.sonnet_model if use_sonnet else self.haiku_model
        prompt = (
            "You are OpenClaw Advisory for Trade Borg. Output JSON only.\n"
            "Advisory-only. Never output execution commands.\n"
            "Keep concise, max 3 idea_briefs, confidence [0,1], sentiment_score int [0,100].\n"
            f"Local draft: {json.dumps(local_draft or {}, ensure_ascii=True)}\n"
            f"Context: {json.dumps(context, ensure_ascii=True)}"
        )
        body = {
            "model": model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        try:
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.post("https://api.anthropic.com/v1/messages", json=body, headers=headers) as resp,
            ):
                if resp.status != 200:
                    return None, 0, 0, model
                raw = await resp.json()
        except Exception:
            return None, 0, 0, model
        content = raw.get("content", []) if isinstance(raw, dict) else []
        text = ""
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text += str(block.get("text", ""))
        usage = raw.get("usage", {}) if isinstance(raw, dict) else {}
        input_tokens = _to_int(usage.get("input_tokens", _estimate_tokens(prompt)), _estimate_tokens(prompt))
        output_tokens = _to_int(usage.get("output_tokens", _estimate_tokens(text)), _estimate_tokens(text))
        return _extract_json(text), input_tokens, output_tokens, model


def _load_recent_distilled_examples(path: Path, limit: int) -> list[dict[str, Any]]:
    if limit <= 0 or not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        out: list[dict[str, Any]] = []
        for line in reversed(lines):
            if len(out) >= limit:
                break
            obj = json.loads(line)
            if isinstance(obj, dict) and "paid_output" in obj:
                paid = obj.get("paid_output", {})
                if isinstance(paid, dict):
                    out.append(
                        {
                            "regime": ((paid.get("regime_commentary") or {}).get("regime", "unknown")),
                            "confidence": ((paid.get("regime_commentary") or {}).get("confidence", 0.0)),
                            "ideas": len(paid.get("idea_briefs", []) or []),
                        }
                    )
        return out
    except Exception:
        return []


def _append_distill(path: Path, record: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=True) + "\n")
    except Exception:
        pass


def _should_escalate(local_payload: dict[str, Any] | None, fallback: dict[str, Any]) -> bool:
    if not ENABLE_SONNET_ESCALATION:
        return False
    src = local_payload or fallback
    regime_conf = _to_float(((src.get("regime_commentary") or {}).get("confidence", 0.0)), 0.0)
    triage = list(src.get("failure_triage", []) or [])
    has_high_triage = any(str((t or {}).get("severity", "")).lower() == "high" for t in triage if isinstance(t, dict))
    if regime_conf < ESCALATE_CONFIDENCE_LT:
        return True
    return bool(ESCALATE_ON_HIGH_TRIAGE and has_high_triage)


_budget = BudgetController(BUDGET_STATE_PATH, DAILY_BUDGET_USD, DAILY_SONNET_CALL_CAP)
_local_client = LocalAdvisoryClient(LOCAL_ENABLED, LOCAL_OLLAMA_URL, LOCAL_MODEL, LOCAL_TIMEOUT)
_anthropic_client = AnthropicAdvisoryClient(
    enabled=PAID_ENABLED,
    api_key=ANTHROPIC_API_KEY,
    haiku_model=PAID_MODEL_HAIKU,
    sonnet_model=PAID_MODEL_SONNET,
    timeout=PAID_TIMEOUT,
    max_tokens=PAID_MAX_TOKENS,
    temperature=PAID_TEMPERATURE,
)


@app.get("/health")
async def health() -> dict[str, Any]:
    oc_health = _openclaw_call("health")
    gateway_ok = bool(oc_health)
    return {
        "status": "ok",
        "bridge": "openclaw_intel",
        "gateway_ok": gateway_ok,
        "local_enabled": LOCAL_ENABLED,
        "paid_enabled": PAID_ENABLED and bool(ANTHROPIC_API_KEY),
        "sonnet_escalation_enabled": ENABLE_SONNET_ESCALATION,
    }


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
    queue_list = queue if isinstance(queue, list) else []
    queue_len = len(queue_list)

    fallback = _fallback_advisory(
        hub_intel=hub_intel,
        trending=trending_list,
        health=hub_health if isinstance(hub_health, dict) else None,
        intel_age=intel_age,
        queue_len=queue_len,
    )

    context = _build_compact_context(hub_intel=hub_intel, trending=trending_list, queue=queue_list, intel_age=intel_age)
    examples = _load_recent_distilled_examples(DISTILL_PATH, LOCAL_RECENT_EXAMPLES)

    local_payload = await _local_client.run(context=context, examples=examples)
    local_norm = _normalize_advisory(local_payload or {}, fallback)

    use_sonnet = _should_escalate(local_payload=local_norm, fallback=fallback)
    paid_norm: dict[str, Any] | None = None
    paid_model_used = ""
    budget_reason = "paid_disabled"

    if _anthropic_client.enabled:
        prompt_for_estimate = _advisory_prompt(context, recent_examples=examples)
        estimate_in = _estimate_tokens(prompt_for_estimate)
        estimate_out = PAID_MAX_TOKENS
        tier = "sonnet" if use_sonnet else "haiku"
        allowed, budget_reason = _budget.can_afford(tier, estimate_in, estimate_out)
        if allowed:
            paid_payload, in_toks, out_toks, paid_model_used = await _anthropic_client.run(
                context=context,
                local_draft=local_norm,
                use_sonnet=use_sonnet,
            )
            if paid_payload:
                paid_norm = _normalize_advisory(paid_payload, local_norm)
                _budget.record("sonnet" if use_sonnet else "haiku", in_toks, out_toks)
                _append_distill(
                    DISTILL_PATH,
                    {
                        "as_of": _now_iso(),
                        "context": context,
                        "local_output": local_norm,
                        "paid_output": paid_norm,
                        "paid_model": paid_model_used,
                    },
                )

    final_payload = paid_norm or local_norm
    budget_state = _budget.load()

    return {
        **final_payload,
        "bridge_meta": {
            "openclaw_status_present": bool(oc_status),
            "openclaw_presence_nodes": len(oc_presence) if isinstance(oc_presence, list) else 0,
            "hub_url": HUB_URL,
            "queue_len": queue_len,
            "intel_age": intel_age,
            "lane_used": "paid" if paid_norm else ("local" if local_payload else "fallback"),
            "paid_model_used": paid_model_used,
            "sonnet_escalation_attempted": use_sonnet,
            "sonnet_escalation_enabled": ENABLE_SONNET_ESCALATION,
            "budget_reason": budget_reason,
            "budget_usd_spent_today": round(_to_float(budget_state.get("usd_spent", 0.0), 0.0), 6),
            "budget_daily_cap_usd": DAILY_BUDGET_USD,
            "sonnet_calls_today": _to_int(budget_state.get("sonnet_calls", 0), 0),
            "sonnet_daily_cap": DAILY_SONNET_CALL_CAP,
            "distill_records_path": str(DISTILL_PATH),
            "local_model": LOCAL_MODEL,
            "paid_model_haiku": PAID_MODEL_HAIKU,
            "paid_model_sonnet": PAID_MODEL_SONNET,
        },
    }


if __name__ == "__main__":
    uvicorn.run(app, host=BRIDGE_HOST, port=BRIDGE_PORT, log_level="info")
