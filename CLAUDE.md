# CLAUDE.md — CSTAM Sandbox Platform (FelCloud, Phase 1)

> **For Claude Code.** Build this whole repository in one session.
> 1. Read this entire file first.
> 2. Build in the order of **§10 Build order**. Run every checkpoint and fix until it passes. Never skip one.
> 3. No TODOs, stubs, `pass`, or `unimplemented!()` in any code path.
> 4. Done = `make up && make demo` passes locally (Docker mode), and OpenStack mode is fully written.
> 5. **Readability is a requirement.** This code will be read by students who must understand every part.
>    See §2 rules — they are as important as the features.

---

## 1. What we are building

FelCloud hosts hackathon sandboxes. We build a platform that:

| # | Criterion (points) | What it means here |
|---|---|---|
| 1 | Sandbox provisioning (15) | `POST /api/sandboxes {team}` → the team gets an isolated VM (container locally) with a private IP, reachable at `team.cstam.felcloud.tn`. A **warm pool** of ready sandboxes makes this take seconds. |
| 2 | Dual HA gateways (15) | Two gateways (HAProxy + Keepalived/VRRP) share **one VIP** with a floating IP. If the master dies, the backup takes the VIP in about a second. |
| 3 | IPAM & reclamation (10) | Our own IP pool with short leases, conflict-free allocation, and immediate release on teardown or expiry. |
| 4 | Zero-downtime reload & auto-rollback (10) | Routes change with **no reload** (HAProxy map + Runtime API). Full config changes are **validated → reloaded → probed → rolled back in < 1 s** if broken. Backup gateway is always updated first. |

Bonus included: a **maintenance page (503)** when a sandbox is down.

### Architecture (one "cell")

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

No Kubernetes. Plain VMs on OpenStack; Docker containers imitate them locally.

---

## 2. Readability rules (mandatory)

1. **Every file starts with a header comment** (3–6 lines): what this file is responsible for, what it is
   NOT responsible for, and which scoring criterion it serves (e.g. `# Criterion 3 — IPAM`).
2. **One responsibility per file.** If a file does two things, split it. Max ~250 lines per file.
3. **Folders follow the features**, not technical layers (see §3). A reader looking for "how IPs are
   allocated" opens `api/app/ipam/`.
4. Every feature folder in the API has the same shape: `routes.py` (HTTP only, no logic) and `service.py`
   (the logic). HTTP handlers just validate input and call the service.
5. Plain names, no clever abstractions. Functions under ~40 lines. Comments explain **why**, not what.
6. Every important state change writes a row in `events` (one helper: `record_event`). The demo and the
   future dashboard read this table.
7. `README.md` contains a **"Where is what"** table mapping each criterion to its files.

---

## 3. Repository structure (create exactly this)

