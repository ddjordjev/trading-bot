# Trading Bot — Analysis Report

Analysis date: 2025-02-19. Scope: Docker setup, services (monitor, analytics, signal generator), database, preflight, run_session.sh, and .env (keys only).

---

## 1. Docker setup summary

### Services

| Service       | Image        | Command               | Ports              | Volumes                    |
|---------------|-------------|------------------------|--------------------|----------------------------|
| trading-bot   | build: .    | `python bot.py`        | `${DASHBOARD_PORT:-9035}:9035` | bot-logs, bot-data |
| monitor       | build: .    | `python run_monitor.py`| none               | bot-logs, bot-data         |
| analytics     | build: .    | `python run_analytics.py` | none            | bot-logs, bot-data         |

### Volumes

- **bot-data**: Persists `data/` (intel_state.json, bot_status.json, trade_queue.json, analytics_state.json, trades.db). Shared by all three services.
- **bot-logs**: Persists `logs/`. Shared by all three.

### Health checks

- **trading-bot**: `curl -sf http://localhost:9035/api/status` every 30s (start period 15s, retries 3). Requires curl (present in Dockerfile).
- **monitor**: `python -c "import sys; sys.exit(0)"` every 60s. Does not verify monitor logic, only that Python runs.
- **analytics**: Same as monitor — trivial exit 0.

### Dependencies

- **trading-bot** `depends_on`: monitor and analytics with `condition: service_started`. Bot starts only after monitor and analytics are started (no wait for healthy).

### Notes

- All services use the same Dockerfile and `env_file: .env`.
- Single-container variant: `docker-compose.single.yml` runs only `trading-bot` (no monitor/analytics).
- No port conflicts: only the bot exposes 9035.

---

## 2. Service-by-service

### Monitor (`services/monitor.py`, entry: `run_monitor.py`)

**Role:** Standalone process that polls external data and writes intel + trade proposals.

**Data flow:**

- **Reads:** `data/bot_status.json` (bot deployment level: HUNTING / ACTIVE / DEPLOYED / STRESSED).
- **Writes:** `data/intel_state.json` (IntelSnapshot), `data/trade_queue.json` (TradeQueue with proposals).

**Intel sources (polling):**

- Fear & Greed, Liquidations (CoinGlass), Macro (ForexFactory), Whale sentiment, TradingView, CoinMarketCap, CoinGecko, TrendingScanner (CryptoBubbles + CMC + Gecko).

**Adaptive intensity:** Poll intervals scale by deployment level (e.g. DEPLOYED → slower TV/scanner; STRESSED → faster intel).

**Trade queue:** After each tick, builds IntelSnapshot, then runs `SignalGenerator.generate(snapshot, queue)` and writes the updated queue. Bot reads the same queue and consumes proposals.

**Communication:** File-based only (SharedState in `shared/state.py`), under `data/` (in container: `/app/data` via volume).

---

### Analytics (`services/analytics_service.py`, entry: `run_analytics.py`)

**Role:** Standalone process that scores strategies and detects patterns from trade history.

**Data flow:**

- **Reads:** `data/trades.db` (SQLite) via `db.store.TradeDB`.
- **Writes:** `data/analytics_state.json` (AnalyticsSnapshot: strategy weights, patterns, suggestions).

**Logic:** `analytics/engine.py` — `AnalyticsEngine.refresh()`:

- Strategy scoring (win rate, profit factor, expectancy, streaks, hourly/regime performance) and a composite weight.
- Pattern detection: time-of-day, regime, strategy/symbol, volatility, quick trades, DCA depth.
- Modification suggestions: disable, reduce_weight, time_filter, regime_filter, streak-based.

**Refresh:** Every 300s (run_analytics.py) or when new trades are detected (compare `trade_count()`).

**Communication:** File-based: reads DB and writes JSON via SharedState. Bot reads analytics_state.json for strategy weights.

---

### Signal generator (`services/signal_generator.py`)

**Role:** Used only by the monitor. Turns an IntelSnapshot into prioritised trade proposals (no I/O of its own).

