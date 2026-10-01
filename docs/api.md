<!--
Reference of every control-plane HTTP endpoint, with a curl example and a real response.
Responsible for: what to send and what comes back. NOT responsible for: how it works inside
(docs/architecture.md). Serves all criteria. Interactive version: http://localhost:8000/docs
-->
# CSTAM API reference

Base URL locally: `http://localhost:8000`. If `API_KEY` is set, every **mutating** call (POST, DELETE)
needs `-H "X-API-Key: <key>"`. CORS is open (`*`). Swagger UI: `/docs`.

Sandbox JSON (returned by every sandbox endpoint):

```json
{"id": "86edf9c6-…", "team": "docteam", "hostname": "docteam.cstam.felcloud.tn",
 "url": "http://docteam.cstam.felcloud.tn/", "ip": "10.20.0.39", "status": "ACTIVE", "driver": "docker",
 "created_at": "2026-10-01T03:13:20Z", "expires_at": "2026-10-01T05:18:32Z", "provision_ms": 51, "error": null}
```

`status`: `PROVISIONING | WARM | ACTIVE | TEARING_DOWN | DELETED | FAILED`.

---

## Health

### `GET /healthz`
```bash
curl localhost:8000/healthz
```
```json
{"ok": true}
```

## Sandboxes — Criterion 1

### `POST /api/sandboxes` — create (or claim) a sandbox
Body: `{"team": "<name>", "ttl_seconds": <optional, default 7200, max 86400>}`.
Team name: `^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$`.

```bash
curl -X POST localhost:8000/api/sandboxes -H 'Content-Type: application/json' \
     -d '{"team":"docteam","ttl_seconds":3600}'
```

| Code | Meaning |
|---|---|
| **201** | Claimed from the warm pool — ready now (`provision_ms` ≈ 50 ms locally) |
| **202** | Warm pool empty — `PROVISIONING`, built in the background; poll `GET /api/sandboxes/{team}` |
| 409 | `{"detail":"team team1 already has a sandbox"}` |
| 422 | bad team name / TTL |
| 507 | IP pool exhausted |

### `GET /api/sandboxes` — list
Live sandboxes only; `?all=true` also returns `DELETED` / `FAILED` ones.
```bash
curl 'localhost:8000/api/sandboxes?all=true'
```
→ array of sandbox JSON.

### `GET /api/sandboxes/{team}` — one sandbox
The team's newest sandbox (live, or its last `DELETED`/`FAILED` one). 404 if the team never had one.
```bash
curl localhost:8000/api/sandboxes/docteam
```

### `DELETE /api/sandboxes/{team}` — teardown → 202
Route removed first, then the VM/container, then the IP goes back to the pool.
```bash
curl -X DELETE localhost:8000/api/sandboxes/docteam
```
```json
{"team": "docteam", "status": "DELETED", "ip": "10.20.0.39", "teardown_ms": 202, "…": "…"}
```

### `POST /api/sandboxes/{team}/renew`
```bash
curl -X POST localhost:8000/api/sandboxes/docteam/renew -H 'Content-Type: application/json' -d '{"ttl_seconds":7200}'
```
→ sandbox JSON with the new `expires_at` (the IP lease is extended too).

### `GET /api/warm-pool`
```bash
curl localhost:8000/api/warm-pool
```
```json
{"target": 3, "warm": 2, "booting": 1}
```

## IP pool — Criterion 3

### `GET /api/ipam` — summary + every lease that is not FREE
```bash
curl localhost:8000/api/ipam
```
```json
{"summary": {"pool_start": "10.20.0.21", "pool_end": "10.20.0.199", "size": 179,
             "counts": {"FREE": 171, "RESERVED": 2, "ALLOCATED": 6, "QUARANTINED": 0},
             "utilisation_percent": 4.5},
 "leases": [{"ip": "10.20.0.21", "state": "ALLOCATED", "team": "team1", "sandbox_id": "bb64aa9d-…",
             "leased_at": "…", "expires_at": "…", "quarantined_at": null}]}
```
`RESERVED` = held by a warm sandbox (no team yet); `ALLOCATED` = owned by a team until `expires_at`.

## Gateways — Criteria 2 + 4

### `GET /api/gateways`
Last status of each agent (refreshed every `MONITOR_INTERVAL_SECONDS`) and who holds the VIP.
```bash
curl localhost:8000/api/gateways
```
```json
{"master": "gw1",
 "gateways": {"gw1": {"name": "gw1", "vrrp_state": "MASTER", "reachable": true, "haproxy_running": true,
                      "config_version": 4, "previous_config_version": 1, "routes_version": 1790824676438,
                      "forced_backup": false, "last_config_apply": {"outcome": "applied", "…": "…"},
                      "last_routes_apply": {"outcome": "applied", "…": "…"}, "checked_at": "…"},
              "gw2": {"vrrp_state": "BACKUP", "…": "…"}}}
```
`vrrp_state`: `MASTER | BACKUP | FAULT | UNKNOWN | UNREACHABLE`.

### `POST /api/gateways/{name}/failover` — force this gateway to BACKUP
Its priority drops by 100, the peer takes the VIP within about a second.
```bash
curl -X POST localhost:8000/api/gateways/gw1/failover
```
```json
{"gateway": "gw1", "forced_backup": true, "outcome": "ok", "http_status": 200}
```

