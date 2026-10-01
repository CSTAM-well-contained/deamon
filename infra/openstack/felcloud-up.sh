#!/usr/bin/env bash
# One command to deploy the CSTAM cell on FelCloud from your laptop.
# Responsible for: installing the OpenStack CLI in a venv, running preflight, picking the FelCloud names
# (external network, Ubuntu image, flavors), creating your keypair, then calling deploy.sh. Logs to deploy.log.
# NOT responsible for: the deployment itself (deploy.sh + heat/*.yaml) or DNS. Serves all criteria in cloud mode.
# Usage (from the repo root, with ./clouds.yaml present):   bash infra/openstack/felcloud-up.sh
set -euo pipefail
REPO=$(cd "$(dirname "$0")/../.." && pwd)
cd "$REPO"
exec > >(tee -a "$REPO/deploy.log") 2>&1
echo "==== $(date) felcloud-up ===="

export OS_CLOUD=${OS_CLOUD:-felcloud}
export OS_CLIENT_CONFIG_FILE=${OS_CLIENT_CONFIG_FILE:-$REPO/clouds.yaml}   # works from any directory
export CLOUDS_YAML=$OS_CLIENT_CONFIG_FILE                                   # deploy.sh copies it to the control VM
[ -f "$CLOUDS_YAML" ] || { echo "missing $CLOUDS_YAML"; exit 1; }

echo "==> OpenStack CLI (venv in .venv-openstack)"
if [ ! -x .venv-openstack/bin/openstack ]; then
    python3 -m venv .venv-openstack || { echo "install python3-venv first: sudo apt install python3-venv"; exit 1; }
    .venv-openstack/bin/pip install -q python-openstackclient python-heatclient
fi
export PATH="$REPO/.venv-openstack/bin:$PATH"

echo "==> Preflight"
bash infra/openstack/preflight.sh || { echo "Preflight found blockers, stopping."; exit 1; }

echo "==> Picking FelCloud names (override any of them by exporting it before running)"
FLAVORS_JSON=$(openstack flavor list -f json)
pick_flavor() {  # $1 = min vCPUs, $2 = min RAM MB → smallest matching flavor name
    python3 -c "import json,sys; f=[x for x in json.loads(sys.argv[1]) if x['VCPUs']>=$1 and x['RAM']>=$2]; \
f.sort(key=lambda x:(x['VCPUs'],x['RAM'])); print(f[0]['Name'] if f else '')" "$FLAVORS_JSON"
}
export EXTERNAL_NETWORK=${EXTERNAL_NETWORK:-$(openstack network list --external -f value -c Name | head -1)}
export IMAGE=${IMAGE:-$(openstack image list -f value -c Name | grep -iE 'ubuntu.*22' | head -1)}
[ -n "$IMAGE" ] || IMAGE=$(openstack image list -f value -c Name | grep -iE 'ubuntu.*24' | head -1)
export FLAVOR=${FLAVOR:-$(pick_flavor 2 4096)}
export SANDBOX_IMAGE=${SANDBOX_IMAGE:-$IMAGE}
export SANDBOX_FLAVOR=${SANDBOX_FLAVOR:-$(pick_flavor 1 1024)}
export ADMIN_CIDR=${ADMIN_CIDR:-$(curl -s -4 https://ifconfig.me)/32}
for v in EXTERNAL_NETWORK IMAGE FLAVOR SANDBOX_FLAVOR ADMIN_CIDR; do
    [ -n "${!v}" ] && [ "${!v}" != "/32" ] || { echo "Could not pick $v automatically: export $v=... and re-run"; exit 1; }
    echo "    $v=${!v}"
done

echo "==> Keypair"
[ -f ~/.ssh/id_ed25519 ] || ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_ed25519 -q
export KEY_NAME=${KEY_NAME:-cstam-$(whoami)}
openstack keypair show "$KEY_NAME" >/dev/null 2>&1 \
    || openstack keypair create --public-key ~/.ssh/id_ed25519.pub "$KEY_NAME" >/dev/null
echo "    KEY_NAME=$KEY_NAME"

echo "==> Agent token (kept in .agent-token, git-ignored)"
[ -s .agent-token ] || openssl rand -hex 16 > .agent-token
export AGENT_TOKEN=$(cat .agent-token)

echo "==> Static gw-agent binary"
[ -x dist/gw-agent ] || make agent-static

echo "==> Deploy (about 10 minutes)"
bash infra/openstack/deploy.sh

echo "==== done. Full log: $REPO/deploy.log ===="
