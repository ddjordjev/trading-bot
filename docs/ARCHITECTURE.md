# System Architecture

> **Audience**: AI agents. Read this FIRST before touching any code.
> This document describes the current implementation. Do not deviate from it unless instructed to do so.

---

## Golden Rules

1. **Bots are lightweight.** They receive 1 trade proposal per tick, validate
   it, and execute or reject. They manage open positions (SL, TP, trailing,
   DCA, partial takes). That's it. NEVER add intelligence, filtering, data
   fetching, or decision-making logic to bots. All brains live in the hub.

2. **One queue.** There is exactly one in-memory trade queue inside HubState.
   The monitor writes to it. When a bot requests work, it gets 1 proposal
   popped off the top. The rest stay for other bots. No per-bot queues, no
   shared/locked queues, no copying, no broadcasting. Simple FIFO pop.

3. **Hub does all thinking.** Market intelligence, signal generation, symbol
   availability filtering, analytics — all happen inside the hub before
   proposals ever reach the queue. If a symbol isn't tradeable, the hub
   drops it. Bots never see it.

4. **Bots are stateless.** No local database. All trade persistence goes to
   hub.db via HTTP. On restart, bots ask the hub for their open trades and
   reconcile with the exchange.

5. **Don't get creative.** If the architecture doesn't support something,
   ask. Don't invent new data flows, new queues, new state management, or
   new communication patterns.

---

## Container Layout

```
┌─────────────────────────────────────────────────┐
│                    HOST                          │
│                                                  │
│  HOST_DATA_DIR (/workspace/trading-bot-data)     │
│    └── hub.db          ← sole persistent DB      │
│                                                  │
│  HOST_LOGS_DIR (/workspace/trading-bot-logs)     │
│    └── bot_*.log, hub_*.log                      │
└──────────────────────┬──────────────────────────┘
                       │ bind mounts
    ┌──────────────────┼──────────────────────┐
    │                  │                      │
┌───▼───┐  ┌──────────▼────────┐  ┌──────────▼─────┐
│bot-hub│  │ bot-momentum (x5) │  │ monitoring     │
│       │  │ bot-extreme       │  │ loki, promtail │
│ :9035 │  │ bot-indicators    │  │ prometheus     │
│       │  │ bot-meanrev       │  │ grafana :3001  │
│       │  │ bot-swing         │  └────────────────┘
│       │  │ + 5 idle bots     │
└───────┘  └───────────────────┘
```

### bot-hub (1 container)

Entry point: `hub_main.py` → FastAPI on port 9035.

Runs in-process:
- **MonitorService** — polls external APIs (TradingView, CoinMarketCap,
  CoinGecko, Fear&Greed, liquidations, macro, whales), writes IntelSnapshot
  to HubState. Also runs TrendingScanner for hot movers.
- **SignalGenerator** — converts intel + trending data into TradeProposal
  objects with priority (CRITICAL/DAILY/SWING) and strength scores.
  Proposals are filtered for symbol availability BEFORE entering the queue.
- **AnalyticsService** — reads hub.db trade history, computes strategy
  weights/patterns/suggestions, persists to analytics_state.json.
- **Web dashboard** — React frontend at `/`, health at `/health`.
- **Internal API** — `/internal/report` (bot ↔ hub), `/internal/trade`
  (trade persistence), `/internal/trades/{bot_id}/open` (recovery).

State: single `HubState` instance (in-memory). Only analytics_state.json
is persisted to disk. Everything else is ephemeral.

Does NOT: connect to exchanges, place orders, manage positions.

### bot-{name} (10 containers, 5 active + 5 idle)

Entry point: `bot.py` → TradingBot class.

Each bot is configured via environment variables:
- `BOT_ID` — unique identifier (momentum, extreme, indicators, etc.)
- `BOT_STYLE` — queue routing tag (momentum / meanrev / swing)
- `DASHBOARD_HUB_URL` — always `http://bot-hub:9035`
- Risk/leverage/tick overrides per profile

