#!/bin/sh
# Keepalived health check: is HAProxy alive AND answering HTTP?
# Responsible for: exit 0 = healthy, anything else = unhealthy (2 failures → FAULT → VIP moves).
# NOT responsible for: restarting HAProxy.
# Criterion 2 — dual HA gateways.
pid=$(cat /run/haproxy.pid 2>/dev/null) || exit 1
kill -0 "$pid" 2>/dev/null || exit 1
curl -fsS -m 0.5 -o /dev/null http://127.0.0.1/__gw_health
