<!--
How CSTAM works, as diagrams: components, provisioning, route update, config release, failover, IPAM.
Responsible for: the "why" and the flow between parts. NOT responsible for: endpoint details (docs/api.md)
or deployment steps (infra/openstack/README.md). Serves all criteria.
-->
# CSTAM architecture

One **cell** = a private network 10.20.0.0/24, two gateways sharing a VIP, sandboxes, and a control plane.
Locally every box below is a Docker container; on OpenStack every box is a VM.

## Components

```mermaid
flowchart LR
    user([Team / browser]) -- "team1.cstam.felcloud.tn" --> fip[Floating IP]
    fip --> vip{{"VIP 10.20.0.100<br/>(Keepalived VRRP)"}}
    vip --> gw1
    vip -.standby.-> gw2

    subgraph gw1 [gw1 — MASTER 10.20.0.11]
        h1[HAProxy<br/>hosts.map] --- k1[Keepalived] --- a1[gw-agent :9000]
    end
    subgraph gw2 [gw2 — BACKUP 10.20.0.12]
        h2[HAProxy<br/>hosts.map] --- k2[Keepalived] --- a2[gw-agent :9000]
    end
    k1 <-. "VRRP adverts (IP proto 112, unicast)" .-> k2

    h1 -- "Host → private IP" --> s1[sbx team1<br/>10.20.0.21]
    h1 --> s2[sbx team2<br/>10.20.0.22]
    h2 --> s1
    h2 --> s2

    subgraph control [Control plane 10.20.0.6]
        api[FastAPI<br/>sandboxes · ipam · gateways] --- db[(PostgreSQL)]
        jobs[jobs: warm pool · reclaimer · monitor] --- api
    end
    api -- "X-Agent-Token<br/>backup first" --> a2
    api --> a1
    api -- "DockerDriver / OpenStackDriver" --> s1
```

| Part | Code | Job |
|---|---|---|
| FastAPI | `api/app/` | the only brain: decides what exists, stores it in Postgres, tells drivers and gateways |
| Drivers | `api/app/drivers/` | create/delete a sandbox with a given IP (Docker container or Nova VM) |
| gw-agent | `agent/src/` | applies routes and configs on ONE gateway, safely, and reports its VRRP state |
| HAProxy | `api/app/gateways/haproxy.cfg.j2` | routes by Host header through `hosts.map` |
| Keepalived | `gateway/keepalived/` | moves the VIP to the healthy gateway |

## Provisioning — warm path (201) and cold path (202)

```mermaid
sequenceDiagram
    autonumber
    participant T as Team
    participant API as API (sandboxes/service.py)
    participant DB as Postgres
    participant D as Driver
    participant G as Gateways (backup, then master)
    T->>API: POST /api/sandboxes {team}
    API->>DB: lock 1 WARM row (FOR UPDATE SKIP LOCKED)
    alt warm sandbox available
        DB-->>API: sandbox (already booted, IP RESERVED)
        API->>DB: ACTIVE, team, hostname, expires_at · lease → ALLOCATED
        API->>D: assign(team, hostname) — welcome page / metadata
        API->>G: PUT /routes (full map) — no reload
        API-->>T: 201 {provision_ms ≈ 50}
    else pool empty
        API->>DB: insert PROVISIONING · allocate IP
        API-->>T: 202 {status: PROVISIONING}
        API->>D: create(id, ip)  (retry ≤ 3 with a new IP on conflict)
        API->>DB: ACTIVE · lease → ALLOCATED
        API->>G: PUT /routes
    end
    Note over API: warm_pool.py refills to WARM_POOL_SIZE every JOB_INTERVAL_SECONDS
```

## Route update — no reload (Criterion 4a)

```mermaid
sequenceDiagram
    participant RS as route_sync.push()
    participant B as gw-agent (BACKUP)
    participant M as gw-agent (MASTER)
    participant H as HAProxy Runtime API
    RS->>RS: desired = {hostname: ip} of every ACTIVE sandbox, routes_version = now(ms)
    RS->>B: PUT /routes {routes_version, routes}
    B->>B: validate hosts + IPs (inside 10.20.0.0/24) — invalid → rejected
    B->>H: add map / set map / del map (only the diff)
    B->>B: write hosts.map (tmp + rename) so a reload keeps it
    B->>H: show map — must equal desired, else restore old map → rolled_back
    B-->>RS: applied {added, removed, changed, apply_ms}
    RS->>M: same, only if the backup did not reject / roll back
```

## Config release with automatic rollback (Criterion 4b)

