# Trading Bot — 10-Day Execution Plan

> **Before doing anything, read [docs/ARCHITECTURE.md](ARCHITECTURE.md).**
> It describes the system design, data flows, and constraints. Violating
> it (adding logic to bots, creating extra queues, etc.) is a bug.

## Goal

Run 10 trading bots on Binance testnet for 10+ continuous days, each starting
with $1,000 (total system capital: $10,000). An 11th container (the Hub) runs
with $0 balance — it handles dashboard, coordination, and trade persistence
only.

The agent operating this plan has full autonomy to add, remove, or reconfigure
strategies at any time based on observed results. The only hard constraints are:

1. **Starting capital: $1,000 per bot × 10 bots = $10,000 total**
   (SESSION_BUDGET=1000 per bot, hub SESSION_BUDGET=0)
2. **Don't blow up** — if any bot's balance drops below $600, halt it and
   reassess. If a bot hits $0, log what happened, analyze why, archive
   the attempt, and restart that bot with $1,000. Each blown account is a
   lesson, not a failure — but never repeat the same mistake twice.
3. **Write reports** — daily snapshot + a final summary at the end
4. **Fix what breaks** — if something crashes, fix it, **reset the 10-day
   counter to Day 1**, and start a fresh run. The 10 days must be
   consecutive and uninterrupted. Log what broke and how it was fixed
   before restarting. Previous run data stays in `docs/reports/` as
   a separate attempt (e.g., `attempt_1/`, `attempt_2/`).

Everything else — which strategies to run, when to change them, leverage,
risk params, symbols — is at the agent's discretion. Use the analytics
engine, trade DB, and logs to make data-driven decisions from the Hub.

### Active Bots (5 — trade from Day 1)

These bots have `is_default=True` in `config/bot_profiles.py`. They connect
to the exchange, register strategies, and process trade queue proposals
immediately on startup.

| Bot | Profile | Strategies | Style | Notable Overrides |
|-----|---------|-----------|-------|-------------------|
| bot-extreme | Extreme Mover | compound_momentum, market_open_volatility | momentum | 20x leverage, 10 max positions, extreme mode on |
| bot-momentum | Momentum | compound_momentum, market_open_volatility | momentum | defaults |
| bot-indicators | Technical Indicators | rsi, macd | momentum | defaults |
| bot-meanrev | Mean Reversion | bollinger, mean_reversion | meanrev | slower tick (120s idle / 60s active) |
| bot-swing | Swing / Grid | swing_opportunity, grid | swing | slowest tick (600s idle / 300s active) |

### Idle Bots (5 — running but not trading)

These bots have `is_default=False`. They start in **lean idle mode**: no
exchange connection, no strategies loaded, no hub communication. They watch
a local activation file (`data/{bot_id}/activate`) every 10 seconds. Enable
them via the dashboard toggle or `/api/bot-profile/{id}/toggle` — the hub
writes the file, the bot detects it and starts up.

| Bot | Profile | Strategies | Style | Notable Overrides |
|-----|---------|-----------|-------|-------------------|
| bot-scalper | Scalper | compound_momentum | momentum | fast tick (15s), tight SL (0.8%), low TP (2%) |
| bot-fullstack | Full Stack | all 8 strategies | momentum | 10 max positions |
| bot-conservative | Conservative | rsi, bollinger | meanrev | 3x leverage, $20 risk, 1% SL |
| bot-aggressive | Aggressive | compound_momentum, rsi | momentum | 20x leverage, $100 risk, 10% TP |
| bot-hedger | Hedge Heavy | compound_momentum, mean_reversion | momentum | hedge on (40% ratio, 2% min profit) |

### Hub (no trading)

| Container | Role | Balance |
|-----------|------|---------|
| bot-hub | Dashboard, coordination, trade persistence (hub.db), CEX scanner aggregation | $0 |

### Trading Modes

| Mode | What happens | Use when |
|------|-------------|----------|
| `paper_local` | PaperExchange simulates trades locally. Nothing hits the exchange. | Pre-launch testing, or exchanges without testnet (MEXC) |
| `paper_live` | Real orders on exchange testnet (demo.binance.com). Fake money, real execution. | 10-day run (Binance/Bybit only — they have testnets) |
| `live` | Real orders on production exchange. Real money. | After 10-day run proves the bot works |

**Note:** MEXC has no testnet. If you set `paper_live` with MEXC, the bot
automatically falls back to `paper_local` to prevent real-money trades.