### `POST /api/gateways/{name}/failover/clear` — undo
```bash
curl -X POST localhost:8000/api/gateways/gw1/failover/clear
```
```json
{"gateway": "gw1", "forced_backup": false, "outcome": "ok", "http_status": 200}
```

### `GET /api/routes` — the host → IP map last pushed to the gateways
```bash
curl localhost:8000/api/routes
```
```json
{"routes_version": 1790824712578,
 "routes": {"team1.cstam.felcloud.tn": "10.20.0.21", "team2.cstam.felcloud.tn": "10.20.0.22"}}
```

## HAProxy config — Criterion 4

Every response of the three POSTs below has the same shape: per-gateway results **in the order they
were sent** (backup first):

```json
{"version": 7, "reason": "…", "status": "ACTIVE | REJECTED | ROLLED_BACK",
 "results": {"gw2": {"vrrp_state": "BACKUP", "outcome": "applied", "validate_ms": 32, "reload_ms": 1,
                     "probe_ms": 122, "rollback_ms": 0},
             "gw1": {"vrrp_state": "MASTER", "outcome": "applied", "…": "…"}}}
```

Per-gateway `outcome`: `applied | unchanged | rejected | rolled_back | skipped` (not sent because the
backup refused it) `| unreachable` (the monitor sends it when the gateway is back).

### `POST /api/config/apply` — render the template with a new version and roll it out
```bash
curl -X POST localhost:8000/api/config/apply
```

### `POST /api/config/rollback` — re-push an old ACTIVE config as a new version
```bash
curl -X POST localhost:8000/api/config/rollback -H 'Content-Type: application/json' -d '{"version":1}'
```
404 if that version does not exist or was never ACTIVE.

### `POST /api/config/inject-bad` — demo of the safety net
```bash
curl -X POST localhost:8000/api/config/inject-bad -H 'Content-Type: application/json' -d '{"mode":"syntax"}'
```
```json
{"version": 7, "status": "REJECTED", "results": {
  "gw2": {"vrrp_state": "BACKUP", "outcome": "rejected", "error": "[ALERT] … unknown keyword 'this' …"},
  "gw1": {"vrrp_state": "MASTER", "outcome": "skipped", "error": "not sent: gw2 refused it first"}}}
```
```bash
curl -X POST localhost:8000/api/config/inject-bad -H 'Content-Type: application/json' -d '{"mode":"semantic"}'
```
```json
{"version": 8, "status": "ROLLED_BACK", "results": {
  "gw2": {"vrrp_state": "BACKUP", "outcome": "rolled_back", "probe_ms": 1505, "rollback_ms": 126,
          "error": "probe never answered version 8"},
  "gw1": {"vrrp_state": "MASTER", "outcome": "skipped"}}}
```

### `GET /api/config/versions` — history, newest first
`?full=true` adds the full config text.
```bash
curl localhost:8000/api/config/versions
```
```json
[{"version": 4, "status": "ACTIVE", "reason": "startup", "checksum": "cac920fd…", "created_at": "…",
  "results": {"gw2": {"outcome": "applied", "…": "…"}, "gw1": {"outcome": "applied", "…": "…"}}}]
```

## Events — the platform's history

### `GET /api/events` — newest first
`?limit=100` (1–1000), `?kind=sandbox.deleted` to filter.
```bash
curl 'localhost:8000/api/events?limit=2'
```
```json
[{"id": 60, "ts": "…", "kind": "sandbox.renewed", "team": "docteam", "message": "docteam renewed until 05:18:32",
  "data": {"expires_at": "…"}},
 {"id": 59, "ts": "…", "kind": "sandbox.claimed", "team": "docteam", "message": "docteam got warm sandbox 10.20.0.39",
  "data": {"ip": "10.20.0.39", "provision_ms": 51}}]
```

Event kinds: `sandbox.claimed`, `sandbox.active`, `sandbox.failed`, `sandbox.deleted` (`reason`,
`teardown_ms`), `sandbox.renewed`, `warm_pool.refilled`, `ipam.conflict`, `ipam.unquarantined`,
`ipam.orphan_fixed`, `driver.orphan_removed`, `config.applied`, `config.rejected`, `config.rolled_back`,
`gateway.state_changed` (failovers), `gateway.resynced`.

---

## Gateway agent (internal, port 9000)

Called only by the API. Every endpoint except `/healthz` needs `X-Agent-Token`.

| Method | Path | Does |
|---|---|---|
| GET | `/healthz` | `ok` |
| GET | `/status` | VRRP state, HAProxy running, config/routes versions, last results |
| PUT | `/routes` | `{routes_version, routes: {host: ip}}` → Runtime API map update, no reload |
| POST | `/config` | `{version, config, checksum}` → validate → swap → reload → probe → (rollback) |
| POST | `/config/rollback` | back to the previous release |
| POST | `/failover`, `/failover/clear` | create / remove the force-backup file |

```bash
docker compose exec client curl -s -H 'X-Agent-Token: change-me' http://10.20.0.11:9000/status
```