```
cstam-sandboxes/
├── README.md                     # pitch, quickstart, "where is what" table, demo guide
├── CLAUDE.md                     # this file
├── Makefile                      # up, down, test, demo, logs, agent-static
├── docker-compose.yml            # local mode: postgres, api, gw1, gw2, client
├── .env.example                  # every setting with a comment
├── .gitignore
│
├── api/                          # CONTROL PLANE — Python 3.12, FastAPI
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── app/
│   │   ├── main.py               # builds the app, mounts routers, starts background jobs
│   │   ├── settings.py           # all config from env (pydantic-settings)
│   │   ├── database.py           # DB engine, session, and the 4 tables
│   │   ├── events.py             # record_event() + GET /api/events
│   │   │
│   │   ├── sandboxes/            # CRITERION 1 — provisioning
│   │   │   ├── routes.py         # HTTP: create, list, get, delete, renew
│   │   │   ├── service.py        # claim_or_create(), teardown(), renew()
│   │   │   └── warm_pool.py      # background job: keep N ready sandboxes
│   │   │
│   │   ├── ipam/                 # CRITERION 3 — IP pool
│   │   │   ├── routes.py         # HTTP: pool summary + active leases
│   │   │   ├── service.py        # allocate(), assign(), release(), quarantine()
│   │   │   └── reclaimer.py      # background job: expired leases, orphans, quarantine
│   │   │
│   │   ├── gateways/             # CRITERIA 2 + 4 — controller side
│   │   │   ├── routes.py         # HTTP: gateway status, failover, config apply/rollback/inject-bad
│   │   │   ├── client.py         # talks to agents; ALWAYS backup first, then master
│   │   │   ├── route_sync.py     # builds {host: ip} from active sandboxes and pushes it
│   │   │   ├── config_sync.py    # renders haproxy.cfg, stores versions, pushes it
│   │   │   ├── monitor.py        # background job: poll agents, detect failover, fix drift
│   │   │   └── haproxy.cfg.j2    # the HAProxy config template
│   │   │
│   │   └── drivers/              # how a sandbox is physically created
│   │       ├── base.py           # the SandboxDriver interface
│   │       ├── docker_driver.py  # local: containers on a bridge network
│   │       └── openstack_driver.py  # real: Neutron port + security group + Nova VM
│   └── tests/
│       ├── conftest.py
│       ├── test_ipam.py          # 30 parallel allocations → 30 distinct IPs
│       ├── test_config_render.py # template is valid and deterministic
│       └── test_push_order.py    # backup gets updates before master; failure stops rollout
│
├── agent/                        # GATEWAY AGENT — Rust, runs on each gateway
│   ├── Cargo.toml
│   └── src/
│       ├── main.rs               # reads settings, starts the HTTP server
│       ├── settings.rs           # CLI flags / env vars
│       ├── http.rs               # endpoints → calls routes.rs / release.rs / vrrp.rs
│       ├── routes.rs             # CRITERION 4a: hosts.map updates via Runtime API (no reload)
│       ├── release.rs            # CRITERION 4b: validate → swap → reload → probe → rollback
│       ├── haproxy.rs            # the ONLY file that runs haproxy or talks to its socket (trait + mock)
│       ├── vrrp.rs               # CRITERION 2: read Keepalived state, force failover
│       └── error.rs
│
├── gateway/                      # gateway image (local) = what runs on gateway VMs (cloud)
│   ├── Dockerfile                # builds agent, installs haproxy + keepalived
│   ├── entrypoint.sh             # starts haproxy, keepalived, agent
│   ├── haproxy/
│   │   ├── bootstrap.cfg         # version-0 config used before the API pushes one
│   │   └── maintenance.http      # the 503 page (raw HTTP response file)
│   └── keepalived/
│       ├── keepalived.conf.tmpl
│       ├── check_haproxy.sh      # is haproxy alive and answering?
│       ├── check_not_forced.sh   # used to force a failover on demand
│       └── notify.sh             # writes MASTER/BACKUP/FAULT to a file the agent reads
│
├── infra/openstack/              # REAL CLOUD deployment
│   ├── README.md                 # step by step + gotchas (allowed-address-pairs, VRRP proto 112)
│   ├── heat/
│   │   ├── cell.yaml             # network, router, SGs, VIP port, floating IP, gw1, gw2
│   │   └── control.yaml          # control VM (api + postgres via docker compose)
│   ├── cloud-init/
│   │   ├── gateway.yaml          # installs haproxy, keepalived, agent (systemd)
│   │   └── sandbox.yaml          # tiny web page showing sandbox ip
│   ├── deploy.sh                 # openstack stack create (cell + control), prints floating IP
│   └── destroy.sh                # openstack stack delete
│
├── scripts/                      # DEMO (also the video script)
│   ├── lib.sh                    # api(), vip_curl(), wait_for(), colors
│   ├── demo.sh                   # runs the 4 demos in order
│   ├── 1_provision.sh
│   ├── 2_failover.sh
│   ├── 3_bad_config.sh
│   └── 4_reclaim.sh
│
└── docs/
    ├── architecture.md           # Mermaid diagrams (see §9)
    └── api.md                    # every endpoint with curl example
```

---

## 4. Tech stack (fixed)

| Part | Tech |
|---|---|
| API | Python 3.12, FastAPI, Uvicorn, SQLAlchemy 2 (async) + asyncpg, pydantic-settings, Jinja2, httpx, APScheduler 3 |
| Drivers | `docker` Python SDK (local), `openstacksdk` (cloud). Wrap blocking calls in `asyncio.to_thread`. |
| DB | PostgreSQL 16 |
| Agent | Rust stable 2021: tokio, axum 0.7, serde, reqwest (rustls), clap (derive+env), thiserror, anyhow, tracing, sha2, hex |
| Gateway | HAProxy ≥ 2.6 (master-worker), Keepalived (VRRP v3, unicast) |
| Infra | OpenStack Heat (HOT), cloud-init, `openstack` CLI |
| Local | Docker Compose v2, Makefile, `busybox:stable` as sandbox |
| Tests | pytest + pytest-asyncio, `cargo test` |