```mermaid
sequenceDiagram
    participant CS as config_sync
    participant A as gw-agent (BACKUP first)
    participant HP as HAProxy (master-worker)
    CS->>CS: render haproxy.cfg.j2 with version N · row PENDING
    CS->>A: POST /config {N, config, sha256}
    A->>A: write releases/vN.cfg (tmp + fsync + rename)
    A->>HP: haproxy -c -f vN.cfg
    alt invalid (syntax)
        A-->>CS: rejected (file deleted, symlink untouched) → MASTER NEVER RECEIVES IT
    else valid
        A->>A: current.cfg → vN.cfg (atomic symlink swap)
        A->>HP: reload (USR2: old workers finish their requests)
        loop every 25 ms, max 1.5 s
            A->>HP: GET /__gw_version
        end
        alt answers N
            A-->>CS: applied (active = N, previous = old)
        else no answer (semantic: e.g. binds :8099)
            A->>A: current.cfg → v(active)
            A->>HP: reload, probe until it answers the old version
            A-->>CS: rolled_back {rollback_ms ≈ 130}
        end
    end
    CS->>CS: ACTIVE / REJECTED / ROLLED_BACK + event
```

## Failover (Criterion 2)

```mermaid
sequenceDiagram
    participant C as Clients
    participant K1 as gw1 Keepalived (prio 150)
    participant K2 as gw2 Keepalived (prio 100)
    participant MON as API monitor
    K1->>K2: advert every 0.2 s (unicast, VRRP v3)
    Note over K1: gw1 dies / HAProxy fails 2 checks / force_backup file (prio −100)
    K2->>K2: no advert for 3 × 0.2 s (or priority 0 on clean stop)
    K2->>K2: MASTER — adds 10.20.0.100, sends gratuitous ARP
    C->>K2: traffic continues (measured gap ≈ 1 s with 1 s curl timeout)
    MON->>MON: /status poll sees gw1 UNREACHABLE, gw2 MASTER → event gateway.state_changed
    Note over K1: gw1 back: rise 2 checks, higher priority → preempts
    MON->>K1: routes_version behind → PUT /routes → event gateway.resynced
```

## IPAM lease states (Criterion 3)

```mermaid
stateDiagram-v2
    [*] --> FREE: seed_pool() at startup
    FREE --> RESERVED: allocate() — FOR UPDATE SKIP LOCKED
    RESERVED --> ALLOCATED: assign(team, ttl)
    RESERVED --> QUARANTINED: driver says IP already used
    ALLOCATED --> FREE: release() — teardown / lease expired
    RESERVED --> FREE: release() — failed build / orphan
    QUARANTINED --> FREE: reclaimer after 60 s
```

Allocation order is `released_at NULLS FIRST, ip_int`: never-used IPs first, then the one released the
longest time ago — a just-freed IP is not handed out again immediately (stale ARP/DNS caches).

Reclaimer (every 5 s, `ipam/reclaimer.py`): expired ACTIVE → teardown (`lease_expired`); PROVISIONING
> 10 min → FAILED; QUARANTINED > 60 s → FREE; leases of DELETED/FAILED sandboxes → FREE
(`ipam.orphan_fixed`); containers/VMs with no live row → deleted (`driver.orphan_removed`).

## Design choices worth knowing

- **Map + `set-dst` instead of one backend per team.** `server sandbox 0.0.0.0:80` means "connect to
  the destination chosen by `set-dst`", i.e. the IP looked up in `hosts.map`. Adding a team is a map
  entry, so no reload is ever needed for routes. Checked on HAProxy 2.6 (Debian 12), the fallback in
  the spec (one backend per team) was not needed.
- **Backup first.** A bad change is caught by the gateway that is *not* serving traffic.
- **Full map, versioned.** The API always sends the whole map; agents diff. A restarted agent reports
  `routes_version 0`, the monitor sees it is behind and re-sends.
- **Agent never trusts input.** Routes must point inside `ALLOWED_CIDR`; configs must match their sha256.

## OpenStack gotchas

1. **allowed-address-pairs** on both gateway ports for 10.20.0.100, or Neutron drops VIP traffic.
2. **IP protocol 112** allowed between gateways, or VRRP adverts are dropped → split brain.
3. Neutron's subnet allocation pool is 10.20.0.200–250 so it never hands out an IP our IPAM owns.
4. Ubuntu 22.04's HAProxy is 2.4 → the gateway cloud-init adds the 2.8 PPA.

Details: [infra/openstack/README.md](../infra/openstack/README.md).
