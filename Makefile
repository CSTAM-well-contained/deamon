# CSTAM sandbox platform — every command you need, local Docker mode.
# Responsible for: up/down, logs, tests (pytest + cargo), demos, building the static agent for VMs.
# NOT responsible for: OpenStack deployment (infra/openstack/deploy.sh).
# Serves all criteria.

COMPOSE := $(shell docker compose version >/dev/null 2>&1 && echo "docker compose" || echo docker-compose)
API     ?= http://localhost:8000
# Rust runs in a container: no local toolchain needed. Caches stay in agent/ (git-ignored).
RUST    := docker run --rm -u $(shell id -u):$(shell id -g) -e CARGO_HOME=/src/.cargo-home \
           -v $(CURDIR)/agent:/src -w /src rust:1-bookworm

.PHONY: up down logs ps test test-api test-agent demo demo-1 demo-2 demo-3 demo-4 agent-static clean

up: .env
	$(COMPOSE) up -d --build
	@echo "waiting for $(API)/healthz …"
	@for i in $$(seq 90); do curl -fs $(API)/healthz >/dev/null && echo "API is up: $(API)/docs" && exit 0; sleep 1; done; \
	 echo "API did not become healthy"; $(COMPOSE) logs --tail=50 api; exit 1

.env:
	cp .env.example .env

down:
	$(COMPOSE) down -v --remove-orphans
	@docker ps -aq --filter label=cstam.managed=true | xargs -r docker rm -f

logs:
	$(COMPOSE) logs -f --tail=100

ps:
	$(COMPOSE) ps
	docker ps --filter label=cstam.managed=true --format 'table {{.Names}}\t{{.Status}}\t{{.Label "cstam.ip"}}'

test: test-api test-agent

test-api:
	$(COMPOSE) exec -T api pytest -q

test-agent:
	$(RUST) cargo test

demo:
	bash scripts/demo.sh

demo-1:
	bash scripts/1_provision.sh
demo-2:
	bash scripts/2_failover.sh
demo-3:
	bash scripts/3_bad_config.sh
demo-4:
	bash scripts/4_reclaim.sh

# Static (musl) gw-agent for gateway VMs → dist/gw-agent
agent-static:
	mkdir -p dist
	docker run --rm -v $(CURDIR)/agent:/src -v $(CURDIR)/dist:/dist -w /src rust:1-bookworm sh -c '\
	  apt-get update -qq && apt-get install -y -qq musl-tools >/dev/null && \
	  rustup target add x86_64-unknown-linux-musl && \
	  CARGO_TARGET_DIR=/tmp/target cargo build --release --target x86_64-unknown-linux-musl && \
	  cp /tmp/target/x86_64-unknown-linux-musl/release/gw-agent /dist/ && chown $(shell id -u):$(shell id -g) /dist/gw-agent'
	@ls -la dist/gw-agent

clean: down
	-docker image rm cstam-api:local cstam-gateway:local
	rm -rf agent/target agent/.cargo-home dist
