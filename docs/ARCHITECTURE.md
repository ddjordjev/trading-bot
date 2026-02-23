# System Architecture

> **Audience**: AI agents. Read this FIRST before touching any code.
> This document describes the current implementation. Do not deviate from it.

---

## Golden Rules

1. **Bots are lightweight executors.** They receive 1 trade proposal from
   the hub queue, validate it (last-minute candle + ticker check), and
   execute or reject. They manage open positions (SL, TP, trailing, DCA,
   partial takes). They run a small number of hub-delegated local tasks
   (ExtremeWatcher, PatternDetector) that operate ONLY on data the hub
   already selected — never on the full market. NEVER add broad market
   scanning, symbol filtering, or independent decision-making to bots.
   All intelligence lives in the hub.

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
  Exchange symbols are fetched directly by the monitor via CCXT (Binance,
  MEXC, Bybit) on startup and refreshed every 5 minutes. Cached in hub.db
  for instant availability on restart.
- **AnalyticsService** — reads hub.db trade history, computes strategy
  weights/patterns/suggestions, persists to analytics_state.json (could
  migrate to a hub.db table in the future — currently JSON for simplicity
  with the nested structure).
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
- `HUB_URL` — always `http://bot-hub:9035`
- Risk/leverage/tick overrides per profile

**Active bots** (`is_default=True` in `config/bot_profiles.py`):
extreme, hedger, momentum, indicators, meanrev, swing.
They connect to the exchange and trade proposals received from the hub
queue. Bots do NOT register local strategies — all trade ideas originate
from the hub's SignalGenerator. The bot validates and executes.

**Idle bots** (`is_default=False`): scalper, fullstack, conservative,
aggressive. They start in lean idle mode — no exchange connection,
no hub communication. They check a local activation file
(`data/{bot_id}/activate`) every 10s. The file is written by the hub's
toggle endpoint when someone enables the bot via the dashboard. That's
the only thing idle bots do — watch for that file. Nothing else.

---

## Data Flow

### Signal Pipeline (hub-internal)

```
Monitor startup → CCXT load_markets() for all exchanges
                  (seeded from hub.db cache, then live fetch)
                              ↓
                  exchange_symbols → SignalGenerator
                              ↓
External APIs → MonitorService → IntelSnapshot (HubState)
                                       ↓
TrendingScanner → hot movers ──→ SignalGenerator
                                       ↓
                              TradeProposal objects
                              (tagged with supported_exchanges)
                                       ↓
                        _route_to_bots() filters:
                          - symbol on exchange? (drop if not)
                          - symbol already in queue? (dedup)
                          - symbol in hub.db open trades? (dedup)
                          - symbol held by a bot on that exchange? (remove exchange)
                                       ↓
Extreme watchlist → top 5 injected as CRITICAL proposals
                              ↓
                        TradeQueue (HubState)
                     single flat in-memory list
```

### Bot ↔ Hub Communication

Three endpoints, clean separation:

**POST /internal/report** — status + queue proposal (every 5s)
```
Bot → {bot_id, bot_style, exchange, open_symbols, ready, bot_status?, positions?}
Hub → {enabled, confirmed_keys, proposal?}
```
The bot sends its current open symbols (from local `_open_trades`) every 5
seconds so the hub always knows what each bot holds (max 5s lag). The `ready`
flag is false during warmup (1 min) — hub returns no proposal until the bot
is ready. When ready, hub picks the best matching proposal (filtered by bot
style, exchange, priority, dedup against hub.db and active symbols), locks
it for 60s, and returns it. Full ticks also include `bot_status` and
`positions` for dashboard reporting.

**POST /internal/queue-update** — immediate consume/reject
```
Bot → {bot_id, exchange, proposal_id, action, reason?}
Hub → {status: "ok"}
```
Bot reports consumed/rejected immediately after evaluating a proposal.
Hub removes the bot's exchange from the proposal's `supported_exchanges`.
If no exchanges remain, the proposal is deleted from the queue. If other
exchanges remain, the proposal stays available for bots on those exchanges.
Rejections are recorded for signal generator cooldowns.

**GET /internal/intel** — cached intel snapshot (once per full tick)
```
Hub → {intel, analytics, extreme_watchlist, intel_age}
```
Returns the full cached snapshot as-is. No bot-specific filtering — the bot
decides what applies to it. Used for position management (reversal risk,
aggression modifiers, exposure adjustments), not for finding trades.

