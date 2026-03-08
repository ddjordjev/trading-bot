# Production Deployment Plan

## Goal

Deploy current `master` to the existing production DigitalOcean runtime safely, using only canonical entrypoints and explicit verification gates.

## 1) Preflight (No Deploy Yet)

- Confirm branch/commit to deploy is final and on `master`.
- Confirm CI is green for that commit.
- Confirm production profile intent remains as tracked in `[/Users/damirdjordjev/workspace/trading-bot/docs/DO_DEPLOYMENT_TRACKER.md](/Users/damirdjordjev/workspace/trading-bot/docs/DO_DEPLOYMENT_TRACKER.md)` (`indicators` and `swing` active).
- Confirm required prod files exist locally and are current:
  - `[/Users/damirdjordjev/workspace/trading-bot/.env](/Users/damirdjordjev/workspace/trading-bot/.env)`
  - `[/Users/damirdjordjev/workspace/trading-bot/env/prod.compose.env](/Users/damirdjordjev/workspace/trading-bot/env/prod.compose.env)`
  - `[/Users/damirdjordjev/workspace/trading-bot/env/prod.runtime.env](/Users/damirdjordjev/workspace/trading-bot/env/prod.runtime.env)`

## 2) Use Canonical Prod Entrypoint

- Run only the documented prod path from `[/Users/damirdjordjev/workspace/trading-bot/docs/DEPLOY_INSTRUCTIONS.md](/Users/damirdjordjev/workspace/trading-bot/docs/DEPLOY_INSTRUCTIONS.md)`:
  - `./scripts/deploy-digitalocean.sh`
- This script already performs:
  - prod secrets materialization,
  - local prod image build + upload bundle,
  - remote bundle load (no remote build),
  - compose env wiring guard checks,
  - `down` + `up -d --no-build` on prod.

## 3) Immediate Post-Deploy Verification

- Verify container health on prod via canonical status check (`make ps-prod` on host equivalent in script context).
- Verify hub endpoints:
  - `curl -sf http://localhost:9045/health`
  - `curl -sf http://localhost:9045/api/status`
- Verify key runtime behavior:
  - hub connects to postgres,
  - at least one bot reports healthy,
  - no startup errors related to missing runtime vars/files.

## 4) Trading Safety Checks

- Confirm active/idle bot profile state matches expected prod policy in tracker:
  - active: `indicators`, `swing`
  - idle deployed: `extreme`, `hedger`
  - not deployed: `momentum`, `meanrev`, `scalper`, `fullstack`, `conservative`, `aggressive`
- Confirm no repeated errors for:
  - trade persistence writes,
  - queue serve/consume loop,
  - exchange auth/market load.

## 5) Rollback Readiness

- If verification fails, perform immediate rollback to the repo release tag `v1.0.0` (treat this as the known-good baseline for rollback in this plan).
- Exact rollback sequence:
  - `git checkout v1.0.0`
  - `./scripts/deploy-digitalocean.sh`
  - `git checkout develop`
- Preserve current logs and DB state for diagnosis before any additional changes.

## 6) Documentation/Operational Closeout

- Update deployment notes in `[/Users/damirdjordjev/workspace/trading-bot/docs/DO_DEPLOYMENT_TRACKER.md](/Users/damirdjordjev/workspace/trading-bot/docs/DO_DEPLOYMENT_TRACKER.md)` with:
  - deployed commit,
  - date/time,
  - active profile state,
  - outcome of health checks.
- If this is treated as a formal production release, follow release tagging policy (`version bump`, `annotated tag`, `push tag`, release note under `releases/`).
