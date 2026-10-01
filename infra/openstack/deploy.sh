#!/usr/bin/env bash
# Deploys the CSTAM cell on OpenStack: Heat stacks, gw-agent binary on both gateways, API on the control VM.
# Responsible for: `openstack stack create --wait` (cell then control), copying files over SSH, printing
# the floating IP and next steps. NOT responsible for: DNS (you add *.cstam.felcloud.tn yourself).
# Usage: KEY_NAME=mykey EXTERNAL_NETWORK=public AGENT_TOKEN=$(openssl rand -hex 16) ./deploy.sh
set -euo pipefail
cd "$(dirname "$0")"
REPO=$(cd ../.. && pwd)

: "${OS_CLOUD:=felcloud}"; export OS_CLOUD
: "${KEY_NAME:?set KEY_NAME to your Nova keypair}"
: "${AGENT_TOKEN:?set AGENT_TOKEN (e.g. openssl rand -hex 16)}"
EXTERNAL_NETWORK=${EXTERNAL_NETWORK:-public}
IMAGE=${IMAGE:-ubuntu-22.04}
FLAVOR=${FLAVOR:-m1.small}
ADMIN_CIDR=${ADMIN_CIDR:-0.0.0.0/0}
SSH_USER=${SSH_USER:-ubuntu}
CLOUDS_YAML=${CLOUDS_YAML:-$HOME/.config/openstack/clouds.yaml}
SSH="ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5"

echo "==> 1/5 cell stack (network, VIP + floating IP, gw1, gw2)"
openstack stack create --wait -t heat/cell.yaml \
    --parameter external_network="$EXTERNAL_NETWORK" --parameter image="$IMAGE" \
    --parameter flavor="$FLAVOR" --parameter key_name="$KEY_NAME" \
    --parameter admin_cidr="$ADMIN_CIDR" --parameter agent_token="$AGENT_TOKEN" cstam-cell

echo "==> 2/5 control stack (API + Postgres VM)"
openstack stack create --wait -t heat/control.yaml \
    --parameter external_network="$EXTERNAL_NETWORK" --parameter image="$IMAGE" \
    --parameter flavor="$FLAVOR" --parameter key_name="$KEY_NAME" \
    --parameter agent_token="$AGENT_TOKEN" --parameter os_cloud="$OS_CLOUD" cstam-control

FIP=$(openstack stack output show cstam-cell floating_ip -f value -c output_value)
CONTROL=$(openstack stack output show cstam-control control_ip -f value -c output_value)

echo "==> 3/5 waiting for SSH + cloud-init on the control VM ($CONTROL)"
until $SSH "$SSH_USER@$CONTROL" 'cloud-init status --wait >/dev/null 2>&1; true'; do sleep 5; done

echo "==> 4/5 gw-agent binary → gw1, gw2 (through the control VM)"
[ -x "$REPO/dist/gw-agent" ] || make -C "$REPO" agent-static
for gw in 10.20.0.11 10.20.0.12; do
    scp -o StrictHostKeyChecking=accept-new -J "$SSH_USER@$CONTROL" "$REPO/dist/gw-agent" "$SSH_USER@$gw:/tmp/gw-agent"
    $SSH -J "$SSH_USER@$CONTROL" "$SSH_USER@$gw" \
        'cloud-init status --wait >/dev/null 2>&1; sudo install -m 755 /tmp/gw-agent /usr/local/bin/gw-agent && sudo systemctl start gw-agent'
done

echo "==> 5/5 API code + clouds.yaml → control VM, docker compose up"
$SSH "$SSH_USER@$CONTROL" 'sudo mkdir -p /opt/cstam && sudo chown -R $(id -u) /opt/cstam'
scp -r "$REPO/api" "$SSH_USER@$CONTROL:/opt/cstam/"
scp -r "$REPO/infra/openstack/cloud-init" "$SSH_USER@$CONTROL:/opt/cstam/"
scp "$CLOUDS_YAML" "$SSH_USER@$CONTROL:/opt/cstam/clouds.yaml"
$SSH "$SSH_USER@$CONTROL" 'cd /opt/cstam && sudo docker compose -f compose.yml up -d --build'

cat <<DONE

Deployed.
  Floating IP (VIP):  $FIP
  API:                http://$CONTROL:8000/docs

Next steps:
  1. DNS:   *.cstam.felcloud.tn  A  $FIP
  2. Try:   curl -X POST http://$CONTROL:8000/api/sandboxes -H 'Content-Type: application/json' -d '{"team":"team1"}'
            curl http://team1.cstam.felcloud.tn/      (or: curl -H 'Host: team1.cstam.felcloud.tn' http://$FIP/)
  3. Gateways: curl http://$CONTROL:8000/api/gateways
DONE