The `extreme_watchlist` is a small curated list of candidates the hub
pre-selected as extreme movers. Bots with `EXTREME_ENABLED=true` subscribe
to WebSocket tickers for ONLY these candidates — they never scan the full
market themselves. See "Delegated Local Tasks" below.

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

- **One flat queue** in `HubState._trade_queue` (type: `TradeQueue`).
  Single `proposals` list — each proposal has a `priority` field
  (CRITICAL / DAILY / SWING). Served in priority order, then by age.
- **Lifecycle**: proposals only have `locked_until`. No consumed/rejected
  fields. A proposal is either available, locked (being evaluated), or
  removed from the queue.
- **Monitor writes**: `_route_to_bots()` adds proposals after filtering
  for symbol availability, queue dedup, hub.db open trade dedup, and
  active symbols on each exchange.
- **Hub serves**: `serve_proposal_to_bot()` picks 1 matching proposal,
  locks it for 300s, returns a copy. Matching criteria:
  - Bot's exchange in `supported_exchanges`
  - Priority in bot's `allowed_priorities` (from BotProfile)
  - `target_bot` matches bot style (or empty = any)
  - Symbol not in `active_symbols` (held by any bot on same exchange)
  - Symbol not in hub.db open trades
- **On consume**: proposal is deleted from the queue. The trade is now
  open — symbol protection shifts to `open_db_symbols` (hub.db) and
  `active_symbols` (bot reports `open_symbols` every 5s).
- **On reject**: bot's exchange is removed from the proposal. Proposal
  stays for other exchanges. Removed entirely when no exchanges left.
  Bot reports immediately via POST `/internal/queue-update`.
- **Lock expiry**: if bot doesn't report within 300s, lock expires and
  proposal becomes available again.

### Deduplication Layers (4 levels)

| Layer | Where | What |
|-------|-------|------|
| Queue dedup | Hub, `_route_to_bots` | Symbol already in queue? |
| Hub DB dedup | Hub, routing + serving | Open trade in hub.db (`closed_at=''`)? |
| Active symbols | Hub, serving | Any bot on this exchange holding it? |
| Exchange check | Bot, before execution | `fetch_positions()` — asks exchange directly |

### Bot Style Filtering

Each `BotProfile` has `allowed_priorities`:

| Bot | Priorities |
|-----|-----------|
| extreme, scalper | `["critical"]` |
| momentum, hedger, indicators, aggressive | `["critical", "daily"]` |
| meanrev, conservative | `["daily"]` |
| swing | `["daily", "swing"]` |
| fullstack | `["critical", "daily", "swing"]` |

Hub only serves proposals whose priority matches. A swing bot never sees
CRITICAL. An extreme bot never sees DAILY/SWING.

---

## Bot Tick Loop (bot.py)

Two cadences run in the main loop:

### Quick hub check (every 5s)
Between full ticks, a lightweight hub poll sends `open_symbols` + `ready`
flag and receives the next proposal. During warmup (first 1 min), `ready`
is false and no proposal is requested or processed. After warmup, the bot
evaluates the proposal and reports consumed/rejected immediately.

### Full tick (30-600s, configurable per bot profile)
1. **Fetch intel** from hub (`GET /internal/intel` — separate call)
2. Fetch balance and positions from exchange
3. Check trailing stops + liquidation risk
4. Scale into positions (PYRAMID DCA / WINNERS adds)
5. Try leverage raises on PYRAMID positions
6. Take partial profit on levered-up positions
7. Check whale position alerts ($100K+ notional)
8. Try wick scalps (counter-trade wicks)
9. Close expired quick trades
10. **Process trade queue** — validate & execute the 1 proposal from hub
11. Legendary day check (uses intel for reversal risk)
12. Fetch candles for held positions (hedge + volatility checks)
13. Hedge check
14. Extreme mover evaluation (hub-curated shortlist only — see below)
15. Write deployment status → full report to hub (`POST /internal/report`)

### Proposal validation
Before executing any proposal, the bot runs a **last-minute validation**
(`_validate_proposal`): fetches recent candles and ticker for the symbol,
then runs its style-specific validator to confirm conditions still match.
If they don't, the proposal is rejected. This is the bot's gate — it
decides WHETHER to trade a given proposal, not WHAT to trade.

### Delegated Local Tasks

Some lightweight tasks run inside the bot but operate ONLY on data the
hub already curated. They do not scan the market independently:

