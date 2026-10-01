#!/usr/bin/env bash
# Demo 1 — Criterion 1 (provisioning) + Criterion 3 (IPAM).
# Responsible for: warm-pool claims in milliseconds (201), cold builds when the pool is empty (202),
# every team reachable through the VIP, and the IP pool view. NOT responsible for: failover (demo 2).
source "$(dirname "$0")/lib.sh"

step "Clean start: remove team1..team5 if a previous run left them"
for team in team1 team2 team3 team4 team5; do api DELETE "/api/sandboxes/$team" >/dev/null || true; done

step "Warm pool: sandboxes already booted and waiting"
for _ in $(seq 60); do
    [ "$(api GET /api/warm-pool | jq '.warm')" -ge 3 ] && break
    sleep 1
done
api GET /api/warm-pool | jq -c .

step "team1..team3 claim warm sandboxes (expect HTTP 201, a few ms)"
for team in team1 team2 team3; do
    body=$(api POST /api/sandboxes "{\"team\":\"$team\"}")
    code=$(api_status)
    [ "$code" = 201 ] || fail "$team: HTTP $code $body"
    ok "$team → $(jq -r '"\(.ip)  \(.hostname)  provision_ms=\(.provision_ms)"' <<<"$body")"
done

step "team4..team5: the warm pool is now empty → cold build in the background (HTTP 202)"
for team in team4 team5; do
    body=$(api POST /api/sandboxes "{\"team\":\"$team\"}")
    code=$(api_status)
    [[ "$code" = 201 || "$code" = 202 ]] || fail "$team: HTTP $code $body"
    ok "$team → HTTP $code, status $(jq -r .status <<<"$body"), ip $(jq -r .ip <<<"$body")"
done
for team in team4 team5; do wait_for "$team" ACTIVE 60; done
ok "team4 + team5 ACTIVE ($(api GET /api/sandboxes/team4 | jq .provision_ms) ms, $(api GET /api/sandboxes/team5 | jq .provision_ms) ms)"

step "Every team is reachable through the VIP $VIP with its own hostname"
for team in team1 team2 team3 team4 team5; do
    read -r code served_by < <(vip_curl "$team.$DOMAIN")
    [ "$code" = 200 ] || fail "$team.$DOMAIN → HTTP $code"
    vip_body "$team.$DOMAIN" | grep -q "Sandbox of $team" || fail "$team page does not show the team"
    ok "$team.$DOMAIN → $code (served by $served_by)"
done

step "IP pool (Criterion 3)"
api GET /api/ipam | jq -c '.summary'
api GET /api/ipam | jq -r '.leases[] | "  \(.ip)  \(.state)  \(.team // "-")"'
