# DigitalOcean Deployment Tracker

Last updated: 2026-03-06
Owner: Damir + Cursor agent

## Goal

Deploy the full trading stack (hub + postgres + 10 bots + monitoring) to one DigitalOcean Droplet with a stable Reserved IPv4.

## Target Runtime Profile

- Droplet size: 4 vCPU / 8 GB RAM / 60+ GB SSD (minimum)
- Preferred headroom: 8 vCPU / 16 GB RAM / 80 GB SSD
- Network: Reserved IPv4 attached to Droplet

## Runtime Sections

### trade-borg-cex (active)

This section is the source of truth for current CEX deployment/runtime settings and image-related state.

- Host/IP: `129.212.252.129`
- Hub URL: `http://129.212.252.129:9045`
- Hub port policy:
  - internal container port: `9035`
  - external published port: `9045` (`DASHBOARD_PUBLISH_PORT`)
  - published bind address: `127.0.0.1` only
- Local access (Mac):
  - use `http://localhost:9045`
  - persistent tunnel managed by LaunchAgent: `~/Library/LaunchAgents/com.tradeborg.ssh-tunnel.plist`
  - label: `com.tradeborg.ssh-tunnel`
- Active trading profile: `extreme` only
- Idle deployed profiles: `hedger`, `indicators` (containers running, profiles disabled)
- Disabled & not deployed on prod (for now): `momentum`, `meanrev`, `swing`, `scalper`, `fullstack`, `conservative`, `aggressive`
- Dashboard profile visibility allowlist: `BOT_PROFILES_VISIBLE_IDS=extreme,hedger,indicators`
- Conservative runtime overrides:
  - `DEFAULT_LEVERAGE=2`
  - `MAX_POSITION_SIZE_PCT=2`
  - `STOP_LOSS_PCT=1.0`
  - `INITIAL_RISK_AMOUNT=5`
  - `MAX_CONCURRENT_POSITIONS=1`
  - `HEDGE_ENABLED=false`
- Trading mode/runtime:
  - `TRADING_MODE=live`
  - `SESSION_BUDGET=50`
  - `EXCHANGE=binance`
- Logging policy:
  - file level follows `LOG_LEVEL`
  - rotation: `100 MB`
  - retention: `7 days`
- Droplet reboot behavior (current production safety mode):
  - stack does **not** auto-start after droplet restart
  - Docker auto-start is intentionally disabled/masked on the droplet to allow manual incident recovery first
  - expected state right after reboot: no trading containers running until explicitly started
- Image/runtime identifiers:
  - Compose project: `trading-bot`
  - Hub image: `trading-bot-bot-hub`
  - Active bot image: `trading-bot-bot-extreme`
  - Postgres image: `postgres:16`
  - Monitoring images: `trading-bot-grafana`, `trading-bot-prometheus`, `trading-bot-loki`, `trading-bot-promtail`

### Re-apply conservative-only mode quickly

```bash
python3 - <<'PY'
import json
from urllib.request import Request, urlopen

BASE = "http://129.212.252.129:9045"
TARGET = "conservative"

profiles = json.loads(urlopen(f"{BASE}/api/bot-profiles").read().decode())
for p in profiles:
    bid = p["id"]
    enabled = bool(p.get("enabled"))
    should_enable = (bid == TARGET)
    if enabled != should_enable:
        req = Request(
            f"{BASE}/api/bot-profile/{bid}/toggle",
            method="POST",
            data=b"",
        )
        urlopen(req).read()

print(urlopen(f"{BASE}/api/bot-profiles").read().decode())
PY
```

### trade-borg-dex (planned)

- Status: not started
- Purpose: reserved for future DEX project deployment settings and image/runtime records
- Rule: keep DEX settings/images isolated here; do not mix with `trade-borg-cex`

## Progress Checklist

### Phase 1 - Account and Access

- [x] Create DigitalOcean account
- [ ] Add billing method
- [ ] Enable 2FA
- [x] Add SSH public key to DigitalOcean
- [x] Confirm local SSH works to a test host

### Phase 2 - Server Provisioning

- [x] Create Ubuntu 24.04 LTS Droplet
- [x] Choose region (closest practical latency target)
- [x] Assign hostname
- [x] Create Reserved IPv4
- [x] Attach Reserved IPv4 to Droplet
- [ ] (Optional) Point DNS A record to Reserved IPv4

### Phase 3 - Base Server Hardening

- [x] SSH in as root
- [ ] Create non-root sudo user
- [ ] Disable password SSH auth
- [ ] Disable root SSH login
- [ ] Configure UFW (allow `22`, `9035`, optional `3001`)
- [ ] Set timezone and NTP checks

### Phase 4 - Runtime Setup

