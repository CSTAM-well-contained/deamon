<!--
OpenStack deployment guide for the CSTAM cell (real VMs instead of local containers).
Responsible for: exact steps, quota check, and the two networking gotchas. NOT responsible for: local
Docker mode (see the root README). Serves all criteria in cloud mode.
-->
# Deploying CSTAM on OpenStack (FelCloud)

Local Docker mode imitates this exact layout. In the cloud each "container" becomes a VM:

| Local (docker-compose) | Cloud (Heat) |
|---|---|
| bridge `cstam-sandbox-net` 10.20.0.0/24 | Neutron network `cstam-sandbox-net` + subnet + router |
| `gw1`, `gw2` containers | `cstam-gw1`, `cstam-gw2` VMs (`cloud-init/gateway.yaml`) |
| VIP 10.20.0.100 on the bridge | VIP **port** 10.20.0.100 + **floating IP** |
| `api` + `postgres` containers | `cstam-control` VM running the same two containers |
| `sbx-*` busybox containers | `sbx-*` VMs created by `api/app/drivers/openstack_driver.py` |

## 0. Before you start

```bash
pip install python-openstackclient python-heatclient
export OS_CLOUD=felcloud                       # entry in ~/.config/openstack/clouds.yaml
openstack quota show                           # see "Quota check" below
openstack keypair list                         # you need one (KEY_NAME)
openstack network list --external              # name of the public network (EXTERNAL_NETWORK)
openstack image list | grep -Ei 'ubuntu|debian'
```

**Quota check** — the cell needs at least: 3 instances (gw1, gw2, control) + 1 per sandbox
(3 warm + one per team), 2 floating IPs (VIP + control), 1 router, 1 network, 2 + 1-per-sandbox
security groups, ~5 + 1-per-sandbox ports. With 20 teams: **26 instances, 26+ ports, 23 security groups**.

## 1. Deploy

```bash
make agent-static                              # builds dist/gw-agent (static musl binary)
KEY_NAME=mykey EXTERNAL_NETWORK=public AGENT_TOKEN=$(openssl rand -hex 16) \
  infra/openstack/deploy.sh
```

`deploy.sh` does, in order:

1. `openstack stack create --wait -t heat/cell.yaml cstam-cell` — network, subnet (Neutron's own pool
   is 10.20.0.200–250, outside our IPAM range .21–.199), router, security groups, VIP port + floating
   IP, gateway ports, gw1 + gw2.
2. `openstack stack create --wait -t heat/control.yaml cstam-control` — control VM 10.20.0.10 + its
   floating IP, Docker, `/opt/cstam/compose.yml` and `.env` (`DRIVER=openstack`).
3. Copies `dist/gw-agent` to both gateways through the control VM (`scp -J`) and starts `gw-agent`.
4. Copies `api/`, `cloud-init/` and your `clouds.yaml` to the control VM, `docker compose up -d --build`.
5. Prints the floating IP and the next steps.

## 2. DNS

```
*.cstam.felcloud.tn.   A   <floating IP printed by deploy.sh>
```

Until DNS exists: `curl -H 'Host: team1.cstam.felcloud.tn' http://<floating IP>/`.

## 3. Check

```bash
curl http://<control IP>:8000/api/gateways            # gw1 MASTER, gw2 BACKUP, same config version
curl -X POST http://<control IP>:8000/api/sandboxes -H 'Content-Type: application/json' -d '{"team":"team1"}'
curl http://team1.cstam.felcloud.tn/
```

A cold sandbox VM takes 30–90 s to boot; warm ones are claimed in well under a second.

## The two gotchas

### Gotcha 1 — allowed-address-pairs (otherwise the VIP is silently dead)

Neutron port security drops every packet whose IP is not one of the port's own fixed IPs. The gateway
holding the VIP answers for 10.20.0.100, which is **not** its fixed IP, so Neutron drops it. Fix (in
`heat/cell.yaml`): both gateway ports get

```yaml
allowed_address_pairs: [{ ip_address: 10.20.0.100 }]
```

The VIP itself is a separate Neutron port with no device: it only reserves the address so nobody
else gets it, and carries the floating IP. Neutron forwards the floating IP to whichever gateway
currently answers ARP for 10.20.0.100.

### Gotcha 2 — VRRP is IP protocol 112

Keepalived's adverts are not TCP or UDP; they are **IP protocol 112**. Security groups drop them unless
a rule allows that protocol. Without it, each gateway thinks the other is dead, **both** become MASTER
(split brain) and traffic flaps. Fix (in `heat/cell.yaml`):

```yaml
gateway_vrrp_rule: { protocol: 112, remote_group: cstam-gateway-sg, direction: ingress }
```

We also use **unicast** VRRP (`unicast_peer`), because many clouds block multicast.

### Smaller ones

- Ubuntu 22.04 ships HAProxy 2.4; `cloud-init/gateway.yaml` adds the `vbernat/haproxy-2.8` PPA
  (Debian 12 already has 2.6).
- The interface inside gateway VMs is usually `ens3` (Heat parameter `interface`).
- `net.ipv4.ip_nonlocal_bind=1` is set on gateways so HAProxy can start before the VIP arrives.
- Advert interval is 1 s in the cloud (`ADVERT_INT=1`, 0.2 s locally): failover takes ~3 s.

## 4. Destroy

```bash
infra/openstack/destroy.sh     # deletes sbx-* servers/ports/security groups, then both stacks
```
