# Architecture Refactoring Plan

> Separate the monolithic codebase into truly independent services.
> **Status:** Planned. Execute after the 10-day run stabilizes.

---

## Problem

Every container (hub, 5 trading bots, monitor, analytics) is built from the
**same Dockerfile** with the **same image** containing all ~125 Python files
and the full Node.js frontend build. The only difference is the entry command
and environment variables.

The hub runs `python bot.py` with `HUB_ONLY=true`, which instantiates a full
`TradingBot` (exchange, risk manager, order manager, notifier, volatility
detector, extreme watcher, etc.) and then just… doesn't register strategies.
It still connects to the exchange, runs the tick loop, and carries all that
dead weight. This is why logs show "hub enabled: false/true" — the hub IS a
trading bot that's been told not to trade.

---

## Target Architecture

```
trading-bot/                     (monorepo)
├── hub/                         Hub + Monitor + Analytics (merged)
│   ├── Dockerfile
│   ├── requirements.txt         fastapi, uvicorn, aiohttp, feedparser, pydantic
│   ├── main.py                  FastAPI app + background monitor/analytics tasks
│   ├── api/                     Dashboard + internal endpoints (from web/server.py)
│   ├── db/                      hub_store.py, models.py
│   ├── intel/                   All intel clients (current intel/)
│   ├── news/                    News monitor
│   ├── scanner/                 Trending scanner
│   ├── analytics/               Analytics engine
│   ├── signal_generator.py      Signal/proposal generation
│   └── profiles.py              Bot profile definitions
│
├── bot/                         Trading bot (lightweight)
│   ├── Dockerfile               No frontend build stage
│   ├── requirements.txt         ccxt, pandas, numpy, ta, aiohttp, pydantic
│   ├── main.py                  Clean entry point
│   ├── trading_loop.py          Tick loop + strategy orchestration
│   ├── hub_client.py            HTTP client for hub communication
│   ├── state_recovery.py        Recover open trades from hub on startup
│   ├── command_server.py        Lightweight aiohttp server (from web/command_server.py)
│   ├── core/
│   │   ├── exchange/            Exchange adapters (binance, bybit, mexc, paper)
│   │   ├── orders/              Order manager, scaler, trailing, hedge, wick
│   │   ├── risk/                Risk manager, daily target, market filter
│   │   ├── patterns/            Pattern detector
│   │   └── extreme/             Extreme mover watcher
│   └── strategies/              All strategy implementations
│
├── shared/                      Shared library (pip-installable or copied)
│   ├── models.py                Pydantic models used by hub and bots
│   ├── state.py                 SharedState reader/writer (JSON IPC)
│   └── settings.py              Base settings class
│
├── docker-compose.yml
└── tests/
```

### Why Merge Monitor + Analytics into Hub

| Factor | Separate | Merged |
|--------|----------|--------|
| Container count | 3 (hub + monitor + analytics) | 1 |
| IPC | File-based JSON (intel_state.json, analytics_state.json) | In-memory |
| RAM | ~300-400MB each (full codebase × 3) | ~400MB total |
| Crash isolation | Independent restarts | One restart takes all three down |
| Complexity | 3 Dockerfiles, 3 requirements, 3 health checks | 1 of each |

The crash isolation trade-off is minimal: the hub is already the single point
of failure for trade persistence and the dashboard. Monitor and analytics are
stable services with no exchange API calls that might error unpredictably.
Docker `restart: unless-stopped` handles the rare crash.

---

## Phases

### Phase 1: Separate Dockerfiles, No Code Changes

**Effort:** 1-2 days

- Create `Dockerfile.bot` without the frontend build stage (no Node.js, no
  `npm install`, no `web/frontend/dist` copy). Saves ~200MB per bot image
  and significant build time.
- Create trimmed `requirements.bot.txt` — no `ruff`, `mypy`, `pytest`,
  `fastapi`, `uvicorn`. Bots only need the command server (`aiohttp`).
- Update `docker-compose.yml`: hub uses current Dockerfile, bots use
  `Dockerfile.bot`.
- Validate: all containers start, bots report to hub, dashboard works.

### Phase 2: Extract Hub as Standalone Service

**Effort:** 1-2 weeks — this is the hard part.

The main challenge: `web/server.py` (1,488 lines) is deeply coupled to
`TradingBot`. Many dashboard endpoints call `_bot.exchange.fetch_positions()`,
`_bot._strategies`, `_bot.orders.execute_signal()` directly. In a separated
hub, these must proxy to bot containers via HTTP.

The proxy pattern partially exists already — bots POST their state to
`/internal/report` and the hub stores it in `_bot_reports`. Bot URLs are
tracked in `_bot_urls`. But ~30-40% of dashboard endpoints still reach into
the local TradingBot object.

**Work:**

1. **Audit every endpoint in `web/server.py`** — classify each as:
   - Hub-native (only needs HubDB / bot_reports) → stays as-is
   - Bot-proxy (needs live bot data) → refactor to HTTP proxy via `_bot_urls`
2. **Create `hub/main.py`** — a FastAPI app that runs WITHOUT `TradingBot`.
   No exchange, no risk manager, no order manager. Just: FastAPI + HubDB +
   bot report aggregation.
