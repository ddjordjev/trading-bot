# OpenClaw Integration Playbook

## Intent

OpenClaw is allowed to communicate externally on its own side (unsandboxed for outside data collection), while this trading system keeps strict internal boundaries.

Inside this project, OpenClaw is an intelligence service only:

- API in: hub reads OpenClaw payloads
- API out: hub exposes advisory data in its own snapshot APIs
- No execution bridge: OpenClaw never gets command execution access inside Trade Borg

## Non-Negotiable Internal Restrictions

1. OpenClaw cannot execute shell commands through hub/bot paths.
2. OpenClaw cannot call bot action endpoints (close, stop, take-profit, tighten-stop).
3. OpenClaw cannot mutate the queue directly.
4. OpenClaw cannot persist trades or touch order execution.
5. OpenClaw payloads are schema-validated and bounded before merge.
6. OpenClaw failures are fail-open; monitor loop must stay alive.

## Recommended Service Mode

Run OpenClaw as a separate service and expose one read-only JSON endpoint.
Hub polls that endpoint and merges advisory fields into `IntelSnapshot`.

## Advisory Payload Contract (Example)

```json
{
  "as_of": "2026-02-24T01:05:00Z",
  "regime_commentary": {
    "regime": "risk_on",
    "confidence": 0.74,
    "why": ["fear reset", "short liquidations increasing"]
  },
  "idea_briefs": [
    {
      "symbol": "SOL/USDT",
      "side": "long",
      "timeframe": "intraday",
      "confidence": 0.68,
      "thesis": "momentum continuation",
      "evidence": ["oi +4.2% 1h", "funding neutral"],
      "risk_notes": ["high beta"]
    }
  ],
  "alt_data": {
    "long_short_ratio": 0.92,
    "liquidations_24h_usd": 323590000,
    "open_interest_24h_usd": 89220000000,
    "sentiment_score": 8
  },
  "failure_triage": [
    {
      "severity": "high",
      "component": "monitor",
      "issue": "snapshot build failing",
      "likely_root_cause": "schema mismatch",
      "suggested_checks": ["monitor traceback", "model fields"]
    }
  ],
  "experiments": [
    {
      "name": "reduce momentum size in caution regime",
      "safety": "paper_only",
      "expected_effect": "lower drawdown",
      "rollback_rule": "disable if hit-rate drops >10% over 50 trades"
    }
  ]
}
```

## Hub Configuration

Use these env vars:

- `OPENCLAW_ENABLED`
- `OPENCLAW_URL`
- `OPENCLAW_TOKEN` (optional)
- `OPENCLAW_POLL_INTERVAL`
- `OPENCLAW_TIMEOUT_SECONDS`

Recommended baseline:

```bash
OPENCLAW_ENABLED=true
OPENCLAW_URL=http://host.docker.internal:18080/intel
OPENCLAW_TOKEN=<optional-shared-secret>
OPENCLAW_POLL_INTERVAL=120
OPENCLAW_TIMEOUT_SECONDS=8
```

## OpenClaw Side Setup (Models + Instructions)

Configure OpenClaw itself to produce a normalized advisory JSON at one endpoint
(`GET /intel` or equivalent). Keep output deterministic and machine-parseable.

### 1) Model profile

- Use one stable primary model for regime/idea generation.
- Optional: use a cheaper fallback model if primary fails.
- Keep temperature low (0.1-0.3) to avoid schema drift.
- Refresh cadence should align with hub polling (typically 60-180s).

### 2) System instruction baseline

OpenClaw prompt should explicitly enforce:

- Return valid JSON only, matching the contract keys/types.
- Advisory only; never output imperative execution commands.
- Include confidence per regime/idea.
- Keep `idea_briefs` and triage entries concise.
- Prefer empty lists over missing keys.

### 3) Auth/key handling

- If endpoint is private, require bearer token and set `OPENCLAW_TOKEN` in hub.
- Rotate token without changing payload schema.
- Never embed exchange API credentials in OpenClaw responses.