**Active bots** (`is_default=True` in `config/bot_profiles.py`):
extreme, momentum, indicators, meanrev, swing.
They connect to the exchange, register strategies, and trade.

**Idle bots** (`is_default=False`): scalper, fullstack, conservative,
aggressive, hedger. They start in lean idle mode — no exchange connection,
no strategies. They poll the hub every 10s for activation.

---

## Data Flow

### Signal Pipeline (hub-internal)

```
External APIs → MonitorService → IntelSnapshot (HubState)
                                       ↓
TrendingScanner → hot movers ──→ SignalGenerator
                                       ↓
                              TradeProposal objects
                                       ↓
                        _route_to_bots() filters:
                          - symbol on exchange? (drop if not)
                          - consumed/rejected/expired? (skip)
                                       ↓
                              TradeQueue (HubState)
                           single in-memory list
```

### Bot ↔ Hub Communication

Every tick (30-600s depending on bot style):

```
Bot → POST /internal/report
      payload: {bot_id, bot_style, bot_status, exchange_symbols, queue_updates}

Hub → response: {
        enabled,               ← is this bot active?
        confirmed_keys,        ← ack'd trade writes
        intel,                 ← latest IntelSnapshot
        analytics,             ← latest AnalyticsSnapshot
        trade_queue,           ← 1 proposal popped for this bot's style
        extreme_watchlist,     ← extreme mover candidates
        intel_age              ← seconds since last intel update
      }
```

The bot's tick loop then:
1. Checks trailing stops, scale-ins, partial takes, wick scalps
2. Processes the 1 trade proposal (validate → execute or reject)
3. Reports consumed/rejected back to hub next tick

### Trade Persistence

```
Bot executes trade → POST /internal/trade {bot_id, action, trade, request_key}
Hub writes to hub.db (dedup by request_key)
Hub confirms via confirmed_keys in next /internal/report response
Bot removes from pending buffer
```

On bot restart:
```
Bot → GET /internal/trades/{bot_id}/open
Bot reconciles with exchange positions
Missing on exchange → POST /internal/recovery-close (excluded from stats)
```

---

## Trade Queue Rules

- **One queue** in `HubState._trade_queue` (type: `TradeQueue`).
- **Three priority buckets**: critical, daily, swing. Checked in that order.
- **Monitor writes**: `_route_to_bots()` adds proposals after filtering
  for symbol availability on connected exchanges.
- **Bot reads**: `read_queue_for_bot_style(style)` pops exactly 1 matching
  proposal from the top. Matching = `target_bot` tag matches the bot's style,
  or `target_bot` is empty (any style).
- **Pop is destructive** — once a bot gets a proposal, it's gone from the queue.
  Other bots get the next one.
- Bot reports consumed/rejected via `queue_updates` in next report payload.
  Hub records rejections for signal generator cooldowns.

---

## Bot Tick Loop (bot.py `_tick()`)

The tick runs every N seconds (configurable per bot profile). Steps:

1. Fetch balance and positions from exchange
2. Check trailing stops + liquidation risk
3. Scale into positions (PYRAMID DCA / WINNERS adds)
4. Try leverage raises on PYRAMID positions
5. Take partial profit on levered-up positions
6. Check whale position alerts ($100K+ notional)
7. Try wick scalps (counter-trade wicks)
8. Close expired quick trades
9. **Process trade queue** — validate & execute the 1 proposal from hub
10. Read market intelligence from hub
11. Legendary day check
12. Run strategies on tracked symbols (candle analysis)
13. Hedge check
14. Extreme mover evaluation
15. Write deployment status → report to hub

**What the bot does**: manage positions (stops, scales, partials, hedges),
execute pre-validated trade proposals, report status.

**What the bot does NOT do**: scan markets, generate signals, filter symbols,
fetch intelligence, compute analytics, decide WHAT to trade. That's all hub.

---

## Persistence