---

## 5. Local environment (Docker mode)

One bridge network imitates the OpenStack private network:

```yaml
networks:
  sandbox-net:
    name: cstam-sandbox-net
    ipam:
      config:
        - subnet: 10.20.0.0/24
          gateway: 10.20.0.1
          ip_range: 10.20.0.240/28    # docker's own auto-IPs stay out of our pool
```

| Service | IP | Notes |
|---|---|---|
| postgres | 10.20.0.5 | healthcheck `pg_isready` |
| api | 10.20.0.6 | port 8000 published, mounts `/var/run/docker.sock` |
| gw1 | 10.20.0.11 | `ROLE=MASTER PRIORITY=150 PEER_IP=10.20.0.12` |
| gw2 | 10.20.0.12 | `ROLE=BACKUP PRIORITY=100 PEER_IP=10.20.0.11` |
| **VIP** | **10.20.0.100** | held by the current master |
| client | 10.20.0.7 | curl container, `sleep infinity`, used by demo scripts |
| sandboxes | 10.20.0.21–199 | created by DockerDriver with IP from our IPAM |

Gateways: `cap_add: [NET_ADMIN, NET_RAW, NET_BROADCAST]`, `sysctls: net.ipv4.ip_nonlocal_bind=1`,
env `GW_NAME ROLE PRIORITY SELF_IP PEER_IP VIP IFACE=eth0 ADVERT_INT=0.2 AGENT_TOKEN`.
Demo scripts always curl from inside `client` so they work on any OS.

---

## 6. API (control plane)

### 6.1 Settings (`settings.py`, from env)

```
DATABASE_URL=postgresql+asyncpg://cstam:cstam@10.20.0.5:5432/cstam
DRIVER=docker                      # docker | openstack
BASE_DOMAIN=cstam.felcloud.tn
POOL_START=10.20.0.21
POOL_END=10.20.0.199
WARM_POOL_SIZE=3
DEFAULT_TTL_SECONDS=7200
MAX_TTL_SECONDS=86400
JOB_INTERVAL_SECONDS=5             # reclaimer + warm pool
MONITOR_INTERVAL_SECONDS=2
GATEWAYS=gw1=http://10.20.0.11:9000,gw2=http://10.20.0.12:9000
AGENT_TOKEN=change-me
API_KEY=                           # if set, mutating endpoints need X-API-Key
# openstack only
OS_CLOUD=felcloud
OS_SANDBOX_NETWORK=cstam-sandbox-net
OS_SANDBOX_SUBNET=cstam-sandbox-subnet
OS_SANDBOX_IMAGE=ubuntu-22.04
OS_SANDBOX_FLAVOR=m1.small
OS_GATEWAY_SECGROUP=cstam-gateway-sg
```

### 6.2 Tables (`database.py`, created at startup)

- **sandboxes**: `id uuid`, `team text null` (null while WARM), `hostname null`, `ip`, `status`
  (`PROVISIONING | WARM | ACTIVE | TEARING_DOWN | DELETED | FAILED`), `driver`, `external_id`,
  `extra jsonb`, `created_at`, `claimed_at`, `expires_at`, `deleted_at`, `provision_ms`, `error`.
  Partial unique index on `team` where status in (PROVISIONING, ACTIVE, TEARING_DOWN).
- **ip_leases**: `ip pk`, `ip_int bigint unique`, `state` (`FREE | RESERVED | ALLOCATED | QUARANTINED`),
  `sandbox_id null`, `team null`, `leased_at`, `expires_at`, `released_at`, `quarantined_at`.
- **config_versions**: `version serial`, `config text`, `checksum`, `reason`, `status`
  (`PENDING | ACTIVE | REJECTED | ROLLED_BACK`), `results jsonb`, `created_at`.
- **events**: `id bigserial`, `ts`, `kind`, `team null`, `message`, `data jsonb`.

Gateway status lives in memory (`app.state.gateways`), refreshed by the monitor.

### 6.3 Drivers (`drivers/base.py`)

```python
@dataclass
class SandboxHandle:
    external_id: str
    ip: str
    extra: dict            # e.g. {"port_id": ..., "secgroup_id": ...}

class SandboxDriver(Protocol):
    async def create(self, sandbox_id: str, ip: str) -> SandboxHandle: ...
    async def assign(self, handle: SandboxHandle, team: str, hostname: str) -> None: ...
    async def delete(self, external_id: str | None, extra: dict) -> None: ...   # idempotent
    async def list_managed(self) -> list[dict]: ...   # [{sandbox_id, external_id, ip}] for orphan cleanup
```

