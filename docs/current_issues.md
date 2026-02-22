# Current Issues

> Observed problems and things to fix.

---

| # | Issue | Status | Done |
|---|-------|--------|------|
| 1 | db-sync container keeps restarting (healthcheck uses curl to port 9035) | Open | |
| 2 | Risk rejection floating point: "size $500 > max $500" | Open | |
| 3 | Kafka consideration for internal comms if performance degrades | Parked | |

---

# Session Changes — 2026-02-22

## Completed This Session

| # | Change | Files |
|---|--------|-------|
| 1 | **Prometheus: added 5 missing bot scrape targets** — was only scraping 6/11 containers (aggressive, scalper, fullstack, conservative, hedger missing) | `prometheus/prometheus.yml` |
| 2 | **Grafana: deduplicated system metrics** — system-level panels (memory, disk, swap, load, network) now filter by `job="bot-hub"` instead of showing N identical lines. CPU/memory panels show per-bot process metrics with `{{bot_id}}` labels. | `grafana/dashboards/trading-bot.json` |
| 3 | **Removed deposit tracking entirely** — fake deposit detection, DB table, API endpoints, bot push logic all removed. `update_balance()` is now a simple one-liner. | `core/risk/daily_target.py`, `bot.py`, `db/hub_store.py`, `web/server.py`, tests |
| 4 | **Hub excluded from Bot Profiles** — `GET /api/bot-profiles` now skips `is_hub=True` profiles so the dashboard only shows trading bots | `web/server.py` |
| 5 | **CLOSE_ALL in fast monitor loop** — emergency kill switch now fires within 1-2s instead of waiting up to 60s for the next tick | `bot.py` |
| 6 | **Consolidated daily report** — one compound email from hub at midnight UTC, not one per bot. Hub aggregates all bot data into a single email with per-bot breakdown. | `hub_main.py`, `bot.py` |
| 7 | **All time-critical checks in fast loop** — CLOSE_ALL, legendary+reversal close, trailing stops, break-even, liquidation, expired trades all run every 1-2s | `bot.py` |
| 8 | **Fixed stale docs** — removed references to file-based trade queue (`trade_queue.json`, `intel_state.json`, `bot_status.json`). Docs now reflect in-memory HubState. | `docs/EXECUTION_PLAN.md`, `shared/models.py`, `services/signal_generator.py` |

## Pending / Known Issues

| # | Issue | Notes |
|---|-------|-------|
| 1 | Docker not responding to CLI | Docker Desktop restarted with fixes, CLI not yet available. Need rebuild to deploy all changes. |
| 2 | Old containers still running stale images | All code changes are on disk only — need `docker compose down && build && up` |
| 3 | Multi-bot balance allocation is honor-system | `SESSION_BUDGET` caps each bot independently but there's no hub-managed reservation. Safe rule: `sum(budgets) <= exchange balance` |
| 4 | `SharedState` (file-based) still exists in code | `shared/state.py` has the old file-based IPC. `HubState` is the active replacement. `SharedState` kept for backward compat but could be removed. |