| What | Where | Survives restart |
|------|-------|-----------------|
| Trade history | hub.db (host bind mount) | Yes |
| Analytics scores | analytics_state.json (host) | Yes |
| Bot status | HubState (memory) | No |
| Intel snapshots | HubState (memory) | No |
| Trade queue | HubState (memory) | No |
| Extreme watchlist | HubState (memory) | No |

**hub.db** is the ONLY persistent database. Lives on host at
`$HOST_DATA_DIR/hub.db`. Never delete. Backups in
`/workspace/trading-bot-backups/`.

Ephemeral JSON files (`bot_status.json`, `trade_queue.json`, etc.) are
created at runtime and should be wiped on rebuild:
```bash
find "$HOST_DATA_DIR" -name "*.json" -o -name "*.lock" | xargs rm -f
```

---

## Trading Modes

| Mode | Exchange | Orders | Use case |
|------|----------|--------|----------|
| `paper_local` | PaperExchange (simulated) | Simulated locally | Pre-launch testing |
| `paper_live` | Binance testnet | Real testnet orders | 10-day validation run |
| `live` | Production exchange | Real money | NEVER without explicit approval |

---

## Bot Profiles (config/bot_profiles.py)

| ID | Style | Active | Strategies |
|----|-------|--------|-----------|
| extreme | momentum | Yes | compound_momentum, market_open_volatility |
| momentum | momentum | Yes | compound_momentum, market_open_volatility |
| indicators | momentum | Yes | rsi, macd |
| meanrev | meanrev | Yes | bollinger, mean_reversion |
| swing | swing | Yes | swing_opportunity, grid |
| scalper | momentum | No | compound_momentum |
| fullstack | momentum | No | all 8 |
| conservative | meanrev | No | rsi, bollinger |
| aggressive | momentum | No | compound_momentum, rsi |
| hedger | momentum | No | compound_momentum, mean_reversion |

`is_default=True` → active on startup. `is_default=False` → lean idle,
activate via dashboard or `/api/bot-profile/{id}/toggle`.

---

## Key Files

| File | Purpose |
|------|---------|
| `hub_main.py` | Hub entry point (FastAPI + services) |
| `bot.py` | Bot entry point (TradingBot class) |
| `hub/state.py` | Single in-memory state (HubState) |
| `web/server.py` | Dashboard + /internal endpoints |
| `services/monitor.py` | Intel polling + signal routing |
| `services/signal_generator.py` | Proposal creation |
| `services/analytics_service.py` | Strategy scoring from trade history |
| `analytics/engine.py` | Analytics computation logic |
| `config/settings.py` | All configuration (env-driven) |
| `config/bot_profiles.py` | Bot profile definitions |
| `core/exchange/paper.py` | PaperExchange (local simulation) |
| `core/exchange/binance.py` | Binance adapter |
| `core/orders/manager.py` | Order management (stops, scales, partials) |
| `core/risk/manager.py` | Risk checks (daily loss, position limits) |
| `core/risk/daily_target.py` | Daily target tier system |
| `db/store.py` | TradeDB (hub.db read/write) |
| `shared/models.py` | Pydantic models (TradeQueue, TradeProposal, etc.) |
| `docker-compose.yml` | Container orchestration |

---

## What NOT To Do

- **Don't add logic to bots.** No market scanning, no symbol filtering, no
  data fetching beyond what's needed for the trade at hand (candle for
  validation, price for execution). Bots are dumb executors.

- **Don't create multiple queues.** One queue. Period. No per-bot queues,
  no shadow queues, no staging areas inside the bot.

- **Don't make bots talk to each other.** All coordination goes through
  the hub. Bots only talk to the hub and the exchange.

- **Don't persist state in bots.** No local DB, no state files. Everything
  goes to hub.db via HTTP.

- **Don't add complexity to the queue.** Pop 1 from top, give to bot, done.
  No locking, no broadcasting, no copy-on-read, no round-robin assignment.

- **Don't lower test coverage thresholds.** Write tests instead.

- **Don't touch hub.db directly.** Use TradeDB methods. Never DROP TABLE,
  DELETE FROM, or raw SQL outside the store module.
