# OpenClaw Operator Guide

## Goal

Configure OpenClaw so it produces stable, advisory-only intel payloads for the hub.

This guide focuses on:

- model profile selection
- instruction template
- safe key handling
- rollout from local no-auth to remote bearer-auth

## Model Profile Recommendations

Use one primary model and optional fallback, both with deterministic settings:

- temperature: `0.1` to `0.3`
- top-p: `0.9` to `1.0`
- refresh cadence: `60-180s`
- output mode: strict JSON only

Suggested profile strategy:

1. Primary model: `anthropic/claude-haiku-4.5` for production advisory.
2. Optional escalation model: `anthropic/claude-sonnet-4.5` (triggered only, disabled by default).
3. Local free lane: Ollama model (for first-pass draft + continuity when paid lane is budget-gated).
4. Same schema contract for every lane.

### Default runtime policy

- Sonnet escalation is configurable but OFF by default.
- Daily budget cap is enforced in bridge runtime.
- If budget is exceeded, bridge fails closed to local/fallback advisory.
- Paid outputs are appended to a distillation JSONL so local lane can reuse patterns as few-shot context.

## OpenClaw Instruction Template

Use this as the system/developer prompt on OpenClaw side:

```text
You are an advisory intelligence module for a crypto trading hub.
You must return JSON only (no markdown, no prose outside JSON).

Rules:
1) Advisory-only: do not output execution commands or imperative trading actions.
2) Respect schema and types exactly.
3) Prefer empty arrays over missing keys.
4) Keep idea_briefs concise and evidence-based.
5) Confidence must be in [0.0, 1.0].
6) sentiment_score must be integer in [0, 100].
7) If uncertain, use neutral/unknown values rather than inventing certainty.

Output schema keys:
- as_of (ISO8601)
- regime_commentary {regime, confidence, why[]}
- idea_briefs[] {symbol, side, timeframe, confidence, thesis, evidence[], risk_notes[]}
- alt_data {long_short_ratio, liquidations_24h_usd, open_interest_24h_usd, sentiment_score}
- failure_triage[] {severity, component, issue, likely_root_cause, suggested_checks[]}
- experiments[] {name, safety, expected_effect, rollback_rule}
```

## Key and Secret Handling

- Keep provider API keys in OpenClaw runtime secrets, not this repository.
- Keep hub/OpenClaw auth secret as `OPENCLAW_TOKEN` on hub when auth is enabled.
- Rotate secrets without changing schema or endpoint path.

## Runtime Modes

### Local now (no auth)

- `OPENCLAW_URL=http://localhost:18080/intel`
- `OPENCLAW_TOKEN=` (empty)

Validation:

```bash
.venv/bin/python scripts/validate_openclaw_endpoint.py --url "http://localhost:18080/intel"
.venv/bin/python scripts/validate_openclaw_hub_integration.py --hub-url "http://localhost:9035" --dashboard-token "$DASHBOARD_TOKEN"
```

### Color-Coded Log Overview

For a readable OpenClaw stream (heartbeat/summaries/errors grouped by event), pipe raw JSON logs through:

```bash
openclaw logs --json --follow | .venv/bin/python scripts/openclaw_logs_overview.py
```

Heartbeat/system-presence noise is hidden by default. Show it when needed:

```bash
openclaw logs --json --follow | .venv/bin/python scripts/openclaw_logs_overview.py --show-heartbeats
```

Useful options:

```bash
# Add periodic rollup counters every 60 lines
openclaw logs --json --follow | .venv/bin/python scripts/openclaw_logs_overview.py --summary-every 60

# Parse an existing log file
.venv/bin/python scripts/openclaw_logs_overview.py /path/to/openclaw.log
```

### Remote later (bearer auth)

- `OPENCLAW_URL=https://<your-openclaw-host>/intel`
- `OPENCLAW_TOKEN=<shared-secret>`

Validation:

```bash
.venv/bin/python scripts/validate_openclaw_endpoint.py --url "$OPENCLAW_URL" --token "$OPENCLAW_TOKEN"
.venv/bin/python scripts/validate_openclaw_hub_integration.py --hub-url "http://localhost:9035" --dashboard-token "$DASHBOARD_TOKEN"
```

## Safety Guardrails (must stay true)

- OpenClaw remains advisory-only.
- Hub owns signal generation and queue decisions.
- No direct queue mutation or bot action calls from OpenClaw.