**Pre-launch** uses `paper_local` (TRADING_MODE=paper_local). This validates
the code without touching the exchange. Once all checks pass, switch to
`paper_live` for Day 1.

**The 10-day run** uses `paper_live` (TRADING_MODE=paper_live). Orders are
placed on Binance testnet — you can see them on demo.binance.com. The
testnet pre-funds accounts with $5,000–$10,000 USDT. SESSION_BUDGET=1000
caps each bot's usable balance to $1,000. The hub runs with SESSION_BUDGET=0
(no trading). Trade PnL is tracked per-trade in `hub.db` (via the hub) and
the daily target tracker.

To reset after a blown bot: restart that specific container. It re-reads
SESSION_BUDGET=1000 and starts fresh with a new PaperExchange balance.

---

## Available Strategies

| Name | Description | Mode |
|------|-------------|------|
| `compound_momentum` | Trend-following with compounding momentum signals | PYRAMID |
| `market_open_volatility` | Trades volatility around market open windows (US/Asia) | PYRAMID |
| `swing_opportunity` | Multi-day swing trades on longer timeframes | PYRAMID |
| `rsi` | RSI overbought/oversold signals | PYRAMID |
| `macd` | MACD crossover signals | PYRAMID |
| `bollinger` | Bollinger Band breakout/reversion | PYRAMID |
| `mean_reversion` | Mean reversion on extended moves | PYRAMID |
| `grid` | Grid trading with fixed intervals | PYRAMID |

All use PYRAMID mode (DCA in): start small → DCA on dips → lever up on recovery.

---

## How to Operate (for the agent)

### Mindset

You are a trading desk operator. You have 10 days and $10,000 across 10 bots. Your job is to:
- Keep the system running 24/7
- Watch what the strategies are doing
- Cut what's losing, double down on what's working
- Tune parameters (leverage, stop-loss, position size) based on results
- Document everything so we learn from it

### Decision Framework

After each day (or sooner if something is clearly wrong):

1. **Check the numbers:** Query `data/hub.db` for win rate, avg PnL,
   per-strategy breakdown. Check the analytics dashboard.
2. **Assess:** Is the bot making money? Which strategies are contributing?
   Which are bleeding? Are positions getting stuck?
3. **Act:** Add/remove strategies, adjust leverage, tighten/loosen stops,
   change symbols, tweak risk params. Or change nothing if it's working.
4. **Log it:** Write a short entry in `docs/reports/daily_log.md` —
   what you saw, what you changed, why.

There are no fixed phases. If compound_momentum is printing money on day 1,
keep it running. If RSI is losing on day 3, kill it immediately. If the market
is ranging and grid trading looks promising, try it. Be adaptive.

### Things to Experiment With

- **Strategy combinations** — do some strategies complement each other?
  (e.g., momentum + mean_reversion might catch both trends and reversals)
- **Leverage** — 5x vs 10x vs 20x. Higher leverage = faster gains but
  tighter liquidation. What's the sweet spot for $100?
- **Position sizing** — INITIAL_RISK_AMOUNT is $50 by default. Try $20
  for more trades or $80 for concentrated bets.
- **Stop-loss width** — 1.5% default. Too tight = stopped out by noise.
  Too wide = big losses. What works for BTC vs ETH?
- **Symbols** — start with BTC/USDT and ETH/USDT. If those work, consider
  adding other pairs the scanner finds.
- **Hedging** — HEDGE_ENABLED=true. Does it help or just add noise?
- **DCA parameters** — DCA_INTERVAL_PCT, DCA_MULTIPLIER. Aggressive DCA
  means bigger positions on dips. Conservative = smaller adds.

### What "Working" Means