3. **Refactor bot-reaching endpoints** to use the existing `_bot_urls`
   registry and forward requests to bot command servers.
4. **Move `/internal/*` endpoints** into hub-specific module.
5. **Validate**: hub starts without exchange credentials, dashboard shows
   aggregated data from bot reports, position actions proxy correctly.

### Phase 3: Merge Monitor + Analytics into Hub

**Effort:** 2-3 days (after Phase 2)

1. Move `MonitorService` and `AnalyticsService` into hub as background
   `asyncio` tasks alongside the FastAPI server.
2. Replace file-based IPC: monitor writes intel directly to an in-memory
   store that the hub API reads. Analytics reads HubDB directly (already
   does this) and writes scores to the same in-memory store.
3. Bots continue reading `intel_state.json` and `analytics_state.json` from
   disk — the hub still writes these files for backward compatibility, but
   the hub itself uses in-memory data.
4. Remove `run_monitor.py`, `run_analytics.py` entry points and their
   Docker service definitions.
5. **Validate**: single hub container serves dashboard, runs intel polling,
   runs analytics refresh. Bots still get intel/analytics via shared files.

### Phase 4: Decompose bot.py

**Effort:** 1 week

The 2,389-line `bot.py` monolith needs splitting:

| New module | Extracted from | Lines (approx) |
|------------|---------------|-----------------|
| `bot/trading_loop.py` | `_run_loop`, `_tick`, `_update_tick_interval` | ~300 |
| `bot/hub_client.py` | `_report_to_hub`, `_push_trade_to_hub`, `_recover_state_from_hub`, `_reconcile_open_trades` | ~200 |
| `bot/trade_executor.py` | `_evaluate_strategy`, `_process_trade_queue`, signal execution | ~400 |
| `bot/state_recovery.py` | Hub recovery, position reconciliation | ~150 |
| `bot/main.py` | `TradingBot.__init__`, `start`, `stop`, `main()` | ~200 |

### Phase 5: Shared Library

**Effort:** 2-3 days

Extract `shared/models.py`, `shared/state.py`, and base settings into a
proper Python package that both hub and bot depend on. Options:

- **Simple copy**: just copy the `shared/` directory into both hub and bot
  Docker images at build time. No package management overhead.
- **Local pip package**: `pip install -e ./shared` in each Dockerfile. Proper
  but adds build complexity.
- **Monorepo with symlinks**: Docker build contexts include shared via
  symlinks or build args.

Recommend starting with simple copy — upgrade to pip package if the shared
surface area grows.

---

## Resulting Docker Compose

```yaml
services:
  hub:
    build:
      context: .
      dockerfile: hub/Dockerfile
    container_name: bot-hub
    ports:
      - "${DASHBOARD_PORT:-9035}:9035"
    volumes:
      - ${HOST_DATA_DIR}:/app/data
      - ${HOST_LOGS_DIR}:/app/logs
    # Runs: FastAPI dashboard + monitor + analytics

  bot-momentum:
    build:
      context: .
      dockerfile: bot/Dockerfile
    environment:
      - BOT_ID=momentum
      - BOT_STRATEGIES=compound_momentum,market_open_volatility
      - HUB_URL=http://hub:9035
    volumes:
      - ${HOST_DATA_DIR}:/app/data
      - ${HOST_LOGS_DIR}:/app/logs
    depends_on:
      hub:
        condition: service_healthy

  bot-indicators:
    # same pattern, different BOT_ID and BOT_STRATEGIES
    ...

  # Loki, Promtail, Prometheus, Grafana unchanged
```

Two container types instead of four. Bot images are ~200MB lighter (no
frontend, no FastAPI, trimmed deps). Hub image is similar size but runs
everything it needs.

---

## What NOT to Change

- **IPC via shared `data/` volume** — keep JSON files for bot ↔ hub
  communication. It works, it's simple, it survives restarts. Kafka/Redis
  is overkill for this scale.
- **Hub HTTP API for trade persistence** — bots push trades to hub via
  `/internal/trade`. This dedup + ack pattern is solid.
- **SQLite for hub.db** — no reason to switch to Postgres for a single-hub
  setup.
- **ccxt for exchange adapters** — it's the right abstraction layer.

---

## Risks

1. **Phase 2 is the bottleneck.** The web/server.py coupling to TradingBot
   is deep. Expect to find endpoints that are hard to proxy (e.g., ones
   that need real-time exchange data). Solution: expand bot command servers
   to expose what the hub needs.
2. **Test refactoring.** `test_web_server.py` (2,039 lines) tests hub and
   bot endpoints together. Needs splitting after Phase 2.
3. **Shared code drift.** If hub and bot have copies of shared models, they
   can drift. Mitigate with CI checks or a shared package.

---

## Decision Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-02-21 | Plan created | Hub runs full TradingBot unnecessarily; bots carry full codebase |
| 2026-02-21 | Merge monitor + analytics into hub | Eliminates file IPC, reduces containers, minimal crash risk increase |
| 2026-02-21 | Defer execution until 10-day run stabilizes | Refactoring during active testing creates unnecessary risk |