- **DockerDriver**: `create` runs `busybox:stable` named `sbx-{short_id}`, network `cstam-sandbox-net`,
  `--ip {ip}`, labels `cstam.managed=true cstam.sandbox={id}`, serving `/www/index.html` with
  `httpd -f -p 80 -h /www`. `assign` rewrites the page via `exec` to show team, hostname, ip.
- **OpenStackDriver**: `create` = security group `sbx-{id}` (TCP 80 only from `OS_GATEWAY_SECGROUP`) →
  Neutron port with `fixed_ips=[{subnet_id, ip_address: ip}]` (IP-already-allocated → raise
  `IpConflictError`) → `create_server(..., nics=[{"port-id"}], userdata=cloud-init/sandbox.yaml, wait=True)`.
  Clean up partial resources on failure. `assign` sets server metadata `team=...`. `delete` removes server,
  port, SG; each step tolerates NotFound.

### 6.4 IPAM (`ipam/service.py`) — Criterion 3

- Pool seeded at startup (insert missing IPs from POOL_START..POOL_END).
- `allocate(sandbox_id)` → one atomic query, safe under concurrency:
  `SELECT ip FROM ip_leases WHERE state='FREE' ORDER BY released_at NULLS FIRST, ip_int LIMIT 1 FOR UPDATE SKIP LOCKED`
  → `RESERVED`. Pool empty → `PoolExhaustedError` → HTTP 507.
- `assign(ip, team, ttl)` → `ALLOCATED` with `expires_at`. `release(ip)` → `FREE`, `released_at=now`.
  `quarantine(ip)` → `QUARANTINED` (driver reported a conflict).
- `summary()` → counts per state, pool size, utilisation %.

`ipam/reclaimer.py` (every JOB_INTERVAL_SECONDS, never crashes the scheduler):
1. ACTIVE sandboxes past `expires_at` → `teardown(reason="lease_expired")`.
2. PROVISIONING for > 10 min → FAILED + cleanup.
3. QUARANTINED for > 60 s → FREE.
4. Orphans: leases whose sandbox is DELETED/FAILED → release (`ipam.orphan_fixed`); driver resources with no
   live sandbox row → delete (`driver.orphan_removed`).

### 6.5 Sandboxes (`sandboxes/service.py`) — Criterion 1

`claim_or_create(team, ttl)`:
1. Validate team `^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$`; active sandbox for team exists → 409.
2. **Warm path:** lock one WARM sandbox (`FOR UPDATE SKIP LOCKED`) → set team, hostname
   `{team}.{BASE_DOMAIN}`, ACTIVE, `expires_at`; `ipam.assign`; `driver.assign`; `route_sync.push()`.
   Return **201** with `provision_ms` (target: < 3 s). Event `sandbox.claimed`.
3. **Cold path** (pool empty): insert PROVISIONING row, return **202**; background task does
   `allocate → driver.create (retry ≤3 on IpConflictError with quarantine) → assign → route_sync.push()`.
   Event `sandbox.active`.
4. Any failure → driver.delete (best effort), ipam.release, FAILED with `error`, event `sandbox.failed`.

`teardown(team, reason)` (idempotent): TEARING_DOWN → **remove route first** (`route_sync.push()`) →
`driver.delete` → `ipam.release` → DELETED. Event `sandbox.deleted` with `reason`, `teardown_ms`.

`renew(team, ttl)` extends sandbox and lease `expires_at`.

`warm_pool.py` (every JOB_INTERVAL_SECONDS): while WARM + PROVISIONING-warm < WARM_POOL_SIZE → create
one (`allocate → driver.create → status WARM`). Event `warm_pool.refilled`.

### 6.6 Gateways (`gateways/`) — Criteria 2 + 4, controller side

**Two kinds of change, two paths:**

| Change | Path | Reload? |
|---|---|---|
| Add/remove a team route | `route_sync.push()` → agent `PUT /routes` → HAProxy Runtime API map update | **No** |
| Change the HAProxy config itself | `config_sync.apply()` → agent `POST /config` → validate/reload/probe/rollback | Yes, zero-downtime (master-worker) |

`client.py` — **rollout order**: get each agent's VRRP state, send to **BACKUP(s) first**, then MASTER.
If the backup returns `rejected` or `rolled_back`, **stop** — never send it to the master. Unreachable
gateways are recorded and fixed later by the monitor. Header `X-Agent-Token` on every call, timeout 5 s.

