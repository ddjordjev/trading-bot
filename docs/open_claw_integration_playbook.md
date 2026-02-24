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
