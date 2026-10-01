<!--
Step-by-step deployment of the CSTAM cell on FelCloud (OpenStack, region North-Africa).
Responsible for: the FelCloud-specific path from "credits activated" to "team1 answers on the floating IP".
NOT responsible for: generic OpenStack details and the two VIP gotchas (see README.md in this folder).
Serves all criteria in cloud mode.
-->
# Deploying on FelCloud

What you end up with (one "cell"):

```
Internet ─▶ floating IP ─▶ VIP port 10.20.0.100 ─▶ cstam-gw1 (MASTER) / cstam-gw2 (BACKUP)
                                                       │  HAProxy + Keepalived + gw-agent
                                                       ▼
                                   sbx-* VMs on cstam-sandbox-net 10.20.0.0/24
cstam-control VM (floating IP): API + Postgres (docker compose, DRIVER=openstack) ─▶ FelCloud APIs
```

Billing is **per second on your credits**: destroy everything when you are not testing (step 8).

---

## 0. Prerequisites (once)

- Credits activated on the team leader's FelCloud account (`Guide_Compte_Credits_CSTAM.pdf`).
- On your laptop: Python 3, Docker (only to build the agent), `ssh`, `openssl`.

```bash
pip install python-openstackclient python-heatclient
```

## 1. Credentials

Put the `clouds.yaml` from Skyline (or the shared Drive) at `~/.config/openstack/clouds.yaml`.
Its entry is called **`felcloud_demo`**, so use that name everywhere:

```bash
mkdir -p ~/.config/openstack && cp clouds.yaml ~/.config/openstack/clouds.yaml && chmod 600 ~/.config/openstack/clouds.yaml
export OS_CLOUD=felcloud_demo
openstack token issue          # must print a token
```

> The file holds an application-credential **secret**. Never commit it (`clouds.yaml` is git-ignored);
> `deploy.sh` copies it only to the control VM, which needs it to create sandbox VMs.

## 2. Preflight (read-only, 30 s)

```bash
infra/openstack/preflight.sh
```

It checks login, **Heat**, the **allowed-address-pairs** extension (without it the VIP cannot work),
and lists the names you need next: external network, Ubuntu images, flavors, keypairs, quotas.
If it reports a blocker, email `contact@felcloud.tn` with the subject `[IEEE CSTAM 3.0] …` before going on.

## 3. Keypair (if preflight found none)

```bash
openstack keypair create --public-key ~/.ssh/id_ed25519.pub cstam
```

## 4. Fill in your values

From the preflight output:

```bash
export OS_CLOUD=felcloud_demo
export KEY_NAME=cstam
export EXTERNAL_NETWORK=<external network name>      # e.g. the one listed under "External network"
export IMAGE=<Ubuntu 22.04 or 24.04 image name>      # gateways + control
export FLAVOR=<2 vCPU / 4 GB flavor>                  # gateways + control
export SANDBOX_IMAGE=$IMAGE
export SANDBOX_FLAVOR=<smallest flavor, 1 vCPU / 1 GB is enough>
export ADMIN_CIDR=$(curl -s https://ifconfig.me)/32   # only YOUR IP can SSH / reach the API
export AGENT_TOKEN=$(openssl rand -hex 16)
echo "$AGENT_TOKEN" > .agent-token                    # keep it, you need it to talk to the agents
```

If the image is Ubuntu 24.04, the HAProxy PPA step in `cloud-init/gateway.yaml` is unnecessary but harmless.

## 5. Deploy (≈ 10 min)

```bash
make agent-static                 # builds dist/gw-agent (static binary for the gateway VMs)
infra/openstack/deploy.sh
```

It creates the `cstam-cell` stack (network, router, security groups, VIP port + floating IP, gw1, gw2),
then the `cstam-control` stack, copies the agent to both gateways, and starts the API.
At the end it prints the **floating IP** (the VIP) and the **API URL**.

If a stack fails: `openstack stack event list cstam-cell --nested-depth 2` shows which resource and why
(usually a wrong image/flavor/network name or a quota).

## 6. DNS

The jury's URLs are `teamX.cstam.felcloud.tn`, and `felcloud.tn` belongs to FelCloud. Pick one:

| Option | How |
|---|---|
| **A. Ask FelCloud** (best for the final) | Email them: `*.cstam.felcloud.tn  A  <floating IP>` |
| **B. Host header** (works right now) | `curl -H 'Host: team1.cstam.felcloud.tn' http://<floating IP>/` |
| **C. sslip.io** (real browser URLs, no DNS work) | On the control VM set `BASE_DOMAIN=<floating IP>.sslip.io` in `/opt/cstam/.env`, then `sudo docker compose -f compose.yml up -d`. URLs become `team1.<floating IP>.sslip.io` |

## 7. Check it works

```bash
API=http://<control IP>:8000
curl $API/api/gateways | jq '{master, gateways: [.gateways[] | {name, vrrp_state, config_version}]}'
curl -X POST $API/api/sandboxes -H 'Content-Type: application/json' -d '{"team":"team1"}'
curl $API/api/sandboxes/team1                        # wait for ACTIVE (cold VM: 30–90 s)
curl -H 'Host: team1.cstam.felcloud.tn' http://<floating IP>/
```

Failover test on real VMs (expect ~3 s with `ADVERT_INT=1`):

```bash
while true; do curl -s -o /dev/null -m 1 -w '%{http_code} ' -H 'Host: team1.cstam.felcloud.tn' http://<floating IP>/; sleep 0.2; done
# in another terminal:
openstack server stop cstam-gw1      # traffic continues via gw2
openstack server start cstam-gw1     # gw1 takes the VIP back
```

**Save credits:** the warm pool keeps 3 VMs running all the time. While testing, set
`WARM_POOL_SIZE=1` in `/opt/cstam/.env` on the control VM and restart the API.

## 8. Destroy (do it every time you stop)

```bash
infra/openstack/destroy.sh     # sbx-* VMs, ports, security groups, then both stacks
openstack server list; openstack floating ip list    # should be empty of cstam resources
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Floating IP times out, gateways look fine | allowed-address-pairs missing on gw ports | `openstack port show cstam-gw1 -c allowed_address_pairs` must contain 10.20.0.100 |
| Both gateways MASTER (split brain) | VRRP (IP protocol 112) blocked | `openstack security group rule list cstam-gateway-sg` must contain protocol 112 |
| `/api/gateways` shows UNREACHABLE | agent not started or wrong token | `ssh -J ubuntu@<control IP> ubuntu@10.20.0.11 systemctl status gw-agent` |
| Sandbox stuck in PROVISIONING then FAILED | wrong `SANDBOX_IMAGE`/`SANDBOX_FLAVOR` or quota | `ssh ubuntu@<control IP> 'cd /opt/cstam && sudo docker compose logs api'` |
| `stack create` fails at once | Heat cannot find a name | `openstack stack event list cstam-cell` |
