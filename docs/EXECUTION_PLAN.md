# Trading Bot — 10-Day Execution Plan

## Goal

Run the bot on Binance testnet for 10+ continuous days starting with $100.
The agent operating this plan has full autonomy to add, remove, or reconfigure
strategies at any time based on observed results. The only hard constraints are:

1. **Starting capital: $100** (SESSION_BUDGET=100)
2. **Don't blow up** — if balance drops below $60, halt and reassess.
   If the account hits $0 (or near-zero), **don't panic and don't force it**.
   Log what happened, analyze why, archive the attempt, reset SESSION_BUDGET
   back to $100, and start a brand new 10-day session with adjusted
   strategies/params based on what you learned. Each blown account is a
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

### Testnet Balance vs Session Budget

Binance testnet pre-funds accounts with $5,000–$10,000 USDT that you
cannot remove. **This does not matter.** In paper mode, `PaperExchange`
manages its own simulated balance starting at exactly $100 (SESSION_BUDGET).
The testnet's $5,000 is only used for live market data (prices, candles).
The bot's `fetch_balance()` returns the simulated balance, not the
exchange's. So if the dashboard shows $40, you really lost $60 of your
$100. If it shows $170, you really made $70. The testnet balance is
invisible to the bot.

To reset after a blown account: restart the Docker containers. The
PaperExchange re-initializes with a fresh $100 on every startup.

---

## Architecture

```
┌──────────────────┐    ┌──────────────┐    ┌──────────────────┐
│   trading-bot    │    │   monitor    │    │    analytics     │
│   (bot.py)       │◄──►│ (run_monitor)│◄──►│ (run_analytics)  │
│                  │    │              │    │                  │
│ - Strategies     │    │ - Intel feeds│    │ - Strategy scores│
│ - Order mgmt     │    │ - Trade queue│    │ - Patterns       │
│ - Risk mgmt      │    │ - Scanning   │    │ - Suggestions    │
│ - Dashboard:8080 │    │              │    │                  │
└────────┬─────────┘    └──────┬───────┘    └────────┬─────────┘
         │                     │                     │
         └─────────── data/ (shared JSON + SQLite) ──┘
```

**IPC via files in `data/`:**
- `bot_status.json` — bot writes, monitor reads
- `intel_state.json` — monitor writes, bot reads
- `analytics_state.json` — analytics writes, bot reads
- `trade_queue.json` — monitor writes proposals, bot reads/executes
- `trades.db` — SQLite trade history (bot writes, analytics reads)

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

You are a trading desk operator. You have 10 days and $100. Your job is to:
- Keep the system running 24/7
- Watch what the strategies are doing
- Cut what's losing, double down on what's working
- Tune parameters (leverage, stop-loss, position size) based on results
- Document everything so we learn from it

### Decision Framework

After each day (or sooner if something is clearly wrong):

1. **Check the numbers:** Query `data/trades.db` for win rate, avg PnL,
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

- **Minimum:** Balance stays above $60 after 10 days (didn't blow up)
- **Good:** Balance grows to $120+ (20% over 10 days)
- **Great:** Consistent daily positive PnL, even if small
- **Target:** Hit the 10% daily target at least a few times

Even if we lose money, the data is valuable. Knowing which strategies
fail and under what conditions is just as important as finding winners.

---

## Day 1: Bootstrap

This is the critical first day. The priority is: **make sure everything works.**

### Step 1: Preflight
```bash
cd /Users/damirdjordjev/workspace/trading-bot
python scripts/preflight_check.py
```

Fix any issues before proceeding.

### Step 2: Start via Docker
```bash
docker compose build
docker compose up -d
```

### Step 3: Verify
- Dashboard loads at http://localhost:8080
- All 3 services healthy: `docker compose ps`
- Bot connects to Binance testnet (check logs)
- Balance shows ~$100 (capped from testnet's $5000)
- Strategies are registered (check Strategies tab)

### Step 4: Watch
Monitor logs for the first 30–60 minutes. Look for:
- Successful exchange connection
- Strategy signals being generated
- Orders being placed (even if simulated via PaperExchange)
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
# Trade count (data survives in Docker volumes)
docker compose exec trading-bot python -c "
from db import TradeDB
db = TradeDB(); db.connect()
print(f'Trades: {db.trade_count()}')
"

# Last bot status
docker compose exec trading-bot cat data/bot_status.json
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
- Dashboard at http://localhost:8080
- Balance matches expectations
- Positions on https://demo.binance.com/en/futures match bot state

---

## Persistent Data (survives restarts and crashes)

| Data | Location | Docker Volume |
|------|----------|---------------|
| Trade history | `data/trades.db` | `bot-data` |
| Bot status | `data/bot_status.json` | `bot-data` |
| Intel state | `data/intel_state.json` | `bot-data` |
| Analytics | `data/analytics_state.json` | `bot-data` |
| Trade queue | `data/trade_queue.json` | `bot-data` |
| Bot logs | `logs/bot_*.log` | `bot-logs` |
| Monitor logs | `logs/monitor_*.log` | `bot-logs` |
| Analytics logs | `logs/analytics_*.log` | `bot-logs` |
| Daily reports | `docs/reports/` | Git (committed) |

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
TRADING_MODE=paper
EXCHANGE=binance
ALLOWED_MARKET_TYPES=spot,futures
SESSION_BUDGET=100
DEFAULT_LEVERAGE=10

MAX_POSITION_SIZE_PCT=5
MAX_DAILY_LOSS_PCT=3
STOP_LOSS_PCT=1.5
TAKE_PROFIT_PCT=5
MAX_CONCURRENT_POSITIONS=3

NOTIFY_EMAIL=damirdjordjev@gmail.com
NOTIFICATIONS_ENABLED=liquidation,stop_loss,spike_detected,daily_summary
```

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
| Dashboard | http://localhost:8080 |
| Exchange | https://demo.binance.com/en/futures |
