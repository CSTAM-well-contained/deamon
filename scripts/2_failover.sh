#!/usr/bin/env bash
# Demo 2 — Criterion 2 (dual HA gateways sharing one VIP).
# Responsible for: killing the MASTER under live traffic and measuring the outage, then a soft
# failover through the API. NOT responsible for: config changes (demo 3).
source "$(dirname "$0")/lib.sh"

ensure_team team1
HOST="team1.$DOMAIN"

step "Before: $(master) is MASTER and holds the VIP"
api GET /api/gateways | jq -c '[.gateways[] | {name, vrrp_state}]'

step "Traffic: $HOST every 50 ms for 15 s; at 3 s 'docker compose stop gw1', at 10 s start it again"
start_traffic "$HOST"
sleep 3;  info "t=3s  stopping gw1";  $COMPOSE stop gw1 >/dev/null 2>&1
sleep 3;  info "t=6s  VIP now answered by: $(vip_curl "$HOST" | cut -d" " -f2)"
sleep 4;  info "t=10s starting gw1"; $COMPOSE start gw1 >/dev/null 2>&1
sleep 5;  stop_traffic
traffic_report
[ "$GAP_MS" -lt 1500 ] && ok "outage below 1.5 s" || warn "outage ${GAP_MS} ms (target < 1500 ms)"
[ "$FAILED" -lt "$TOTAL" ] || fail "no request succeeded"

for _ in $(seq 20); do [ "$(master)" = gw1 ] && break; sleep 1; done
step "After: $(master) is MASTER again (gw1 has the higher priority and preempts)"
api GET /api/events?kind=gateway.state_changed\&limit=6 | jq -r '.[] | "  \(.ts[11:23])  \(.message)"'

step "Soft failover through the API: POST /api/gateways/gw1/failover"
api POST /api/gateways/gw1/failover | jq -c '{gateway, forced_backup}'
for _ in $(seq 10); do [ "$(master)" = gw2 ] && break; sleep 1; done
[ "$(master)" = gw2 ] && ok "gw2 took the VIP" || fail "gw2 did not become MASTER"
read -r code served_by < <(vip_curl "$HOST")
ok "$HOST → $code served by $served_by"

step "Undo: POST /api/gateways/gw1/failover/clear"
api POST /api/gateways/gw1/failover/clear | jq -c '{gateway, forced_backup}'
for _ in $(seq 10); do [ "$(master)" = gw1 ] && break; sleep 1; done
[ "$(master)" = gw1 ] && ok "gw1 is MASTER again" || fail "gw1 did not come back"