## Validation Checklist (Before Enabling in Production Hub)

1. Endpoint returns HTTP 200 + valid JSON.
2. Payload validates against hub schema (no type errors in logs).
3. `openclaw` appears in `sources_active` after first successful pull.
4. `/api/modules` reports OpenClaw connected state.
5. Toggling `/api/module/openclaw/toggle` on/off works and clears cache on disable.

Quick verification commands:

```bash
# Validate endpoint shape quickly
curl -sS -H "Authorization: Bearer $OPENCLAW_TOKEN" "$OPENCLAW_URL" | jq .
```

```bash
# Verify module visibility from hub dashboard API
curl -sS -H "Authorization: Bearer $DASHBOARD_TOKEN" http://localhost:9035/api/modules | jq .
```

```bash
# Validate payload contract against hub OpenClaw schema model
.venv/bin/python scripts/validate_openclaw_endpoint.py --url "$OPENCLAW_URL" --token "$OPENCLAW_TOKEN"
```

## Local Templates

- `/.env.openclaw.example` contains a non-secret baseline for hub + bridge env wiring.
- `scripts/validate_openclaw_endpoint.py` validates endpoint JSON against `OpenClawSnapshot`.
- `scripts/validate_openclaw_hub_integration.py` verifies OpenClaw visibility in `/api/modules` and `/api/intel`.
- `docs/openclaw_operator_guide.md` provides model/profile/instruction guidance (local now, remote later).

## Rollout and Rollback

Rollout:

1. Set `OPENCLAW_*` env vars.
2. Start with conservative polling (`120s`) and monitor logs.
3. Verify data presence in API/module status before relying on signals.

Rollback:

1. Toggle OpenClaw module off (`/api/module/openclaw/toggle`) or set `OPENCLAW_ENABLED=false`.
2. Confirm OpenClaw fields are scrubbed from hub snapshot.
3. Keep hub running (fail-open behavior), no trading interruption.

## Local No-Auth Baseline (current setup)

Use this when OpenClaw is running locally and only reachable on your machine/network:

```bash
OPENCLAW_ENABLED=true
OPENCLAW_URL=http://localhost:18080/intel
OPENCLAW_TOKEN=
```

Validate endpoint and hub wiring:

```bash
.venv/bin/python scripts/validate_openclaw_endpoint.py --url "http://localhost:18080/intel"
.venv/bin/python scripts/validate_openclaw_hub_integration.py --hub-url "http://localhost:9035" --dashboard-token "$DASHBOARD_TOKEN"
```

Or run both checks via one command:

```bash
./scripts/openclaw_preflight.sh
```

## Future Auth Upgrade (bearer token)

When you move beyond local no-auth:

1. Require bearer auth at OpenClaw endpoint.
2. Set `OPENCLAW_TOKEN` on hub to same shared secret.
3. Re-run validation:
   - `validate_openclaw_endpoint.py --token "$OPENCLAW_TOKEN"`
   - `validate_openclaw_hub_integration.py --dashboard-token "$DASHBOARD_TOKEN"`

## Data Flow

1. OpenClaw gathers external intelligence.
2. OpenClaw serves normalized JSON over HTTP.
3. Hub monitor polls and validates JSON.
4. Hub merges advisory fields into `IntelSnapshot`.
5. Signal generation remains hub-owned and bot-safe.

## Security Posture

- Outside communication freedom is on OpenClaw side.
- Internal command/execution rights remain closed.
- Trust boundary stays at OpenClaw HTTP payload parsing.

## Isolated Local Test Stack

To avoid touching your existing running deployment, use the dedicated shadow stack:

```bash
export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
docker compose -f docker-compose.openclaw.dev.yml up -d --build
```

This starts:

- `openclaw-hub` on `http://localhost:9135`
- `openclaw-momentum` linked only to that hub