- [x] Install Docker Engine + Compose plugin
- [x] Install Git
- [x] Create directories (`/opt/trading-bot`, `/opt/trading-bot-data`, `/opt/trading-bot-logs`)
- [x] Clone repo into `/opt/trading-bot`
- [x] Add production `.env` values
- [x] Set `HOST_DATA_DIR=/opt/trading-bot-data`
- [x] Set `HOST_LOGS_DIR=/opt/trading-bot-logs`

### Phase 5 - First Deploy

- [x] Pull latest code on target branch
- [x] Build images and start stack
- [x] Verify all services healthy
- [x] Verify dashboard/API reachability
- [ ] Verify bot-hub to postgres connectivity
- [ ] Verify at least one bot can report to hub

### Phase 6 - Ops and Safety

- [x] Configure log retention policy
- [ ] Configure low-cost external DB backup destination
- [ ] Add backup script + schedule
- [ ] Add restore drill notes
- [ ] Add uptime/health checks

### Phase 7 - Cutover

- [ ] Freeze local environment changes
- [ ] Export/import any required current state
- [ ] Final config sanity check (keys, mode, exchanges)
- [ ] Planned cutover window
- [ ] Start cloud stack
- [ ] Post-cutover validation checklist

## Environment Values to Fill Before Deploy

- `TRADING_MODE=...`
- `EXCHANGE=...`
- `BINANCE_*` and/or `BYBIT_*` credentials
- `HUB_DB_BACKEND=postgres`
- `HUB_POSTGRES_DSN=...`
- `HOST_DATA_DIR=/opt/trading-bot-data`
- `HOST_LOGS_DIR=/opt/trading-bot-logs`
- `DASHBOARD_TOKEN=...` (set this)

## Quick Resume Notes (for future chats)

When resuming this deployment in a new chat:

1. Read this file first.
2. Report current checked boxes.
3. Continue from the first unchecked item in order.
4. Keep all changes and commands focused on DigitalOcean deployment only.

## Session Notes

- 2026-03-05: Deployment planning started. Chosen direction: DigitalOcean first.
- 2026-03-05: Droplet created (`Ubuntu 24.04`, FRA1, 2 vCPU/4 GB/80 GB). SSH verified using `do_trading_bot` key.
- 2026-03-05: Reserved IPv4 attached (`129.212.252.129`). Docker + Compose + Git installed. Base deploy directories created under `/opt`.
- 2026-03-05: Workspace synced to `/opt/trading-bot`; `.env` installed on server with host paths set to `/opt/trading-bot-data` and `/opt/trading-bot-logs`; first `docker compose up -d --build` completed and services started.
- 2026-03-05: Logging policy tightened for bot + hub: file sink now follows `LOG_LEVEL`, rotates at `100 MB`, retention `7 days`; deployed to Droplet with rebuild.
- 2026-03-05: Applied `$50 steady` runtime mode — only `conservative` profile enabled in hub; all other profiles disabled.
- 2026-03-05: Conservative container runtime env verified live: `DEFAULT_LEVERAGE=2`, `MAX_POSITION_SIZE_PCT=2`, `INITIAL_RISK_AMOUNT=5`, `MAX_CONCURRENT_POSITIONS=1`, `HEDGE_ENABLED=false`.
- 2026-03-06: Hub external access moved to `:9045` (internal service remains `:9035`). Compose now uses `DASHBOARD_PUBLISH_PORT` to avoid breaking bot-to-hub internal routing.
- 2026-03-06: Performed full DO clean start for live run (down, wiped Postgres/monitoring volumes, wiped ephemeral host state, rebuilt/up).
- 2026-03-06: Re-applied conservative-only profile and stopped all non-conservative bot containers.
- 2026-03-06: Hub now cleanly fetches production symbols via CCXT (Binance + Bybit); prior `lighter_client` ccxt import issue resolved by pinning `ccxt==4.5.39`.
- 2026-03-06: Live trading auth verified on DO. `bot-conservative` now connects in `TRADING_MODE=live` with Binance production (`sandbox=False`), market load succeeds, and the prior `-2015` error is resolved after IP whitelist update.
- 2026-03-06: Runtime switched to `extreme` for live execution. Kept `hedger` and `indicators` deployed as idle (disabled in hub), and removed 7 non-target bot containers from prod to reduce CPU/RAM noise.
- 2026-03-06: Bot Profiles UI/API now filtered to deployed IDs only (`extreme`, `hedger`, `indicators`) to avoid confusing non-deployed idle cards.
- 2026-03-06: Production droplet set to manual-recovery mode: Docker auto-start on reboot is disabled/masked, so stack remains down after restart until we intentionally bring it up.
