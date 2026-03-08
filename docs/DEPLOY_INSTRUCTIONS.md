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
- materializes prod runtime secrets
- enforces compose env-file wiring
- fails fast if runtime env files are not resolved in compose config

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
