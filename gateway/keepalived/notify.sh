#!/bin/sh
# Keepalived notify hook, called on every VRRP transition as: notify.sh INSTANCE <name> <STATE> <prio>.
# Responsible for: writing MASTER / BACKUP / FAULT to /run/gw/vrrp_state, which gw-agent reports.
# NOT responsible for: moving the VIP (Keepalived does it).
# Criterion 2 — dual HA gateways.
mkdir -p /run/gw
echo "$3" > /run/gw/vrrp_state
echo "[notify] $(date '+%H:%M:%S.%3N') $2 -> $3"
