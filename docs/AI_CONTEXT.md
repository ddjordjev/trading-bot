# AI Context (Startup Brief)

Last verified: 2026-03-04  
Owner: platform/docs

Use this file as the default startup context for new chats.

## New Chat Startup Instruction

Read `docs/AI_CONTEXT.md` first.  
Read `docs/ARCHITECTURE.md` only if the task touches architecture or data flow.  
Read `docs/summary.html` only when requirements history or deep human narrative is needed.

## One-Screen System Snapshot

- The hub decides what to trade; bots decide whether to execute a specific proposal.
- Bots are lightweight executors and position managers, not signal generators.
- There is one in-memory queue in the hub; bots pop one proposal at a time.
- The hub filters invalid symbols before queueing (exchange support, dedup, open-trade checks).
- Bots run last-minute validation (candles + ticker) before execution.
- Trade persistence is hub-centric: bots push events to hub, hub writes `hub.db`.
- Bots are stateless and recover by asking hub for open trades, then reconciling with exchange.
- Idle bots are silent: no exchange and no hub communication until activation file appears.
- Hub intelligence includes monitor, scanner, signal generation, analytics, and dashboard APIs.
- Delegated local tasks in bots (ExtremeWatcher, PatternDetector) only use hub-curated inputs.
- Fast queue pickup happens every 5s; full management tick runs every 30-600s.
- Primary exchange symbol intelligence is fetched in hub via CCXT and refreshed periodically.

## Hard Invariants (Do Not Change)

- Bots must not do broad market scanning or independent opportunity discovery.
- Keep exactly one queue: `HubState._trade_queue`.
- No per-bot queues, no shadow queues, no copy-on-read queue behavior.
- Bots must not persist local trade databases; no bot-local DB state.
- Trade ideas must originate from hub `SignalGenerator`, not bot-local strategies.
- All bot coordination must happen through the hub, never bot-to-bot.
- Hub must filter unsupported/untradeable symbols before queue insertion.
- Idle bots remain silent except activation-file polling.
- If architecture does not support a new flow, stop and ask before inventing one.

## Current Run Profile

### Active and Idle Bots

- Active by default: `extreme`, `momentum`, `indicators`, `meanrev`, `swing`
- Idle by default: `hedger`, `scalper`, `fullstack`, `conservative`, `aggressive`
- Idle activation file: `data/{bot_id}/activate`

### Exchanges and Services

- Hub service: `bot-hub` on `:9035`
- Supported exchanges in architecture: Binance (primary), Bybit
- Hub fetches exchange symbols directly (not via bots)

### Cadence and Queue Behavior

```text
Quick hub check: every 5s
Full bot tick: 30-600s (profile-configured)
Proposal lock TTL: 300s
Queue serve model: pop/lock one matching proposal per bot request
```

### Key Risk / Safety Guardrails

```text
Low-balance gate: pause new entries below MIN_TRADEABLE_EQUITY_USDT
Paused mode: continue managing open positions, keep reporting to hub
Resume condition: equity recovers above threshold
```

## API Contract Quick Refs

### `POST /internal/report`

Bot sends status + readiness + open symbols; hub returns enabled state, confirmed write keys, and optional proposal.

```json
{
  "request": {
    "bot_id": "momentum",
    "bot_style": "momentum",
    "exchange": "binance",
    "open_symbols": ["BTC/USDT:USDT"],
    "ready": true
  },
  "response": {
    "enabled": true,
    "confirmed_keys": ["req_123"],
    "proposal": {}
  }
}
```

### `POST /internal/queue-update`

Bot immediately reports consume/reject decision after proposal evaluation.

```json
{
  "request": {
    "bot_id": "momentum",
    "exchange": "binance",
    "proposal_id": "abc",
    "action": "consume_or_reject",
    "reason": "optional"
  },
  "response": { "status": "ok" }
}
```

### `GET /internal/intel`

Hub returns cached intel snapshot and curated shortlist data.

```json
{
  "response": {
    "intel": {},
    "analytics": {},
    "extreme_watchlist": [],
    "intel_age": 12.3
  }
}
```

### Trade Persistence and Recovery

```text
POST /internal/trade                  -> bot sends trade event + request_key
GET  /internal/trades/{bot_id}/open  -> bot restart recovery source
POST /internal/recovery-close         -> close missing-on-exchange records
```

## Critical Paths By File

- `hub_main.py`: Hub entry point and service wiring.
- `hub/state.py`: Single in-memory hub state and queue ownership.
- `services/monitor.py`: Intel refresh and proposal routing into queue.
- `services/signal_generator.py`: Trade proposal generation and scoring.
- `web/server.py`: Dashboard plus internal bot/hub API endpoints.
- `bot.py`: Bot runtime loop, validation, execution, and reporting.
- `config/bot_profiles.py`: Active/idle profiles and style/priority routing metadata.
- `db/store.py`: Hub DB persistence layer and trade history access.
- `shared/models.py`: Queue/proposal/reporting data model contracts.
- `scanner/binance_futures.py`: Binance native scanner and rolling market state.

## Known Sharp Edges

- Do not reintroduce strategy generation in bots; bots only validate/execute hub proposals.
- Do not add bot-local DB writes for convenience; all trade persistence goes to hub.
- Do not add additional queue structures, lock managers, or per-bot queue slices.
- Do not make idle bots call hub endpoints or connect exchanges before activation.
- Do not bypass hub symbol filtering by pushing raw symbols directly from bots.
- Do not treat `summary.html` as canonical architecture truth when conflicts appear.

## Context Drift Change Log

- 2026-03-04: Introduced AI-first startup document and doc role split.
- 2026-03-04: Standardized startup instruction to read AI context first.
- 2026-03-04: Marked `ARCHITECTURE.md` as canonical technical source and `summary.html` as human reference.

## Constants Quick Block

```text
Hub URL in containers: http://bot-hub:9035
Hub internal report cadence: 5s
Proposal lock duration: 300s
Primary persistent DB: $HOST_DATA_DIR/hub.db
Bots local DB policy: forbidden
```
