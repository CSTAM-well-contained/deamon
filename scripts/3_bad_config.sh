#!/usr/bin/env bash
# Demo 3 — Criterion 4 (zero-downtime changes and automatic rollback).
# Responsible for: route add/remove without reload, a syntax-bad config stopped at the backup,
# a semantic-bad config rolled back in < 1 s — all with live traffic. NOT responsible for: failover.
source "$(dirname "$0")/lib.sh"

ensure_team team1
api DELETE /api/sandboxes/flash >/dev/null || true
worker_pid() { $COMPOSE exec -T "$1" pgrep -n haproxy; }   # newest haproxy process = current worker

step "Live traffic on team1.$DOMAIN during the whole demo"
start_traffic "team1.$DOMAIN"
master_before=$(master)
pid_before=$(worker_pid "$master_before")

step "Route change WITHOUT reload: add then remove team 'flash'"
api POST /api/sandboxes '{"team":"flash"}' | jq -c '{team, ip, status, provision_ms}'
wait_for flash ACTIVE 60
read -r code _ < <(vip_curl "flash.$DOMAIN"); ok "flash.$DOMAIN → $code"
api DELETE /api/sandboxes/flash | jq -c '{team, status, teardown_ms}'
read -r code _ < <(vip_curl "flash.$DOMAIN"); ok "flash.$DOMAIN → $code (route removed)"
[ "$(worker_pid "$master_before")" = "$pid_before" ] && ok "HAProxy worker pid unchanged ($pid_before): no reload" \
    || fail "HAProxy was reloaded"

step "Inject a SYNTAX-bad config: the backup's 'haproxy -c' must reject it, the master never sees it"
result=$(api POST /api/config/inject-bad '{"mode":"syntax"}')
jq -r '.results | to_entries[] | "  \(.key) (\(.value.vrrp_state)): \(.value.outcome)"' <<<"$result"
[ "$(jq -r .status <<<"$result")" = REJECTED ] || fail "expected REJECTED"
[ "$(jq -r ".results.$master_before.outcome" <<<"$result")" = skipped ] && ok "master $master_before untouched" \
    || fail "master received the bad config"

step "Inject a SEMANTIC-bad config (valid syntax, binds :8099): reload, probe fails, automatic rollback"
result=$(api POST /api/config/inject-bad '{"mode":"semantic"}')
jq -r '.results | to_entries[] | "  \(.key) (\(.value.vrrp_state)): \(.value.outcome)  rollback_ms=\(.value.rollback_ms // "-")"' <<<"$result"
[ "$(jq -r .status <<<"$result")" = ROLLED_BACK ] || fail "expected ROLLED_BACK"
rollback_ms=$(jq '[.results[].rollback_ms // empty] | max' <<<"$result")
[ "$rollback_ms" -lt 1000 ] && ok "rolled back in ${rollback_ms} ms (< 1000)" || fail "rollback took ${rollback_ms} ms"

stop_traffic
step "Traffic during all of this"
traffic_report
[ "$FAILED" = 0 ] && ok "0 failed requests" || fail "$FAILED requests failed"

step "Config history (/api/config/versions)"
api GET /api/config/versions | jq -r '.[:6][] | "  v\(.version)  \(.status)\t\(.reason)"'
