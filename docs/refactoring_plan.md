# Architecture Refactoring — Completed

> Queue-driven architecture: hub is the brain, bots are lightweight executors.
> **Status:** Implemented on `refactoring` branch.

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────────────┐
│                         BOT-HUB (single container)                   │
│                                                                      │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────────┐   │
│  │   Monitor     │  │  Analytics   │  │     FastAPI Dashboard    │   │
│  │  Service      │  │  Service     │  │  /health /api/* /internal│   │
│  │              │  │              │  │                          │   │
│  │  • Intel     │  │  • Strategy  │  │  • Bot report endpoint   │   │
│  │  • News      │  │    weights   │  │  • Queue serving         │   │
│  │  • Trending  │  │  • Patterns  │  │  • Dashboard frontend    │   │
│  └──────┬───────┘  └──────┬───────┘  └──────────┬───────────────┘   │
│         │                 │                      │                   │
│         ▼                 ▼                      ▼                   │
│  ┌──────────────────────────────────────────────────────────────┐    │
│  │                    HubState (in-memory)                       │    │
│  │  intel | analytics | trade_queue | bot_statuses | symbols    │    │
│  └──────────────────────────────────────────────────────────────┘    │
│         │                                                           │
│         ▼                                                           │
│  ┌──────────────────────────┐  ┌──────────────────────────┐        │
│  │    SignalGenerator        │  │    CandleFetcher          │        │
│  │  • Intel-based proposals  │  │  • Centralized candle     │        │
│  │  • Technical analysis     │  │    fetching (ccxt)        │        │
│  │    (RSI, MACD, BB, etc.) │  │  • Reduces API rate hits  │        │
│  └──────────────────────────┘  └──────────────────────────┘        │
└──────────────────────────────────────────────────────────────────────┘
                              │
                    /internal/report
                              │
        ┌─────────────┬───────┼───────┬─────────────┐
        ▼             ▼       ▼       ▼             ▼
  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐
  │bot-moment│  │bot-indic.│  │bot-meanr.│  │bot-swing │  ... (10 total)
  │          │  │          │  │          │  │          │
  │ Queue    │  │ Queue    │  │ Queue    │  │ Queue    │
  │ consumer │  │ consumer │  │ consumer │  │ consumer │
  │          │  │          │  │          │  │          │
  │Validator │  │Validator │  │Validator │  │Validator │
  │(Momentum)│  │(Indicat.)│  │(MeanRev) │  │(Swing)   │
  │          │  │          │  │          │  │          │
  │ Order    │  │ Order    │  │ Order    │  │ Order    │
  │ Manager  │  │ Manager  │  │ Manager  │  │ Manager  │
  └──────────┘  └──────────┘  └──────────┘  └──────────┘
```

---

## What Changed (from old architecture)

### Before (monolithic)
- Each bot independently: fetched candles for ALL symbols, ran ALL strategies,
  generated signals, AND executed trades
- Monitor + Analytics were separate Docker containers using file-based IPC
- Hub was a TradingBot with `hub_only=true` flag
- Per-bot queue files (`trade_queue_{bot_id}.json`)
- ~16 symbols × 8 strategies × 5 bots = 640 API calls per tick cycle

### After (queue-driven)
- **Hub** is a dedicated FastAPI application (`hub_main.py`) — not a TradingBot
- Monitor + Analytics run as asyncio tasks inside the hub process (in-memory IPC)
- Hub runs ALL technical analysis centrally via `SignalGenerator`
- Single shared in-memory trade queue filtered by bot style at read time
- Bots are lightweight executors: consume queue → validate → execute
- Each bot has a per-type **validator** for spot-checking proposals
- Idle bots skip heavy initialization entirely (lean idle loop)
- ~16 symbols fetched once by hub, not 5× by each bot

### Key files changed
| File | Change |
|------|--------|
| `hub_main.py` | New entry point for hub (FastAPI + monitor + analytics) |
| `hub/state.py` | In-memory state replacing file-based SharedState |
| `hub/candle_fetcher.py` | Centralized market data fetcher |
| `bot.py` | Removed strategy execution loop, added queue validation |
| `validators/` | New per-bot-type lightweight validators |
| `services/signal_generator.py` | Added centralized technical analysis |
| `services/monitor.py` | Runs in-process inside hub |
| `docker-compose.yml` | Separate Dockerfiles, removed monitor/analytics services |
| `Dockerfile.hub` | Full image with frontend |
| `Dockerfile.bot` | Trimmed image without frontend/Node.js |

### What stayed the same
- All exchange adapters (`core/exchange/`)
- Order management (`core/orders/`)
- Risk management (`core/risk/`)
- Strategy implementations (`strategies/`)
- Database layer (`db/`)
- Shared models (`shared/models.py`)

---

## Docker Services

| Service | Image | Entry Point | Role |
|---------|-------|-------------|------|
| `bot-hub` | `Dockerfile.hub` | `hub_main.py` | Brain + data presenter + orchestrator |
| `bot-momentum` | `Dockerfile.bot` | `bot.py` | Momentum trade executor |
| `bot-indicators` | `Dockerfile.bot` | `bot.py` | RSI/MACD trade executor |
| `bot-meanrev` | `Dockerfile.bot` | `bot.py` | Mean reversion trade executor |
| `bot-swing` | `Dockerfile.bot` | `bot.py` | Swing/grid trade executor |
| `bot-extreme` | `Dockerfile.bot` | `bot.py` | Extreme mover hunter |
| `bot-scalper` | `Dockerfile.bot` | `bot.py` | Quick scalps (idle by default) |
| `bot-fullstack` | `Dockerfile.bot` | `bot.py` | All-strategies (idle by default) |
| `bot-conservative` | `Dockerfile.bot` | `bot.py` | Low-leverage (idle by default) |
| `bot-aggressive` | `Dockerfile.bot` | `bot.py` | High-leverage (idle by default) |
| `bot-hedger` | `Dockerfile.bot` | `bot.py` | Hedge-focused (idle by default) |

5 bots active by default, 5 idle. Idle bots are truly lightweight — no
exchange connection, no strategy loading, just a health check loop.

---

## Decision Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-02-21 | Plan created | Hub runs full TradingBot unnecessarily |
| 2026-02-21 | Merge monitor + analytics into hub | Eliminates file IPC, reduces containers |
| 2026-02-21 | Queue-driven architecture | Centralize analysis, bots as executors |
| 2026-02-22 | Implemented all 6 phases | Full refactor on `refactoring` branch |
