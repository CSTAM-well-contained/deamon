#!/usr/bin/env bash
# Shared helpers for the demo scripts (sourced, never run directly).
# Responsible for: api() calls, vip_curl() from inside the client container, wait_for(), background
# traffic with a failure report, and colored step/ok/fail printers.
# NOT responsible for: any demo scenario (1_*.sh … 4_*.sh). Serves all criteria (demo).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."   # every script runs from the repository root
API=${API:-http://localhost:8000}
# shellcheck disable=SC2034  # DOMAIN is used by the demo scripts that source this file
DOMAIN=${BASE_DOMAIN:-cstam.felcloud.tn}
VIP=10.20.0.100
if [ -z "${COMPOSE:-}" ]; then
    if docker compose version >/dev/null 2>&1; then COMPOSE="docker compose"; else COMPOSE="docker-compose"; fi
fi

BOLD=$'\e[1m'; GREEN=$'\e[32m'; RED=$'\e[31m'; YELLOW=$'\e[33m'; BLUE=$'\e[34m'; RESET=$'\e[0m'
step() { echo; echo "${BOLD}${BLUE}▶ $*${RESET}"; }
info() { echo "  $*"; }
ok()   { echo "  ${GREEN}✔ $*${RESET}"; }
warn() { echo "  ${YELLOW}! $*${RESET}"; }
fail() { echo "  ${RED}✘ $*${RESET}"; exit 1; }

# api METHOD PATH [JSON] → prints the response body; HTTP code is saved in $API_STATUS_FILE.
API_STATUS_FILE=$(mktemp)
api() {
    local method=$1 path=$2 body=${3:-}
    local args=(-sS -X "$method" "$API$path" -o /dev/stdout -w '%{http_code}' -H 'Content-Type: application/json')
    [ -n "${API_KEY:-}" ] && args+=(-H "X-API-Key: $API_KEY")
    [ -n "$body" ] && args+=(-d "$body")
    local output
    output=$(curl "${args[@]}")
    echo "${output: -3}" > "$API_STATUS_FILE"
    echo "${output%???}"
}
api_status() { cat "$API_STATUS_FILE"; }

# Run a command inside the "client" container (a user sitting on the network).
in_client() { $COMPOSE exec -T client "$@"; }

# vip_curl HOST [PATH] → "<http status> <gateway that served it>"
vip_curl() {
    in_client curl -s -o /dev/null -m 10 -w '%{http_code} %header{x-served-by}\n' -H "Host: $1" "http://$VIP${2:-/}"
}
# vip_body HOST [PATH] → the page body
vip_body() { in_client curl -s -m 10 -H "Host: $1" "http://$VIP${2:-/}"; }

# wait_for TEAM STATUS TIMEOUT_SECONDS
wait_for() {
    local team=$1 wanted=$2 timeout=$3 status=""
    for _ in $(seq "$timeout"); do
        status=$(api GET "/api/sandboxes/$team" | jq -r '.status // empty')
        [ "$status" = "$wanted" ] && return 0
        sleep 1
    done
    fail "$team is '$status' after ${timeout}s (wanted $wanted)"
}

master() { api GET /api/gateways | jq -r '.master // "none"'; }

# Make sure TEAM has an ACTIVE sandbox (used by demos that need team1).
ensure_team() {
    local status
    status=$(api GET "/api/sandboxes/$1" | jq -r '.status // empty')
    if [ "$status" != "ACTIVE" ]; then
        api POST /api/sandboxes "{\"team\":\"$1\"}" >/dev/null
        wait_for "$1" ACTIVE 60
    fi
}

# start_traffic HOST → curl HOST through the VIP every 50 ms (inside client) until stop_traffic.
# Each line of the log is "<epoch ms> <http status>".
start_traffic() {
    in_client sh -c 'rm -f /tmp/stop /tmp/traffic.log'
    in_client sh -c "while [ ! -f /tmp/stop ]; do t=\$(date +%s%3N); \
        c=\$(curl -s -o /dev/null -m 1 -w '%{http_code}' -H 'Host: $1' http://$VIP/); \
        echo \"\$t \$c\" >> /tmp/traffic.log; sleep 0.05; done" &
    TRAFFIC_PID=$!
    sleep 0.5
}
stop_traffic() {
    in_client touch /tmp/stop
    wait "$TRAFFIC_PID" 2>/dev/null || true
}

# traffic_report → prints totals and the longest outage; sets TOTAL, FAILED, GAP_MS.
traffic_report() {
    read -r TOTAL FAILED GAP_MS < <(in_client cat /tmp/traffic.log | awk '
        { total++ }
        $2 != "200" { failed++; if (!down) { down = 1; start = last_ok ? last_ok : $1 } }
        $2 == "200" { if (down && $1 - start > gap) gap = $1 - start; down = 0; last_ok = $1 }
        END { printf "%d %d %d\n", total, failed, gap }')
    info "requests: $TOTAL   failed: $FAILED   longest failure gap: ${GAP_MS} ms"
}
