<!--
Project front page: pitch, quickstart, "Where is what" table, demo guide, project tree.
Responsible for: getting a new reader from zero to a running demo and to the right file.
NOT responsible for: deep explanations (docs/architecture.md) or endpoint details (docs/api.md).
-->
# CSTAM Sandbox Platform — FelCloud, Phase 1

**One API call gives a hackathon team its own isolated machine at `team.cstam.felcloud.tn` — in about
50 ms.** A warm pool keeps sandboxes booted in advance; our own IPAM hands out private IPs with short
leases and takes them back on expiry; two HAProxy gateways share one floating VIP through Keepalived, so
killing the active one costs about a second of traffic; and every routing change is applied **without
reloading HAProxy**, while full config changes are validated, probed and **rolled back automatically in
~130 ms** — always on the backup gateway first, so a broken config never reaches live traffic.

```
Users ──DNS *.cstam.felcloud.tn──▶ Floating IP ──▶ VIP 10.20.0.100
                                                   │ (Keepalived VRRP)
                                     ┌─────────────┴─────────────┐
                                gw1 (MASTER)                gw2 (BACKUP)
                           HAProxy + Keepalived + agent   (same)
                                     └─────────────┬─────────────┘
                                   Host header → hosts.map → private IP
                                                   ▼
                             sandbox VMs 10.20.0.21–199 (one per team)

Control VM: FastAPI (sandboxes, IPAM, reclaimer, warm pool, gateway client, monitor) + Postgres
           ──▶ OpenStack APIs (Nova, Neutron)   ──▶ gateway agents (:9000)
```

## Quickstart (local Docker mode)

Needs Docker with Compose v2 (`docker compose` or `docker-compose`), `make`, `curl`, `jq`.
No Rust or Python needed on the host.

```bash
cp .env.example .env
make up        # builds everything, waits for http://localhost:8000/healthz  (Swagger: /docs)
make demo      # runs the 4 demos below; stops at the first failure
make test      # pytest (in the api container) + cargo test (in a rust container)
make down      # removes everything, including sandbox containers
```

Try it by hand:

```bash
curl -X POST localhost:8000/api/sandboxes -H 'Content-Type: application/json' -d '{"team":"team1"}'
docker compose exec client curl -s -H 'Host: team1.cstam.felcloud.tn' http://10.20.0.100/
```

(All traffic tests run from the `client` container, which sits on the private network like a user
would, so this works on Linux, macOS and Windows.)

## Where is what

| Criterion | What to look at | Files |
|---|---|---|
| **1 — Sandbox provisioning** (15) | warm claim → 201, cold build → 202, teardown, renew | `api/app/sandboxes/service.py`, `api/app/sandboxes/warm_pool.py`, `api/app/sandboxes/routes.py` |
| | how a sandbox is physically made | `api/app/drivers/base.py`, `docker_driver.py`, `openstack_driver.py`, `infra/openstack/cloud-init/sandbox.yaml` |
| **2 — Dual HA gateways** (15) | VRRP config, health checks, forced failover | `gateway/keepalived/*`, `agent/src/vrrp.rs`, `gateway/entrypoint.sh` |
| | failover detection, resync of a restarted gateway | `api/app/gateways/monitor.py` |
| | VIP + floating IP + the two cloud gotchas | `infra/openstack/heat/cell.yaml`, `infra/openstack/README.md` |
| **3 — IPAM & reclamation** (10) | atomic allocation, leases, quarantine | `api/app/ipam/service.py` (+ `api/tests/test_ipam.py`) |
| | expiry, stuck builds, orphans | `api/app/ipam/reclaimer.py` |
| **4 — Zero-downtime reload & rollback** (10) | routes via Runtime API, no reload | `agent/src/routes.rs`, `api/app/gateways/route_sync.py` |
| | validate → swap → reload → probe → rollback | `agent/src/release.rs`, `agent/src/haproxy.rs` |
| | backup first, stop on failure | `api/app/gateways/client.py` (+ `api/tests/test_push_order.py`) |
| | the config template + versions | `api/app/gateways/haproxy.cfg.j2`, `api/app/gateways/config_sync.py` |
| **Bonus — maintenance page** | 503 when a sandbox is down | `gateway/haproxy/maintenance.http` |
| Event log (all) | every important state change | `api/app/events.py` → `GET /api/events` |

## Demo guide (`make demo`, or `make demo-1` … `demo-4`)

| Script | Shows | Expected |
|---|---|---|
| `scripts/1_provision.sh` | warm pool; team1–3 claim warm (**201**, ~50 ms); team4–5 cold (**202**); all reachable via the VIP; IP pool | 5 × HTTP 200 with the team's page |
| `scripts/2_failover.sh` | curl every 50 ms for 15 s; `docker compose stop gw1` at 3 s, start at 10 s; soft failover via API | longest gap ≈ 1.1 s, `gateway.state_changed` events |
| `scripts/3_bad_config.sh` | route add/remove (same HAProxy worker pid = no reload); syntax-bad → `rejected` on backup, master `skipped`; semantic-bad → `rolled_back` | **0 failed requests**, rollback ≈ 130 ms |
| `scripts/4_reclaim.sh` | 15 s lease expires → DELETED, IP FREE, 404; delete frees the IP at once; stopped sandbox → **503** maintenance page | all ✔ |

Measured on the reference laptop: warm claim 42–51 ms, cold build ~330 ms (Docker), failover gap
1121 ms (one in-flight request with a 1 s timeout), semantic rollback 126–139 ms.

## Cloud (OpenStack)

The same design on real VMs: `infra/openstack/` (Heat stacks, cloud-init, `deploy.sh`, `destroy.sh`).
Step by step, quotas and the two networking gotchas (allowed-address-pairs, VRRP protocol 112):
[infra/openstack/README.md](infra/openstack/README.md).

## More docs

- [docs/architecture.md](docs/architecture.md) — diagrams of every flow (provisioning, route update,
  config release + rollback, failover, IPAM states).
- [docs/api.md](docs/api.md) — every endpoint with a curl example and a real response.

## Project tree

```
.
├── api/              control plane (Python 3.12, FastAPI) — one folder per feature
│   ├── app/sandboxes/    Criterion 1: provisioning + warm pool
│   ├── app/ipam/         Criterion 3: IP pool + reclaimer
│   ├── app/gateways/     Criteria 2+4: agent client, routes, configs, monitor, HAProxy template
│   ├── app/drivers/      Docker (local) and OpenStack (cloud) sandbox drivers
│   └── tests/            pytest: IPAM concurrency, template, backup-first rollout
├── agent/            gw-agent (Rust): routes without reload, safe config releases, VRRP state
├── gateway/          gateway image: HAProxy + Keepalived + agent, bootstrap config, 503 page
├── infra/openstack/  Heat templates, cloud-init, deploy/destroy scripts
├── scripts/          the 4 demos (also the video script) + shared helpers
├── docs/             architecture diagrams and API reference
├── docker-compose.yml  local cell: postgres, api, gw1, gw2, client on 10.20.0.0/24
└── Makefile          up / down / test / demo / logs / agent-static
```

Settings: every variable is documented in [.env.example](.env.example). Secrets (`.env`,
`clouds.yaml`) are git-ignored.
