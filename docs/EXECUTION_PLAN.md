# Trading Bot — 10-Day Execution Plan

## Goal

Run 5 trading bots on Binance testnet for 10+ continuous days, each starting
with $1,000 (total system capital: $5,000). A 6th container (the Hub) runs
with $0 balance — it handles dashboard, coordination, and trade persistence
only.

The agent operating this plan has full autonomy to add, remove, or reconfigure
strategies at any time based on observed results. The only hard constraints are:

1. **Starting capital: $1,000 per bot × 5 bots = $5,000 total**
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
engine, trade DB, and logs to make data-driven decisions.

### Default Bots (5 active)

| Bot | Profile | Strategies | Style |
|-----|---------|-----------|-------|
| bot-extreme | Extreme Mover | compound_momentum, market_open_volatility | momentum |
| bot-momentum | Momentum | compound_momentum, market_open_volatility | momentum |
| bot-indicators | Technical Indicators | rsi, macd | momentum |
| bot-meanrev | Mean Reversion | bollinger, mean_reversion | meanrev |
| bot-swing | Swing / Grid | swing_opportunity, grid | swing |

### Idle Bots (can be activated from Settings page)

| Bot | Profile |
|-----|---------|
| bot-scalper | Scalper |
| bot-fullstack | Full Stack |
| bot-conservative | Conservative |
| bot-aggressive | Aggressive |
| bot-hedger | Hedge Heavy |

### Hub (no trading)

| Container | Role | Balance |
|-----------|------|---------|
| bot-hub | Dashboard, coordination, trade persistence (hub.db) | $0 |

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

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     bot-hub ($0, no trading)                 │
│  Dashboard :9035 ◄── browser                                │
│  /internal/report ◄── trading bots POST snapshots + reads   │
│  hub.db (sole persistent trade DB)                          │
│  Reads/writes shared JSON on behalf of bots                 │
└────────┬───────────────────────────────────────┬────────────┘
         │  HTTP POST/response                   │
    ┌────┴────────────────────────┐              │
    │  5 trading bots (in-memory) │              │
    │  bot-extreme   $1000        │              │
    │  bot-momentum  $1000        │              │
    │  bot-indicators $1000       │              │
    │  bot-meanrev   $1000        │              │
    │  bot-swing     $1000        │              │
    │  (+ 5 idle containers)      │              │
    │                             │              │
    │  Each bot:                  │              │
    │  - Strategies + Orders      │              │
    │  - Risk mgmt + Exchange     │    ┌────────┴──────────┐
    │  - Zero file I/O            │    │  data/ (shared vol)│
    │  - Reports to hub via HTTP  │    │  intel_state.json  │
    └─────────────────────────────┘    │  analytics_state   │
                                       │  trade_queue.json  │
    ┌──────────────┐  ┌──────────────┐ │  bot_status.json   │
    │   monitor    │  │  analytics   │ │  hub.db            │
    │ - Intel feeds│  │ - Scores     │ │                    │
    │ - Scanning   │  │ - Patterns   │ └────────────────────┘
    │ - Queue gen  │  │ - Feedback   │        ▲
    └──────┬───────┘  └──────┬───────┘        │
           └─────────────────┴────── read/write ┘
```

**Data flow:**
- Trading bots are **stateless** — in-memory only, zero file access
- All shared data (intel, analytics, trade queue, extreme watchlist) flows
  through the hub's `/internal/report` HTTP endpoint
- Bots POST their snapshots to hub → hub writes to shared volume on their behalf
- Hub reads intel/analytics/queue from shared volume → returns in HTTP response
- `hub.db` is the sole persistent trade DB; bots push trades via HTTP

**Shared files in `data/` (hub + monitor + analytics read/write):**
- `bot_status.json` — hub writes (proxied from bots), monitor reads
- `intel_state.json` — monitor writes, hub reads (proxied to bots)
- `analytics_state.json` — analytics writes, hub reads (proxied to bots)
- `trade_queue.json` — monitor writes proposals, hub reads (proxied to bots)
- `hub.db` — SQLite trade history (hub writes, analytics reads)

**Logs:** `logs/` directory (1-day rotation, 30-day retention)

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

You are a trading desk operator. You have 10 days and $5,000 across 5 bots. Your job is to:
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

- **Minimum:** Total balance stays above $3,000 after 10 days (didn't blow up)
- **Good:** Total balance grows to $6,000+ (20% over 10 days)
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
- `docker-compose.yml` and `Dockerfile` — container setup
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
python scripts/preflight_check.py
```