- **Minimum:** Total balance stays above $6,000 after 10 days (didn't blow up)
- **Good:** Total balance grows to $12,000+ (20% over 10 days)
- **Great:** Consistent daily positive PnL across most bots, even if small
- **Target:** Hit the 10% daily target at least a few times per bot

Even if we lose money, the data is valuable. Knowing which strategies
fail and under what conditions is just as important as finding winners.
Comparing performance across bots reveals which strategy combos work best.

---

## Pre-Launch: Running & Testing

**Mode: `paper_local`** — all pre-launch testing uses local simulation.
No orders hit the exchange. This validates the code only.

Before Day 1 starts, every component must be verified in isolation and then
end-to-end. This phase has **no timer** — take as long as needed to get a
clean bill of health. The 10-day clock starts only after everything below
passes.

### 1. Codebase Analysis (new session checkpoint)

If you are a new agent or starting a fresh chat session, **do a full code
analysis before running anything.** Read the key modules, understand the
architecture, and verify that nothing is obviously broken. This codebase is
complex (80+ files, 3 services, 8 strategies, multiple intel feeds) and
context from prior sessions may be stale.

At minimum, review:
- `bot.py` — main entry point, strategy registration, tick loop
- `core/exchange/` — exchange adapters, PaperExchange (simulated trading)
- `core/risk/` — risk manager, daily target tracker
- `core/orders/` — order manager, scaler, trailing, hedge
- `strategies/` — all strategy implementations
- `services/` — monitor, analytics, signal generator
- `config/settings.py` — all configuration knobs
- `docker-compose.yml`, `Dockerfile.hub`, `Dockerfile.bot` — container setup
- `docs/EXECUTION_PLAN.md` — this file (you're reading it)

**Optional:** `.cursor/chat_history.md` has previous session context and
decisions. Only read it if the user explicitly asks — otherwise figure
everything out from the code and this plan.

Look for: mismatched ports, broken imports, stale references, logic bugs,
anything that would prevent a clean startup. Fix issues before proceeding.

**Bug classification — strict rules:**

Only flag things that will **crash the bot** or **lose money**. A finding
must demonstrate a concrete failure: a traceback that will happen, or a
dollar amount that will be wrong. If neither applies, it's not a bug.

- A missing log statement is NOT a bug
- A missing try/except is NOT a bug unless the exception WILL occur in normal flow
- "Could theoretically crash if the API returns X" is NOT a bug unless X is a documented/observed API response
- Defensive coding suggestions are NOT bugs
- Style and maintainability issues are NOT bugs

If a scan finds zero bugs under these rules, the group is **CLEAN** — move
on. Do not re-scan clean groups hoping to find something. The audit
converges when all groups are clean, not when the agent runs out of things
to suggest.

### 2. Preflight Check

```bash
.venv/bin/python scripts/preflight_check.py
```

This validates: API keys (Binance testnet), exchange connectivity, USDT balance,
BTC/USDT ticker, futures support, leverage, risk limits, trading mode, email.
Fix any failures before proceeding.

### 3. Docker Build (no run)

Build all images and verify they compile without errors. **Do NOT start
containers at this step** — the run happens in step 6 after configuration.

```bash
docker compose build
```

Verify: all images build successfully (exit code 0, no build errors).

### 4. Configure Active & Idle Bots

Verify `config/bot_profiles.py` has the correct `is_default` flags:
- `is_default=True`: extreme, momentum, indicators, meanrev, swing (5 active)
- `is_default=False`: scalper, fullstack, conservative, aggressive, hedger (5 idle)

Verify `.env` has the correct trading mode (`TRADING_MODE=paper_local` for
pre-launch, `TRADING_MODE=paper_live` for the 10-day run).

### 5. Launch & Smoke Test

Clean ephemeral state, start all containers, and verify health:

```bash
# Wipe ephemeral state (keeps .db files intact):
find "$HOST_DATA_DIR" -name "*.json" -o -name "*.lock" | xargs rm -f

# Start
docker compose up -d

# Wait for hub health check (start_period is 90s)
sleep 15
docker compose ps
```

Verify:
- Hub + all 10 bot containers show "healthy" in `docker compose ps`
- Hub health: `curl -s http://localhost:9035/health` → `{"status":"ok","bot_running":true,"mode":"hub"}`
- 5 active bots show balance ~$1,000 and strategies registered (check logs)
- 5 idle bots show "lean idle mode" or "IDLE" status (check logs)
- Dashboard total balance shows ~$5,000 (5 active × $1,000; idle bots + hub excluded)
- Monitor is populating intel data (check hub logs for intel polling)

### 6. End-to-End Signal Flow

Watch logs for ~10–15 minutes and confirm the full pipeline:

```
Monitor polls intel + BinanceFuturesScanner updates incremental symbol state
  (`cex_binance_symbol_state`) and minute snapshots (`cex_binance_snapshots`)
  → merged hot movers (Binance primary, legacy scanner additive)
  → writes IntelSnapshot to HubState → SignalGenerator produces TradeProposals
  → proposals stored in HubState trade queue → active bots receive via /internal/report response
  → bot validates + executes → PaperExchange.place_order() → trade pushed to hub via HTTP
Hub writes hub.db → Analytics reads hub.db → computes strategy weights → persists analytics
Idle bots stay in lean idle mode (activation-file watch only): no exchange
connection and no hub communication until activated
```

If no trades fire naturally (market is quiet), verify the signal path is at least
being evaluated by checking log lines like "Queue: warmup" or "Risk check: passed/rejected".

### 7. Teardown (only if re-running pre-launch)

```bash
docker compose down
find "$HOST_DATA_DIR" -name "*.json" -o -name "*.lock" | xargs rm -f
```

Once all steps pass, the system is running and you proceed to Day 1.
For Day 1, you switch `.env` to `paper_live`, rebuild, and restart.

---

## Day 1: Bootstrap

**Switch to `paper_live` now.** Change `TRADING_MODE=paper_live` in `.env`.
From this point, all orders go to Binance testnet (demo.binance.com).
You can see trades, positions, and balance on the exchange dashboard.

This is the critical first day. The priority is: **make sure everything works.**

### Step 1: Switch mode & preflight
```bash
cd /Users/damirdjordjev/workspace/trading-bot
# Ensure .env has TRADING_MODE=paper_live
.venv/bin/python scripts/preflight_check.py
```

Fix any issues before proceeding.

### Step 2: Start via Docker
```bash
docker compose build
docker compose up -d
```

### Step 3: Verify
- Dashboard loads at http://localhost:9035
- All containers healthy: `docker compose ps`
- 10 trading bots + hub running (monitor + analytics are in-process inside hub)
- Total balance ~$10,000 (10 × $1,000 per bot; hub $0)
- Each bot has its strategies registered (check Strategies tab / dropdown)
- Check demo.binance.com — trades should appear there

### Step 4: Watch
Monitor logs for the first 30–60 minutes. Look for:
- Successful exchange connection
- Strategy signals being generated
- Orders being placed on testnet (check demo.binance.com)
- No repeating errors or crashes

### Step 5: First report
Once everything is stable, write `docs/reports/day01.md` with the
initial state: balance, strategies active, any issues found and fixed.

---

## Startup Commands

### Docker (recommended for 10-day run)

```bash
cd /Users/damirdjordjev/workspace/trading-bot

# Build
docker compose build

# Start all services
docker compose up -d

# Live logs (all bots)
docker compose logs -f

# Health check
docker compose ps

# Stop
docker compose down

# Helper script (alternative)
./scripts/run_session.sh start
./scripts/run_session.sh status
./scripts/run_session.sh logs
./scripts/run_session.sh snapshot
./scripts/run_session.sh stop
```

### Direct Python (for debugging only)

```bash
# Terminal 1: Hub (dashboard + monitor + analytics)
.venv/bin/python hub_main.py

# Terminal 2: Bot
BOT_ID=momentum BOT_STYLE=momentum .venv/bin/python bot.py
```

---

## Crash Recovery

If the system crashes, Cursor restarts, or you're a new agent picking this up:

### 1. Assess the situation
```bash
cd /Users/damirdjordjev/workspace/trading-bot
docker compose ps
docker compose logs --tail 50 bot-hub
```

### 2. Check persisted state
```bash
# Trade data is on the host — no Docker needed to inspect it:
ls -la $HOST_DATA_DIR/*.db
# Or via a running container:
docker compose exec bot-hub python -c "
from db import TradeDB
db = TradeDB(); db.connect()
print(f'Trades: {db.trade_count()}')
"

# Bot status (via hub API — bot status is in-memory)
curl -s http://localhost:9035/health
```

### 3. Read the daily log
Check `docs/reports/daily_log.md` to understand what was running,
what changes were made, and where things left off.

### 4. Restart
```bash
# Containers exist but stopped:
docker compose up -d

# Containers unhealthy:
docker compose restart

# Code was changed:
docker compose build && docker compose up -d
```

### 5. Verify
- Dashboard at http://localhost:9035
- Balance matches expectations
- Positions on https://demo.binance.com/en/futures match bot state

---

## Persistent Data (survives restarts, crashes, and Docker destruction)

Data and logs are stored on the **host filesystem** via bind mounts, not
Docker named volumes. This means they survive `docker compose down -v`,
container rebuilds, and even full Docker removal.

Host paths are configured in `.env`:

| Env var | Default path | Container mount |
|---------|-------------|----------------|
| `HOST_DATA_DIR` | `/Users/damirdjordjev/workspace/trading-bot-data` | `/app/data` |
| `HOST_LOGS_DIR` | `/Users/damirdjordjev/workspace/trading-bot-logs` | `/app/logs` |

| Data | Host location | Notes |
|------|--------------|-------|
| Hub database | `$HOST_DATA_DIR/hub.db` | **Sole** persistent trade + analytics DB (critical) |
| Binance snapshots | `hub.db.cex_binance_snapshots` | Minute-level Binance futures snapshot history |
| Binance symbol state | `hub.db.cex_binance_symbol_state` | Incremental one-row-per-symbol multi-horizon aggregates |
| Analytics | `$HOST_DATA_DIR/analytics_state.json` | Persisted strategy scores (survives restarts) |
| Bot logs | `$HOST_LOGS_DIR/bot_*.log` | 1-day rotation, 30-day retention |
| Monitor logs | `$HOST_LOGS_DIR/monitor_*.log` | 1-day rotation, 30-day retention |
| Analytics logs | `$HOST_LOGS_DIR/analytics_*.log` | 1-day rotation, 30-day retention |
| Daily reports | `docs/reports/` | Git (committed) |
| Daily backups | `/Users/damirdjordjev/workspace/trading-bot-backups/` | Host-side, launchd 3 AM daily |

**Important:** Only `hub.db` is critical. Per-bot `trades.db` files no longer
exist — trading bots are fully stateless (in-memory only). All trade
persistence flows through the hub via HTTP. Bots recover open positions from
the hub on startup and mark dead trades via `recovery_close`.

JSON/lock files are ephemeral state that gets recreated on startup — safe
to delete during rebuilds.

---

## Reporting

### Daily Log (`docs/reports/daily_log.md`)

Append an entry every day (or after every significant change):

```markdown
## Day N — YYYY-MM-DD

**Balance:** $XX.XX (start: $100)
**Trades today:** N (W wins / L losses)
**Active strategies:** [list]
**Changes made:** [what and why]
**Issues:** [any bugs or unexpected behavior]
**Notes:** [market conditions, observations]
```

### Final Report (`docs/reports/FINAL_REPORT.md`)

Written at the end of the 10-day run:

1. **Results** — starting vs ending balance, total PnL, total trades
2. **Strategy Rankings** — which strategies performed best (by win rate,
   total PnL, risk-adjusted return)
3. **Best Configuration Found** — the combo of strategies, leverage, and
   risk params that worked best
4. **Lessons Learned** — what surprised us, what failed, market insights
5. **Recommendation for Live** — go/no-go, suggested config, suggested
   starting capital

---

## Key Config (.env)

```ini
# Pre-launch testing:
TRADING_MODE=paper_local
# 10-day run (switch when pre-launch passes):
# TRADING_MODE=paper_live
EXCHANGE=binance
ALLOWED_MARKET_TYPES=spot,futures
SESSION_BUDGET=1000          # $1,000 per trading bot (hub overrides to $0)
DEFAULT_LEVERAGE=10

MAX_POSITION_SIZE_PCT=5
MAX_DAILY_LOSS_PCT=3
STOP_LOSS_PCT=1.5
TAKE_PROFIT_PCT=5
MAX_CONCURRENT_POSITIONS=3

NOTIFY_EMAIL=damirdjordjev@gmail.com
NOTIFICATIONS_ENABLED=liquidation,stop_loss,spike_detected,daily_summary
```

**Per-bot overrides** are set in `docker-compose.yml` via environment
variables. The hub has `SESSION_BUDGET=0` hardcoded. Individual bots
(e.g. bot-extreme) can override leverage, risk, and strategy params.

---

## Strategy Change Procedure

1. Edit `config/bot_profiles.py` → update strategies for the target bot profile
2. `docker compose build && docker compose up -d`
3. Verify in dashboard → Strategies tab
4. Log the change in `docs/reports/daily_log.md`

---

## Quick Reference

| Action | Command |
|--------|---------|
| Start | `docker compose up -d` |
| Stop | `docker compose down` |
| Rebuild | `docker compose build && docker compose up -d` |
| Logs (hub) | `docker compose logs -f bot-hub` |
| Logs (bot) | `docker compose logs -f bot-momentum` |
| Logs (all) | `docker compose logs -f` |
| Health | `docker compose ps` |
| Restart | `docker compose restart bot-hub` |
| Preflight | `.venv/bin/python scripts/preflight_check.py` |
| Snapshot | `./scripts/run_session.sh snapshot` |
| Dashboard | http://localhost:9035 |
| Exchange | https://demo.binance.com/en/futures |
