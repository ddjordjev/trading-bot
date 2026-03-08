# Deployment Instructions (Env-Safe)

This runbook prevents env-file mixups that break startup.

## Root Cause We Must Avoid

`docker-compose.yml` uses dynamic env-file pointers:

- `${RUNTIME_ENV_OVERRIDE_FILE}`
- `${RUNTIME_SECRETS_FILE}`

If you run raw `docker compose up/down/build` without the compose env file
(`env/local.compose.env` or `env/prod.compose.env`), those pointers are not
set, so required runtime keys can be missing inside containers.

## One Rule

Never use raw `docker compose` for deploy/restart flows.

Use only:

- Local: `make up-local`, `make fresh-local`, `make ps-local`
- Prod: `make up-prod`, `make ps-prod` or `./scripts/deploy-digitalocean.sh`

---

## Local Deploy (safe)

Use these exact commands for local work:

```bash
# full clean local restart (recommended default)
make fresh-local

# quick start without rebuild
make up-local

# status
make ps-local
```

`make fresh-local` already does:
- runtime secrets materialization
- full down/build/up
- ephemeral state wipe only

### Local Command Discipline

- Never run `docker compose up/down/build` directly.
- Never manually set `RUNTIME_ENV_OVERRIDE_FILE` for local runs.
- Prefer `scripts/run_session.sh` for day-to-day local operations:

```bash
./scripts/run_session.sh start
./scripts/run_session.sh rebuild
./scripts/run_session.sh status
```

### Local Wiring Verification (one-liner)

If there is any doubt, verify compose resolution before start:

```bash
docker compose --env-file .env --env-file env/local.compose.env config | rg "path: env/local.runtime.env|path: env/local.runtime.secrets.env"
```

Expected output includes both paths above.

---

## PROD Deploy (safe)

Prefer:

```bash
./scripts/deploy-digitalocean.sh
```

This script already:
- validates required prod env files
- runs local/remote disk-space preflight checks
- materializes prod runtime secrets
- bakes prod runtime secrets directly into deploy images
- exports/uploads/loads images one-by-one (no single giant tarball)
- backs up currently loaded `trading-bot-*` images on DO before loading new ones
- builds/loads only the reduced prod set (hub + 4 bots + monitoring)
- enforces compose env-file wiring
- fails fast if runtime env files are not resolved in compose config
- enforces `restart=no` on deployed containers after `up`
- removes non-deployed trade bot containers/images from DO after deploy

### DO image backup before replacement

Before loading new images on DO, keep a rollbackable snapshot of currently loaded
`trading-bot-*` images under `/opt/trading-bot/.artifacts/pre-deploy-image-backups/`.
Use one timestamped folder per deploy:

```bash
cd /opt/trading-bot
mkdir -p .artifacts/pre-deploy-image-backups/"$(date -u +%Y%m%dT%H%M%SZ)"
backup_dir=".artifacts/pre-deploy-image-backups/$(ls -1t .artifacts/pre-deploy-image-backups | head -n1)"
docker images --format '{{.Repository}}' | grep '^trading-bot-' | sort -u > "$backup_dir/images.txt"
while IFS= read -r img; do
  [ -z "$img" ] && continue
  docker save "$img" | gzip > "$backup_dir/${img}.tar.gz"
done < "$backup_dir/images.txt"
```

### Incident safeguards (2026-03-08)

These safeguards were added after a failed deploy sequence:

- **No monolithic image tarballs:** image transfer is per-image only.
- **No accidental full-bot rebuilds:** build step is explicitly scoped to
  `bot-hub`, `openclaw-bridge`, `bot-extreme`, `bot-hedger`,
  `bot-indicators`, `bot-swing`, and monitoring.
- **No overlapping deploy runs:** single-instance lock directory at
  `/tmp/trading-bot-prod-deploy.lockdir`.
- **SSH hang protection:** SSH uses `BatchMode=yes`, connect timeout, and
  server-alive options to fail instead of hanging.
- **Context contamination prevention:** `.artifacts/` must remain in
  `.dockerignore` so deploy bundles are never copied into image build context.
- **Disk pressure prevention:** local and remote free-space checks run before
  build/upload/load.

If any safeguard fails, stop and fix before retrying deploy.

If running manually on the target host, use only:

```bash
make up-prod
make ps-prod
```

Do not run `docker compose up -d` directly.

---

## Quick Verification

After deploy, verify hub is healthy and runtime keys are effective:

```bash
curl -sf http://localhost:9035/health
curl -sf http://localhost:9035/api/status
```

If startup fails with missing required settings, stop immediately and rerun via
the canonical entrypoint (Makefile/script), not raw compose.