`route_sync.push()` — one `asyncio.Lock`. Desired state = `{hostname: ip}` for all ACTIVE sandboxes. Always
send the full map (agents compute the diff), with an increasing `routes_version`.

`config_sync.py` — one `asyncio.Lock`.
- `apply(reason)`: render `haproxy.cfg.j2` with next version → save row PENDING → push via client →
  status ACTIVE / REJECTED / ROLLED_BACK + per-gateway results → event `config.applied|rejected|rolled_back`.
- `rollback(version)`: re-push an old ACTIVE config as a new version.
- `inject_bad(mode)` (demo): `syntax` = append `this is not haproxy {{{` → expect `rejected`, traffic
  untouched. `semantic` = valid config but frontend binds `:8099` → passes validation, probe fails, agent
  rolls back → expect `rolled_back` with `rollback_ms < 1000`.

`monitor.py` (every MONITOR_INTERVAL_SECONDS): `GET /status` on each agent → update `app.state.gateways`;
VRRP state changed → event `gateway.state_changed` (this records failovers); agent's config version or
routes version behind → re-push (event `gateway.resynced`). At startup: push config + routes once agents
answer (retry up to 60 s).

### 6.7 HAProxy template (`gateways/haproxy.cfg.j2`)

Routing is driven by a **map file**, so adding a team never needs a reload:

```
global
    master-worker
    pidfile /run/haproxy.pid
    stats socket /run/haproxy.sock mode 600 level admin expose-fd listeners
    log stdout format raw local0
    # config version {{ version }}

defaults
    mode http
    log global
    option httplog
    timeout connect 2s
    timeout client  30s
    timeout server  30s
    retries 1

frontend fe_http
    bind :{{ bind_port | default(80) }}
    http-request return status 200 content-type text/plain string "ok" if { path /__gw_health }
    http-request return status 200 content-type text/plain string "{{ version }}" if { path /__gw_version }
    http-request set-var(txn.dst) req.hdr(host),field(1,:),lower,map(/etc/haproxy/hosts.map)
    http-request return status 404 content-type text/html string "<h1>No sandbox for this host</h1>" unless { var(txn.dst) -m found }
    http-request set-dst var(txn.dst)
    http-response set-header X-Served-By "${GW_NAME}"
    http-response set-header X-Config-Version "{{ version }}"
    default_backend be_sandboxes

backend be_sandboxes
    # 0.0.0.0 = "connect to the address chosen by set-dst" (the team's private IP)
    server sandbox 0.0.0.0:80
    errorfile 503 /etc/haproxy/maintenance.http
```

`hosts.map` format: one line per route, `team5.cstam.felcloud.tn 10.20.0.23`.
If sandbox is down → connection fails → HAProxy returns the 503 maintenance page.
Checkpoint 3 must prove this works; if `set-dst` with `0.0.0.0` does not behave on the installed HAProxy,
fall back to rendering one backend per team (keep the map for `use_backend`) and document it.

### 6.8 HTTP endpoints

| Method | Path | Does |
|---|---|---|
| GET | `/healthz` | `{"ok": true}` |
| POST | `/api/sandboxes` | `{team, ttl_seconds?}` → 201 (warm) or 202 (cold) |
| GET | `/api/sandboxes` | list (`?all=true` includes DELETED/FAILED) |
| GET | `/api/sandboxes/{team}` | one sandbox |
| DELETE | `/api/sandboxes/{team}` | teardown → 202 |
| POST | `/api/sandboxes/{team}/renew` | `{ttl_seconds}` |
| GET | `/api/ipam` | summary + non-FREE leases |
| GET | `/api/warm-pool` | size, target |
| GET | `/api/gateways` | both gateways + `master` name |
| POST | `/api/gateways/{name}/failover` | force this gateway to BACKUP |
| POST | `/api/gateways/{name}/failover/clear` | undo |
| GET | `/api/routes` | current host → ip map + routes_version |
| GET | `/api/config/versions` | history (`?full=true` includes config text) |
| POST | `/api/config/apply` | re-render & push |
| POST | `/api/config/rollback` | `{version}` |
| POST | `/api/config/inject-bad` | `{mode: "syntax" \| "semantic"}` → agent results |
| GET | `/api/events` | `?limit=100&kind=` newest first |

