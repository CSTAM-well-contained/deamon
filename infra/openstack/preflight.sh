#!/usr/bin/env bash
# FelCloud preflight: checks, READ-ONLY, that this cloud account can run the CSTAM cell before deploy.sh.
# Responsible for: login, services (Heat, DNS, Octavia), names deploy.sh needs (external network, image,
# flavor, keypair), the two VIP prerequisites (allowed-address-pairs, port security) and quotas.
# NOT responsible for: creating anything (see deploy.sh). Serves all criteria in cloud mode.
# Usage: OS_CLOUD=felcloud_demo infra/openstack/preflight.sh
set -uo pipefail
: "${OS_CLOUD:=felcloud_demo}"; export OS_CLOUD

GREEN=$'\e[32m'; RED=$'\e[31m'; YELLOW=$'\e[33m'; BOLD=$'\e[1m'; RESET=$'\e[0m'
ok()   { echo "  ${GREEN}✔ $*${RESET}"; }
bad()  { echo "  ${RED}✘ $*${RESET}"; BLOCKERS=$((BLOCKERS + 1)); }
warn() { echo "  ${YELLOW}! $*${RESET}"; }
step() { echo; echo "${BOLD}▶ $*${RESET}"; }
BLOCKERS=0

step "Login (cloud entry: $OS_CLOUD)"
if openstack token issue -f value -c project_id >/dev/null 2>&1; then
    ok "authenticated, project $(openstack token issue -f value -c project_id)"
else
    bad "cannot log in: check ~/.config/openstack/clouds.yaml and the entry name '$OS_CLOUD'"
    exit 1
fi

step "Services in the catalog"
CATALOG=$(openstack catalog list -f value -c Type 2>/dev/null)
for service in compute network image; do
    grep -qx "$service" <<<"$CATALOG" && ok "$service" || bad "$service missing"
done
grep -qx orchestration <<<"$CATALOG" && ok "orchestration (Heat): deploy.sh can be used as is" \
    || bad "no Heat: ask FelCloud, or use a CLI-only deploy (no stack create)"
grep -qx dns <<<"$CATALOG" && ok "dns (Designate) available" || warn "no Designate: use a wildcard DNS record or sslip.io (see FELCLOUD.md)"
grep -qx load-balancer <<<"$CATALOG" && ok "load-balancer (Octavia) exists (not needed)" || true

step "Network extensions needed by the VIP"
EXT=$(openstack extension list --network -f value -c Alias 2>/dev/null)
grep -qx allowed-address-pairs <<<"$EXT" && ok "allowed-address-pairs" || bad "allowed-address-pairs missing: the VIP cannot work"
grep -qx port-security <<<"$EXT" && ok "port-security" || warn "port-security extension not listed"

step "External network (EXTERNAL_NETWORK)"
openstack network list --external -f value -c Name | sed 's/^/    /'
[ -n "$(openstack network list --external -f value -c Name)" ] || bad "no external network visible"

step "Ubuntu images (IMAGE) — prefer 22.04 or 24.04"
openstack image list -f value -c Name | grep -iE 'ubuntu|debian' | sed 's/^/    /' || warn "no Ubuntu/Debian image found"

step "Flavors (FLAVOR) — gateways and control need >= 1 vCPU / 2 GB"
openstack flavor list -f value -c Name -c VCPUs -c RAM | sort -k3 -n | awk '{printf "    %-28s %s vCPU  %s MB\n", $1, $3, $2}'

step "Keypairs (KEY_NAME)"
KEYS=$(openstack keypair list -f value -c Name)
[ -n "$KEYS" ] && sed 's/^/    /' <<<"$KEYS" || bad "no keypair: openstack keypair create --public-key ~/.ssh/id_ed25519.pub cstam"

step "Quotas (cell = 3 VMs + 1 per sandbox; 2 floating IPs; 1 router)"
openstack quota show -f value -c instances -c cores -c ram -c floating-ips -c routers -c ports -c secgroups 2>/dev/null \
    | paste -sd' ' | awk '{printf "    instances %s  cores %s  ram %s MB  floating-ips %s  routers %s  ports %s  secgroups %s\n", $1,$2,$3,$4,$5,$6,$7}' \
    || warn "could not read quotas (check them in Skyline)"
echo "    Used now: $(openstack server list -f value -c ID | wc -l) servers, $(openstack floating ip list -f value -c ID | wc -l) floating IPs"

echo
if [ "$BLOCKERS" -eq 0 ]; then
    echo "${GREEN}${BOLD}Ready.${RESET} Pick the names above and run deploy.sh (see infra/openstack/FELCLOUD.md)."
else
    echo "${RED}${BOLD}$BLOCKERS blocker(s).${RESET} Fix them (or ask contact@felcloud.tn) before deploy.sh."
fi