**Output:** Adds/updates entries in a `TradeQueue` (CRITICAL / DAILY / SWING).

**Proposal types (examples):**

- **CRITICAL:** Mass liquidation reversal, macro spike, extreme 1h movers (cooldowns ~30s–2min).
- **DAILY:** Trending momentum + TV alignment, fear-zone BTC buy, multi-intel convergence, overleveraged fade (cooldowns ~1h).
- **SWING:** Capitulation dip buy, greed reversal plan, ETH rotation (cooldowns ~1–3 days).

**How proposals are created:** Rules on snapshot fields (e.g. `mass_liquidation`, `macro_spike_opportunity`, `hot_movers`, `fear_greed`, `preferred_direction`, `overleveraged_side`, `regime`, TV analyses). Cooldowns and deduplication avoid duplicate proposals for same priority/symbol/strategy.

---

## 3. Database schema

**Location:** `data/trades.db` (path in code: `db/store.py` → `DB_PATH = Path("data/trades.db")`).

**Connection:** SQLite, WAL mode. `TradeDB` in `db/store.py`; models in `db/models.py`.

**Table: `trades`**

| Column              | Type    | Notes                    |
|---------------------|--------|--------------------------|
| id                  | INTEGER| PK, autoincrement        |
| symbol              | TEXT   | e.g. BTC/USDT            |
| side                | TEXT   | long/short               |
| strategy            | TEXT   | strategy name            |
| action              | TEXT   | open/close/scale/etc     |
| scale_mode          | TEXT   | pyramid / winners / etc  |
| entry_price, exit_price | REAL |                         |
| amount              | REAL   |                          |
| leverage            | INTEGER|                          |
| pnl_usd, pnl_pct    | REAL   |                          |
| is_winner           | INTEGER| 0/1                      |
| hold_minutes        | REAL   |                          |
| was_quick_trade     | INTEGER| 0/1                      |
| was_low_liquidity   | INTEGER| 0/1                      |
| dca_count           | INTEGER|                          |
| max_drawdown_pct    | REAL   |                          |
| market_regime       | TEXT   | risk_on, normal, etc     |
| fear_greed          | INTEGER| 0–100                    |
| daily_tier          | TEXT   | losing, building, etc    |
| daily_pnl_at_entry  | REAL   |                          |
| signal_strength     | REAL   |                          |
| hour_utc, day_of_week | INTEGER |                      |
| volatility_pct      | REAL   |                          |
| opened_at, closed_at| TEXT   | ISO timestamps           |

**Indexes:** `idx_trades_strategy`, `idx_trades_symbol`, `idx_trades_closed`, `idx_trades_winner`.

**Who writes:** Bot (OrderManager path that logs to TradeDB). **Who reads:** Analytics service and any code using `TradeDB` (e.g. dashboard/API if implemented).

---

## 4. Preflight check coverage (`scripts/preflight_check.py`)

**Purpose:** Validate before going live (API, exchange, risk limits).

**Checks performed:**

1. Config loaded (mode, exchange).
2. API keys for selected exchange (MEXC/Binance/Bybit) present in settings.
3. Exchange capabilities and allowed market types.
4. Exchange connectivity (connect).
5. USDT balance ≥ `initial_risk_amount`; session budget if set.
6. Market data (e.g. BTC/USDT ticker).
7. If futures allowed: list futures symbols, set leverage on BTC/USDT.
8. Risk sanity: max_daily_loss_pct ≤ 10, initial_risk_amount ≤ 500, max_notional_position ≥ 1000.
9. Trading mode (paper vs live).
10. Email notifications (SMTP + notify email).

**Not covered:**

- Docker / compose (no check that `docker compose up` can run).
- Existence or writability of `data/` or volume mounts.
- Monitor/analytics-specific config (e.g. intel API keys beyond exchange).
- Dashboard port or dashboard health.
- Trade DB schema or migrations.

So: preflight is exchange/bot/risk focused; it does not validate the full Docker stack or file/DB layout.

---