- **ExtremeWatcher** — subscribes to WebSocket tickers for a small list
  of extreme mover candidates received from the hub via `/internal/intel`.
  The hub selects these candidates (typically 5-15 symbols). The top 5
  are also injected into the trade queue as CRITICAL proposals with
  `target_bot="extreme"`. New entries come through the queue (validated
  and deduped like any other proposal). ExtremeWatcher's local role is
  **position monitoring** — tight stops, fast exits on open positions.
  It MUST NOT fetch the full exchange symbol list or scan broadly.

- **PatternDetector** — runs chart pattern analysis on candles fetched
  for a specific proposal that already arrived from the hub queue. It
  enriches the signal with smarter SL/TP levels. It does NOT scan for
  new trading opportunities.

### What the bot does
- Manage positions (stops, scales, partials, hedges, wick scalps)
- Validate proposals against its own style conditions before executing
- Decide WHETHER to accept or reject a proposal (capacity, risk, validation)
- Use intel from hub for position management (reversal risk, aggression)
- Monitor open positions from ExtremeWatcher shortlist (fast exits/stops)
- Enrich proposals with PatternDetector analysis
- Report consumed/rejected back to hub

### What the bot does NOT do
- Scan markets or generate signals (hub does this)
- Filter symbols for availability (hub does this before queuing)
- Compute analytics or strategy scores (hub does this)
- Decide WHAT to trade (hub decides, bot only decides whether to execute)
- Fetch full exchange symbol lists for analysis (only the hub does this)

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

**analytics_state.json** persists strategy weights, patterns, and
suggestions so they survive hub restarts. It's a JSON file because
the structure is deeply nested (Pydantic model). Could be moved to a
hub.db table in the future.

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

Each bot has a `style` that determines which queue proposals it receives.
The `strategies` field in BotProfile is metadata for display/documentation
purposes — bots do NOT register local strategies. All trade proposals
originate from the hub's SignalGenerator.

| ID | Style | Active | Description |
|----|-------|--------|-------------|
| extreme | momentum | Yes | High-leverage extreme mover hunter |
| momentum | momentum | Yes | Trend-following with compounding |
| indicators | momentum | Yes | Classic RSI + MACD signals |
| meanrev | meanrev | Yes | Bollinger + mean reversion |
| swing | swing | Yes | Multi-day swings + grid trading |
| scalper | momentum | No | Quick scalps, tight stops |
| fullstack | momentum | No | All-strategy coverage |
| conservative | meanrev | No | Low leverage, tight risk |
| aggressive | momentum | No | High leverage, big upside |
| hedger | momentum | No | Momentum with aggressive hedging |

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
| `core/extreme/watcher.py` | ExtremeWatcher (hub-curated shortlist) |
| `core/orders/manager.py` | Order management (stops, scales, partials) |
| `core/risk/manager.py` | Risk checks (daily loss, position limits) |
| `core/risk/daily_target.py` | Daily target tier system |
| `db/store.py` | TradeDB (hub.db read/write) |
| `shared/models.py` | Pydantic models (TradeQueue, TradeProposal, etc.) |
| `docker-compose.yml` | Container orchestration |

---

## What NOT To Do

- **Don't add broad scanning to bots.** No full market scanning, no
  symbol filtering, no independent data fetching. Bots only fetch data
  for specific symbols they're already trading or that the hub told them
  to watch (ExtremeWatcher shortlist). All broad intelligence lives in
  the hub.

- **Don't create multiple queues.** One queue. Period. No per-bot queues,
  no shadow queues, no staging areas inside the bot.

- **Don't make bots talk to each other.** All coordination goes through
  the hub. Bots only talk to the hub and the exchange.

- **Don't persist state in bots.** No local DB, no state files. Everything
  goes to hub.db via HTTP.

- **Don't add complexity to the queue.** Serve 1 proposal, lock 60s, bot
  reports immediately. No broadcasting, no copy-on-read, no round-robin.
  Don't buffer queue outcomes — always report consumed/rejected right away.

- **Don't lower test coverage thresholds.** Write tests instead.

- **Don't touch hub.db directly.** Use TradeDB methods. Never DROP TABLE,
  DELETE FROM, or raw SQL outside the store module.

- **Don't add hub communication to idle bots.** Idle bots only watch a
  local activation file. No HTTP, no exchange, no nothing.
