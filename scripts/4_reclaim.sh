#!/usr/bin/env bash
# Demo 4 — Criterion 3 (reclamation) + bonus maintenance page.
# Responsible for: lease expiry → teardown + IP freed + route gone; manual delete frees the IP at once;
# a stopped sandbox shows the 503 maintenance page. NOT responsible for: provisioning speed (demo 1).
source "$(dirname "$0")/lib.sh"

lease_state() { api GET /api/ipam | jq -r --arg ip "$1" '[.leases[] | select(.ip == $ip) | .state][0] // "FREE"'; }
for team in tempteam fastteam; do api DELETE "/api/sandboxes/$team" >/dev/null || true; done

step "tempteam gets a 15 s lease"
ip=$(api POST /api/sandboxes '{"team":"tempteam","ttl_seconds":15}' | jq -r .ip)
wait_for tempteam ACTIVE 60
read -r code _ < <(vip_curl "tempteam.$DOMAIN"); ok "tempteam ($ip) → $code, lease $(lease_state "$ip")"
info "waiting 25 s for the reclaimer…"; sleep 25
status=$(api GET /api/sandboxes/tempteam | jq -r .status)
reason=$(api GET "/api/events?kind=sandbox.deleted&limit=20" | jq -r '[.[] | select(.team == "tempteam")][0].data.reason')
[ "$status" = DELETED ] && ok "tempteam is DELETED (reason: $reason)" || fail "tempteam is $status"
[ "$(lease_state "$ip")" = FREE ] && ok "IP $ip is FREE again" || fail "IP $ip still held"
read -r code _ < <(vip_curl "tempteam.$DOMAIN")
[ "$code" = 404 ] && ok "tempteam.$DOMAIN → 404 (route removed)" || fail "expected 404, got $code"

step "fastteam: create then delete → IP free immediately"
api POST /api/sandboxes '{"team":"fastteam"}' >/dev/null
wait_for fastteam ACTIVE 60
ip=$(api GET /api/sandboxes/fastteam | jq -r .ip)
info "fastteam has $ip ($(lease_state "$ip"))"
api DELETE /api/sandboxes/fastteam | jq -c '{status, teardown_ms}'
[ "$(lease_state "$ip")" = FREE ] && ok "IP $ip FREE right after the delete" || fail "IP $ip still held"

step "Maintenance page: stop team1's sandbox container, then ask for team1"
ensure_team team1
ip=$(api GET /api/sandboxes/team1 | jq -r .ip)
container=$(docker ps -q --filter "label=cstam.ip=$ip" --filter label=cstam.managed=true)
docker stop -t 0 "$container" >/dev/null
read -r code _ < <(vip_curl "team1.$DOMAIN")
[ "$code" = 503 ] && ok "team1.$DOMAIN → 503" || fail "expected 503, got $code"
vip_body "team1.$DOMAIN" | grep -o '<h1>.*</h1>' | sed 's/^/  /'
docker start "$container" >/dev/null
sleep 1
read -r code _ < <(vip_curl "team1.$DOMAIN"); ok "sandbox started again → $code"
