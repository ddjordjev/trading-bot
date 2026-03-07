DOCKER_BIN := /Applications/Docker.app/Contents/Resources/bin/docker
DC_LOCAL := $(DOCKER_BIN) compose --env-file .env --env-file env/local.compose.env
DC_PROD := $(DOCKER_BIN) compose --env-file .env --env-file env/prod.compose.env
HOST_DATA_DIR ?= /Users/damirdjordjev/workspace/trading-bot-data

.PHONY: up-local down-local build-local fresh-local ps-local enable-all-local-bots \
	up-prod down-prod ps-prod

up-local:
	./scripts/materialize_runtime_secrets.sh local
	$(DC_LOCAL) up -d

down-local:
	$(DC_LOCAL) down

build-local:
	./scripts/materialize_runtime_secrets.sh local
	$(DC_LOCAL) build

fresh-local:
	./scripts/materialize_runtime_secrets.sh local
	$(DC_LOCAL) down
	find "$(HOST_DATA_DIR)" \( -name "*.json" -o -name "*.lock" -o -name "activate" -o -name "STOP" -o -name "CLOSE_ALL" \) | xargs rm -f
	$(DC_LOCAL) build
	$(DC_LOCAL) up -d

ps-local:
	$(DC_LOCAL) ps

enable-all-local-bots:
	.venv/bin/python -c "import json; from urllib.request import Request, urlopen; base='http://127.0.0.1:9035'; profiles=json.loads(urlopen(f'{base}/api/bot-profiles').read().decode()); [urlopen(Request(f\"{base}/api/bot-profile/{p['id']}/toggle\", method='POST', data=b'')).read() for p in profiles if not p.get('enabled')]; print(json.dumps(json.loads(urlopen(f'{base}/api/bot-profiles').read().decode()), indent=2))"

up-prod:
	./scripts/materialize_runtime_secrets.sh prod
	$(DC_PROD) up -d

down-prod:
	$(DC_PROD) down

ps-prod:
	$(DC_PROD) ps