Sandbox JSON: `{id, team, hostname, url, ip, status, driver, created_at, expires_at, provision_ms, error}`.
CORS `*`. `/docs` (Swagger) must work.

---

## 7. Gateway agent (Rust) — `agent/`

Binary `gw-agent`. Settings (flag + env): `--listen 0.0.0.0:9000`, `--token` (required), `--gw-name`,
`--haproxy-bin haproxy`, `--socket /run/haproxy.sock`, `--map-file /etc/haproxy/hosts.map`,
`--releases-dir /etc/haproxy/releases`, `--current-link /etc/haproxy/current.cfg`,
`--reload-cmd "kill -USR2 $(cat /run/haproxy.pid)"`, `--probe-url http://127.0.0.1/__gw_version`,
`--probe-timeout-ms 1500`, `--probe-interval-ms 25`, `--vrrp-state-file /run/gw/vrrp_state`,
`--force-backup-file /run/gw/force_backup`, `--allowed-cidr 10.20.0.0/24`, `--keep-releases 20`.

### 7.1 Endpoints (all but `/healthz` need `X-Agent-Token`)

| Method | Path | Does |
|---|---|---|
| GET | `/healthz` | `ok` |
| GET | `/status` | `{gw_name, vrrp_state, haproxy_running, config_version, routes_version, forced_backup, last_config_apply, last_routes_apply}` |
| PUT | `/routes` | `{routes_version, routes: {host: ip}}` → see 7.2 |
| POST | `/config` | `{version, config, checksum}` → see 7.3 |
| POST | `/config/rollback` | go back to previous release |
| POST | `/failover` / `/failover/clear` | create / remove force-backup file |

### 7.2 `routes.rs` — route updates without reload

1. **Validate** every entry: hostname matches `^[a-z0-9-]+\.[a-z0-9.-]+$`, IP is IPv4 inside `allowed_cidr`.
   Any invalid entry → `rejected` (422), nothing changed.
2. Same `routes_version` and same content → `unchanged`.
3. Diff against current map → for each change send over the socket: `add map`, `set map`, `del map`.
4. Write the full map to `hosts.map` (temp file + rename) so it survives a reload/restart.
5. Verify with `show map`; mismatch or socket error → restore the previous map (file + socket) →
   `rolled_back` (409). Return `{outcome, added, removed, changed, apply_ms}`.

### 7.3 `release.rs` — full config pipeline (one apply at a time, `tokio::sync::Mutex`)

```
0. sha256(config) != checksum → 400.  Same version + checksum as active → "unchanged".
1. WRITE    releases/v{N}.cfg  (tmp + fsync + rename)
2. VALIDATE haproxy -c -f v{N}.cfg       fail → delete file → "rejected" (422) with stderr
3. SWAP     symlink current.cfg → v{N}.cfg atomically (symlink tmp + rename)
4. RELOAD   run reload_cmd (master-worker: old workers finish their requests → no dropped traffic)
5. PROBE    poll probe_url every 25 ms until body == N, max probe_timeout_ms
     ok   → previous = active, active = N, save state.json → "applied" (200)
     fail → ROLLBACK: swap back to v{active}, reload, probe until body == active
            → "rolled_back" (409) with rollback_ms
6. PRUNE    keep last keep_releases (never active/previous)
Every step timed → validate_ms, reload_ms, probe_ms, rollback_ms in the response.
```

State `{active, previous}` in `releases/state.json` (tmp + rename), loaded at startup.

### 7.4 `haproxy.rs`

The only place that runs commands or uses the socket:

```rust
pub trait Haproxy: Send + Sync {
    async fn validate(&self, path: &Path) -> Result<(), String>;
    async fn reload(&self) -> Result<(), String>;
    async fn probe_version(&self) -> Option<String>;
    async fn is_running(&self) -> bool;
    async fn socket(&self, command: &str) -> Result<String, String>;  // Runtime API
}
```

Real implementation + `MockHaproxy` for tests. **Required tests:** config applied; config rejected leaves
symlink untouched; probe failure rolls back and symlink points to previous; unchanged; prune keeps
active/previous; routes: invalid IP rejected, diff produces correct socket commands, socket failure restores map.

Rules: no `unwrap()`/`expect()` outside `main` and tests; errors with `thiserror`; logs with `tracing`.

### 7.5 `vrrp.rs`

