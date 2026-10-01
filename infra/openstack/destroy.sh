#!/usr/bin/env bash
# Removes everything deploy.sh created: sandbox VMs/ports/security groups first (they live on the
# cell network and would block its deletion), then the control and cell stacks.
# Responsible for: a clean teardown. NOT responsible for: DNS records or your keypair.
set -euo pipefail
: "${OS_CLOUD:=felcloud}"; export OS_CLOUD

echo "==> sandboxes created by the API (names sbx-*)"
for server in $(openstack server list --name '^sbx-' -f value -c ID); do
    openstack server delete --wait "$server"
done
for port in $(openstack port list -f value -c ID -c Name | awk '$2 ~ /^sbx-/ {print $1}'); do
    openstack port delete "$port"
done
for group in $(openstack security group list -f value -c ID -c Name | awk '$2 ~ /^sbx-/ {print $1}'); do
    openstack security group delete "$group"
done

echo "==> stacks"
openstack stack delete --yes --wait cstam-control || true
openstack stack delete --yes --wait cstam-cell
echo "Destroyed."