## 5. Bugs, issues, and concerns

### 5.1 `scripts/run_session.sh` — snapshot command (broken)

**Location:** `cmd_snapshot`, inline Python.

**Problem:** Uses `db.get_recent(10)` and `t.timestamp`, `t.pnl`.  

- `TradeDB` has no method `get_recent`. It has `get_all_trades(limit=500)` (and similar).
- `TradeRecord` has `opened_at`/`closed_at`, not `timestamp`, and `pnl_usd`/`pnl_pct`, not `pnl`.

**Effect:** `./scripts/run_session.sh snapshot` will raise AttributeError/NameError.

**Fix (conceptual):** Use e.g. `db.get_all_trades(10)` and format using `t.closed_at` (or `opened_at`) and `t.pnl_usd` (or `pnl_pct`).

---

### 5.2 Monitor/analytics healthchecks are trivial

**Issue:** Both use `python -c "import sys; sys.exit(0)"`. They do not check that the service loop is running or that it can read/write shared state.

**Effect:** Compose will show “healthy” even if the process crashes after startup or if it cannot access `data/`.

**Suggestion:** Optionally add a lightweight check (e.g. that a key file was updated recently, or a small HTTP/script that touches state and exits 0).

---

### 5.3 No dependency on “healthy” for bot

**Issue:** Bot uses `depends_on: monitor, analytics` with `condition: service_started`. It does not wait for healthchecks to pass.

**Effect:** Bot can start before monitor/analytics have written initial intel/analytics state; first runs may see missing or empty files. For file-based reads this is usually handled by defaults (e.g. empty queue, default analytics), but timing can be noisier.

**Suggestion:** If you want stricter ordering, use `condition: service_healthy` once healthchecks are meaningful.

---

### 5.4 `.env` and secrets

**Checked:** `.env` exists and is used by compose (`env_file: .env`). Keys present include: TRADING_MODE, EXCHANGE, MEXC/BINANCE/BYBIT API keys (prod/test), ALLOWED_MARKET_TYPES, SESSION_BUDGET, risk params, dashboard (DASHBOARD_PORT, etc.), intel (INTEL_*, COINGLASS, CMC, COINGECKO, etc.), SMTP/NOTIFY_EMAIL, LOG_LEVEL, and others.

**Concern:** The repo’s `.env` appears to contain real API keys and secrets. Do not commit `.env` with production values; use `.env.example` and keep `.env` in `.gitignore`. The report does not list or expose any secret values.

---

### 5.5 Ports and imports

- **Ports:** Only 9035 is used; no conflicts found.
- **Imports:** `from db import TradeDB` is valid via `db/__init__.py`. Analytics uses `from db.store import TradeDB` and `from db.models import ...`; bot uses `from db import TradeDB` and `from db.models import TradeRecord`. All consistent.
- **Shared state path:** `SharedState` uses `DATA_DIR = Path("data")`. In Docker, working directory is `/app`, so `data/` is `/app/data`, which is the mounted volume. No mismatch found.

---

### 5.6 Single Dockerfile for all services

All three services use the same image. That’s correct: same codebase, same Python deps, and only the command differs. No issue.

---

## 6. Will `docker compose up` work?

**Expected to work:**

- Build: single Dockerfile, multi-stage (frontend + Python), curl installed for bot healthcheck.
- Compose: three services, shared volumes, bot depends on monitor and analytics start.
- Port: 9035 exposed and mapped from `DASHBOARD_PORT`.
- Data dir: created by services and SharedState/TradeDB; volume provides persistence.

**Likely to work only after fixing:**

- `run_session.sh snapshot`: fix DB API and field names as in 5.1.

**Recommendations:**

1. Fix `run_session.sh` snapshot to use `get_all_trades` and TradeRecord fields (`closed_at`/`opened_at`, `pnl_usd`/`pnl_pct`).
2. Ensure `.env` is gitignored and not committed with real secrets.
3. Optionally harden healthchecks for monitor/analytics and use `service_healthy` for the bot if you want stricter startup ordering.

---

*End of report.*