This validates: API keys (Binance testnet), exchange connectivity, USDT balance,
BTC/USDT ticker, futures support, leverage, risk limits, trading mode, email.
Fix any failures before proceeding.

### 3. Docker Build & Smoke Test

```bash
docker compose build
docker compose up -d
docker compose ps          # all 3 services should be "healthy"
docker compose logs --tail 20 trading-bot
docker compose logs --tail 20 monitor
docker compose logs --tail 20 analytics
```

Verify:
- No crash loops or repeating errors
- Dashboard loads at http://localhost:9035
- All 5 trading bots + hub show healthy in `docker compose ps`
- Each bot connects to Binance testnet (check per-bot logs)
- Dashboard total balance shows ~$5,000 (5 × $1,000; hub excluded)
- Hub balance is $0
- Monitor is writing `data/intel_state.json` (intel feeds polling)
- Analytics is writing `data/analytics_state.json`

### 4. End-to-End Signal Flow

Watch logs for ~10–15 minutes and confirm the full pipeline:

```
Strategy generates Signal → RiskManager.check_signal() → OrderManager.execute_signal()
  → PaperExchange.place_order() → trade pushed to hub via HTTP (request_key for dedup)
Monitor polls intel → writes IntelSnapshot → SignalGenerator produces TradeProposals
  → bot reads trade_queue.json → processes proposals
Hub writes hub.db → Analytics reads hub.db → computes strategy weights → writes analytics_state.json
Bot startup → asks hub for open trades → reconciles with exchange → recovery-closes dead ones
```

If no trades fire naturally (market is quiet), verify the signal path is at least
being evaluated by checking log lines like "Strategy X: no signal" or
"Risk check: passed/rejected".

### 5. Teardown & Clean Slate

```bash
docker compose down
# Wipe ephemeral state (keeps .db files intact):
find "$HOST_DATA_DIR" -name "*.json" -o -name "*.lock" | xargs rm -f
```

Once all 5 steps pass, proceed to Day 1 with confidence.

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
python scripts/preflight_check.py
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
- 5 trading bots + hub + monitor + analytics running
- Total balance ~$5,000 (5 × $1,000 per bot; hub $0)
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

# Live logs
docker compose logs -f trading-bot

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
# Terminal 1: Monitor
python run_monitor.py

# Terminal 2: Analytics
python run_analytics.py

# Terminal 3: Bot + Dashboard
python bot.py
```

---

## Crash Recovery

If the system crashes, Cursor restarts, or you're a new agent picking this up:

### 1. Assess the situation
```bash
cd /Users/damirdjordjev/workspace/trading-bot
docker compose ps
docker compose logs --tail 50 trading-bot
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

# Last bot status
cat $HOST_DATA_DIR/bot_status.json
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
| Bot status | `$HOST_DATA_DIR/bot_status.json` | Runtime state (ephemeral) |
| Intel state | `$HOST_DATA_DIR/intel_state.json` | Runtime state (ephemeral) |
| Analytics | `$HOST_DATA_DIR/analytics_state.json` | Runtime state (ephemeral) |
| Trade queue | `$HOST_DATA_DIR/trade_queue.json` | Runtime state (ephemeral) |
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

1. Edit `bot.py` → `main()` function (around line 1091)
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
| Logs (bot) | `docker compose logs -f trading-bot` |
| Logs (all) | `docker compose logs -f` |
| Health | `docker compose ps` |
| Restart | `docker compose restart trading-bot` |
| Preflight | `python scripts/preflight_check.py` |
| Snapshot | `./scripts/run_session.sh snapshot` |
| Dashboard | http://localhost:9035 |
| Exchange | https://demo.binance.com/en/futures |