Read `vrrp_state_file` (MASTER/BACKUP/FAULT, missing → UNKNOWN). Failover = create `force_backup_file`
(Keepalived's `check_not_forced.sh` fails → priority −100 → VIP moves to peer).

---

## 8. Gateway image & OpenStack

### 8.1 `gateway/`

- **Dockerfile**: stage 1 `rust:1-bookworm` builds `gw-agent`; stage 2 `debian:bookworm-slim` + `haproxy
  keepalived curl iproute2 procps tini`.
- **entrypoint.sh**: create `/run/gw`, `/etc/haproxy/releases`; write `BACKUP` to vrrp_state; if no
  `current.cfg`, install `bootstrap.cfg` as `v0.cfg` (same template rendered with version 0 and an empty
  map); touch `hosts.map`; render keepalived.conf from env; start `haproxy -W -db -f current.cfg -p
  /run/haproxy.pid &`, `keepalived --dont-fork --log-console &`, then `exec gw-agent`.
- **keepalived.conf.tmpl**: `vrrp_version 3`, `vrrp_instance` with `state ${ROLE}`, `priority ${PRIORITY}`,
  `advert_int ${ADVERT_INT}`, `unicast_src_ip ${SELF_IP}`, `unicast_peer { ${PEER_IP} }`,
  `virtual_ipaddress { ${VIP}/24 dev ${IFACE} }`, track scripts `chk_haproxy` (interval 1, fall 2, rise 2)
  and `chk_not_forced` (weight −100), `notify /usr/local/bin/notify.sh`.
- `check_haproxy.sh`: haproxy pid alive AND `curl -fsS -m 0.5 http://127.0.0.1/__gw_health`.
- `check_not_forced.sh`: `[ ! -f /run/gw/force_backup ]`.  `notify.sh`: `echo "$3" > /run/gw/vrrp_state`.

### 8.2 `infra/openstack/`

- **heat/cell.yaml** parameters: `external_network`, `image`, `flavor`, `key_name`, `admin_cidr`,
  `agent_token`. Resources:
  - network `cstam-sandbox-net` + subnet 10.20.0.0/24, **allocation pool 10.20.0.200–250** (Neutron's own
    auto-IPs stay outside our IPAM range .21–.199) + router to `external_network`.
  - SG `cstam-gateway-sg`: TCP 80/443 from anywhere, 22 from `admin_cidr`, 9000 from control SG,
    **IP protocol 112 (VRRP) from itself**, ICMP. SG `cstam-control-sg`: 8000 + 22 from `admin_cidr`.
  - **VIP port** 10.20.0.100 (no device) + **floating IP on the VIP port**.
  - Ports gw1 (10.20.0.11) and gw2 (10.20.0.12) with **`allowed_address_pairs: [{ip_address: 10.20.0.100}]`**
    (without this Neutron drops VIP traffic).
  - Servers gw1/gw2 with `cloud-init/gateway.yaml` (ROLE/PRIORITY/SELF_IP/PEER_IP filled, `ADVERT_INT=1`).
  - Output: `floating_ip`.
- **heat/control.yaml**: control VM on the same network with docker + compose, runs `api` + `postgres`
  with `DRIVER=openstack`.
- **cloud-init/gateway.yaml**: install haproxy + keepalived, same scripts/config as the container,
  `ip_nonlocal_bind=1`, haproxy systemd with `-f /etc/haproxy/current.cfg`, agent as systemd service with
  `RELOAD_CMD="systemctl reload haproxy"`. **cloud-init/sandbox.yaml**: page with hostname + ip, served on :80.
- **deploy.sh / destroy.sh**: `set -euo pipefail`, `openstack stack create --wait` / `stack delete --wait`,
  print floating IP and next steps (DNS `*.cstam.felcloud.tn A <FIP>`, `make agent-static` to build a musl binary).
- **README.md**: exact steps + the two gotchas (allowed-address-pairs, VRRP protocol 112) + quota check.

---

## 9. Demo scripts, Makefile, docs

`scripts/lib.sh`: `API=${API:-http://localhost:8000}`; `api METHOD PATH [JSON]` (curl + jq);
`vip_curl HOST [PATH]` → runs inside `client`: prints `status served_by`; `wait_for TEAM STATUS TIMEOUT`;
`step / ok / fail` colored printers.

1. **1_provision.sh** — show warm pool; create team1..team3 (warm, print `provision_ms`); create team4..team5
   (cold if pool empty); curl each via VIP → 200 + team name; show `/api/ipam`.
2. **2_failover.sh** — background loop in `client` hitting team1 every 50 ms for 15 s; at 3 s
   `docker compose stop gw1`; at 10 s start it again. Print: total, failed, **longest failure gap in ms**,
   gateway before/after. Then soft failover via `/api/gateways/gw1/failover` and `/clear`.
3. **3_bad_config.sh** — with a curl loop running: add + remove a team (show 0 failed, no reload);
   inject `syntax` → `rejected`, master untouched (backup-first); inject `semantic` → `rolled_back` +
   `rollback_ms`; print `/api/config/versions`.
4. **4_reclaim.sh** — create `tempteam` with `ttl_seconds=15`; wait 25 s; show DELETED (lease_expired), IP
   FREE, curl → 404; create + delete `fastteam` → IP free immediately; `docker stop` a sandbox → curl → 503
   maintenance page.

**Makefile**: `up` (build, compose up, wait `/healthz`), `down` (also removes `sbx-*`), `logs`, `ps`, `test`
(pytest in api container + `cargo test`), `demo`, `demo-1..4`, `agent-static`
(`x86_64-unknown-linux-musl`), `clean`.

**docs/architecture.md** (Mermaid): component diagram; sequence for provisioning (warm + cold); sequence for
route update (no reload); sequence for config apply with rollback; sequence for failover; IPAM state
diagram; OpenStack gotchas. **docs/api.md**: every endpoint with curl + response.

**README.md**: 1-paragraph pitch, diagram, quickstart (`cp .env.example .env && make up && make demo`),
**Where is what** table (criterion → files), OpenStack pointer, project tree with one line per folder.

---

## 10. Build order (run each checkpoint; fix until green)

1. **Scaffold** tree, `.gitignore`, `.env.example`, Makefile, `git init`.
2. **Agent** (§7) with mock tests. ✅ `cd agent && cargo build && cargo test`
3. **Gateway image + compose (gw1, gw2, client)**. ✅ `curl 10.20.0.100/__gw_version` → `0` from client;
   gw1 is MASTER; `docker compose stop gw1` → VIP still answers via gw2. Manually `PUT /routes` with a busybox
   container IP → curl with Host header → 200; stop that container → 503 page. `POST /config` valid → applied,
   garbage → rejected.
4. **API base + IPAM + tests**. ✅ `pytest tests/test_ipam.py` (30 parallel allocations → 30 distinct IPs)
5. **Config template + config_sync + client (backup-first)**. ✅ `pytest tests/test_config_render.py
   tests/test_push_order.py`; `POST /api/config/apply` → both `applied`.
6. **DockerDriver + sandboxes service + route_sync + warm pool**. ✅ warm pool fills to 3; `POST
   /api/sandboxes {"team":"team1"}` → 201 in < 3 s; curl VIP with `Host: team1.cstam.felcloud.tn` → 200.
7. **Reclaimer + monitor + events**. ✅ TTL 15 s sandbox disappears and IP is FREE; `/api/gateways` shows
   master; restarting gw1 gets resynced (event `gateway.resynced`).
8. **inject-bad + rollback**. ✅ semantic → `rolled_back`, `rollback_ms < 1000`; syntax → `rejected` on
   backup and master never received it.
9. **Demo scripts**. ✅ `make down && make up && make demo` passes end to end.
10. **OpenStack**: driver + Heat + cloud-init + deploy/destroy. ✅ `python -m py_compile`, `bash -n`,
    `shellcheck` if present, `openstack orchestration template validate` if CLI available.
11. **Docs + README**. ✅ `make test` green. Check every file has its header comment (§2).

## 11. Definition of done

- [ ] `make up` from clean clone healthy in < 3 min (after image pulls).
- [ ] Warm claim returns in < 3 s; 10 concurrent creates → 10 ACTIVE, 10 distinct IPs, all reachable.
- [ ] Killing the master: traffic continues; measured gap printed (target < 1.5 s locally); event
      `gateway.state_changed` recorded.
- [ ] Restarted gateway auto-resyncs config and routes.
- [ ] Route add/remove: 0 failed requests, no HAProxy reload.
- [ ] Syntax-bad config → `rejected` on backup, master untouched, 0 failed requests.
- [ ] Semantic-bad config → `rolled_back`, `rollback_ms < 1000`.
- [ ] Lease expiry and manual delete free the IP and remove the route.
- [ ] Stopped sandbox → 503 maintenance page.
- [ ] `cargo test` and `pytest` pass; every file has its header comment; README "Where is what" table exists.
- [ ] Secrets never committed (`.env`, `clouds.yaml` in `.gitignore`).