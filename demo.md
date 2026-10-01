<!--
Presenter's guide for the CSTAM demo (also the video script).
Responsible for: what to run, what to say, and what the audience should see, demo by demo.
NOT responsible for: installing the platform (README.md "Setup") or how it works inside (docs/architecture.md).
Serves all criteria.
-->
# CSTAM — Demo guide

Four short demos, one per scoring criterion. Each runs as a script that **checks its own results**:
a green ✔ means it really happened, and a red ✘ stops the demo.

| # | Script | Criterion | Duration |
|---|---|---|---|
| 1 | `scripts/1_provision.sh` | 1 — Sandbox provisioning (+ 3 IPAM view) | ~10 s |
| 2 | `scripts/2_failover.sh` | 2 — Dual HA gateways | ~30 s |
| 3 | `scripts/3_bad_config.sh` | 4 — Zero-downtime reload & auto-rollback | ~10 s |
| 4 | `scripts/4_reclaim.sh` | 3 — IPAM & reclamation (+ bonus 503 page) | ~40 s |

## Before you start

```bash
make up                  # platform running, http://localhost:8000/healthz answers
make ps                  # postgres, api, gw1, gw2, client + 3 warm sbx-* containers
```

For a recording, open three terminals:

1. **Demo**: `make demo` (all four) or `make demo-1` … `make demo-4` (one at a time).
2. **Events** (the platform's live history):
   ```bash
   watch -n1 "curl -s 'localhost:8000/api/events?limit=12' | jq -r '.[] | \"\(.ts[11:19])  \(.kind)  \(.message)\"'"
   ```
3. **Gateways** (who holds the VIP):
   ```bash
   watch -n1 "curl -s localhost:8000/api/gateways | jq -c '.gateways[] | {name, vrrp_state, config_version}'"
   ```

Optional: show Swagger at http://localhost:8000/docs.

> Every request to a sandbox goes through the **VIP 10.20.0.100** from the `client` container, a
> machine on the private network acting as a user. Host header = `team.cstam.felcloud.tn`, the same
> way DNS `*.cstam.felcloud.tn` would send it in the cloud.

---

## Demo 1 — Provisioning in milliseconds (Criterion 1)

```bash
make demo-1
```

**What happens**

1. Removes team1–team5 if an earlier run left them, so the demo can be repeated.
2. Shows the **warm pool**: 3 sandboxes are already booted with an IP, waiting for a team.
3. team1, team2 and team3 ask for a sandbox → **HTTP 201** in about **45 ms**. Each one takes a warm
   sandbox: it is relabelled, its welcome page is rewritten, and its route is added to both gateways.
4. team4 and team5 ask while the pool is empty → **HTTP 202 PROVISIONING**, built in the background,
   ACTIVE about 0.3 s later. Meanwhile the warm pool starts refilling.
5. Each team is curled through the VIP → **200** and a page saying `Sandbox of teamN`.
6. The IP pool: 179 IPs, each team with an `ALLOCATED` lease, warm sandboxes `RESERVED`.

**What to say**

- "One API call, and the team has its own machine at `team1.cstam.felcloud.tn` in under 50 ms,
  because the work was done in advance by the warm pool."
- "When the pool is empty we don't fail: we answer 202 and build it. The pool refills by itself."
- "Each sandbox has its own private IP from *our* IPAM, not Docker's or Neutron's."

**Expected output (shortened)**

```
✔ team1 → 10.20.0.21  team1.cstam.felcloud.tn  provision_ms=46
✔ team4 → HTTP 202, status PROVISIONING, ip 10.20.0.24
✔ team4 + team5 ACTIVE (334 ms, 347 ms)
✔ team1.cstam.felcloud.tn → 200 (served by gw1)
```

---

## Demo 2 — Kill the master gateway (Criterion 2)

```bash
make demo-2
```

**What happens**

1. gw1 is MASTER and holds the VIP; gw2 is BACKUP.
2. A loop in `client` curls team1 **every 50 ms for 15 s**.
3. At **3 s**: `docker compose stop gw1`. gw2's Keepalived takes the VIP and sends a gratuitous ARP.
4. At **10 s**: gw1 starts again. Once HAProxy passes 2 health checks, gw1 takes the VIP back
   because it has the higher priority (150 vs 100).
5. Report: total requests, failed requests, **longest failure gap**.
6. **Soft failover** through the API: `POST /api/gateways/gw1/failover` → gw2 becomes MASTER;
   `/failover/clear` → gw1 is back.

**What to say**

- "Two gateways, one IP. Keepalived sends a heartbeat every 200 ms. When it stops, the backup takes
  the IP."
- "We lost about one second: a single request that was in flight when gw1 died."
- "The controller noticed by itself. Look at the `gateway.state_changed` events. When gw1 came back
  without its routes, the monitor sent them again (`gateway.resynced`)."
- "Operators can also move the VIP on purpose, e.g. before maintenance."

**Expected output**

```
requests: 247   failed: 1   longest failure gap: 1122 ms
✔ outage below 1.5 s
  …  gw1: MASTER → UNREACHABLE
  …  gw2: BACKUP → MASTER
✔ gw2 took the VIP
✔ gw1 is MASTER again
```

---

## Demo 3 — Changes without downtime, broken configs caught (Criterion 4)

```bash
make demo-3
```

Traffic runs against team1 for the **whole** demo.

**What happens**

1. **Route change without reload**: team `flash` is created (→ 200) and deleted (→ 404). The
   HAProxy worker PID on the master **does not change**. Routes are updated live through HAProxy's
   Runtime API (`add map` / `del map`); HAProxy is never reloaded.
2. **Syntax-bad config** (`this is not haproxy {{{` appended): sent to the **backup first**. Its
   `haproxy -c` rejects it, so the rollout **stops** and the master shows `skipped`.
3. **Semantic-bad config** (valid syntax, but listens on :8099 instead of :80): passes validation,
   so the backup swaps and reloads. Its probe `GET /__gw_version` gets no answer, so it **rolls back
   automatically** in about 130 ms. The master again shows `skipped`.
4. Traffic report: **0 failed requests**.
5. Config history: `ACTIVE`, `REJECTED`, `ROLLED_BACK` versions.

**What to say**

- "Adding a team is a line in a map, changed in memory. HAProxy is never restarted for routes."
- "Real config changes go to the gateway that is *not* serving traffic first. If it fails there, the
  master never sees it."
- "Some mistakes pass validation. That's why the agent checks that the new config actually answers,
  and puts the old one back in about 130 ms if it doesn't."

**Expected output**

```
✔ HAProxy worker pid unchanged (39): no reload
  gw2 (BACKUP): rejected
  gw1 (MASTER): skipped
  gw2 (BACKUP): rolled_back  rollback_ms=137
✔ rolled back in 137 ms (< 1000)
requests: 51   failed: 0
```

---

## Demo 4 — Getting resources back (Criterion 3 + bonus)

```bash
make demo-4
```

**What happens**

1. `tempteam` gets a **15-second lease**, is reachable, and its IP is `ALLOCATED`.
2. Wait 25 s. The **reclaimer** (runs every 5 s) tears it down: status `DELETED`, reason
   `lease_expired`, IP back to `FREE`, URL now **404**.
3. `fastteam` is created and deleted → its IP is `FREE` **right after** the delete. Teardown order:
   route removed first, then the container, then the IP.
4. **Maintenance page**: team1's sandbox container is stopped → the URL answers **503** with the
   "Sandbox under maintenance" page instead of a timeout. The container is started again → 200.

**What to say**

- "Hackathon sandboxes are short-lived. Leases expire on their own and nothing leaks: not the VM,
  not the IP, not the route."
- "IPs are handed out atomically. 30 parallel requests in our tests got 30 different IPs. A freed IP
  goes to the back of the queue so a stale cache never points to a new team."
- "If a team's machine is down, its users get a clear page, not a hanging browser."

**Expected output**

```
✔ tempteam is DELETED (reason: lease_expired)
✔ IP 10.20.0.27 is FREE again
✔ tempteam.cstam.felcloud.tn → 404 (route removed)
✔ IP 10.20.0.28 FREE right after the delete
✔ team1.cstam.felcloud.tn → 503
  <h1>503 - Sandbox under maintenance</h1>
```

> The 503 takes about 4 s to appear: HAProxy tries to connect for 2 s, retries once, then serves the page.

---

## Doing it by hand (for questions from the jury)

```bash
# create / inspect / delete a sandbox
curl -X POST localhost:8000/api/sandboxes -H 'Content-Type: application/json' -d '{"team":"jury"}'
curl localhost:8000/api/sandboxes/jury
docker compose exec client curl -s -H 'Host: jury.cstam.felcloud.tn' http://10.20.0.100/
curl -X DELETE localhost:8000/api/sandboxes/jury

# what is where
curl localhost:8000/api/ipam | jq .summary
curl localhost:8000/api/routes
curl localhost:8000/api/gateways | jq '{master}'

# the safety net, one call each
curl -X POST localhost:8000/api/config/inject-bad -H 'Content-Type: application/json' -d '{"mode":"syntax"}'
curl -X POST localhost:8000/api/config/inject-bad -H 'Content-Type: application/json' -d '{"mode":"semantic"}'

# see the gateway's own view (agent API, needs the token)
docker compose exec client curl -s -H 'X-Agent-Token: change-me' http://10.20.0.11:9000/status | jq
```

(With the standalone `docker-compose` binary, replace `docker compose` with `docker-compose`.)

## Troubleshooting

| Symptom | Fix |
|---|---|
| Demo 1 waits forever for the warm pool | `make logs`, look at `api`. Usually Docker can't pull `busybox:stable`: `docker pull busybox:stable` |
| `team1 already has a sandbox` (409) in a manual test | `curl -X DELETE localhost:8000/api/sandboxes/team1` |
| Demo 2 gap above 1.5 s | Machine under heavy load. The demo prints a warning but does not fail. Run it again |
| Anything looks stuck | `make down && make up`: a clean start in a few seconds |
| Want to replay from zero | `make down && make up && make demo` |
