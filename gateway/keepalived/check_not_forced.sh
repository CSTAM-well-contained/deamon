#!/bin/sh
# Keepalived track script used to force a failover on demand.
# Responsible for: failing while /run/gw/force_backup exists (gw-agent creates it on POST /failover),
# which lowers our priority by 100 so the peer takes the VIP. NOT responsible for: creating the file.
# Criterion 2 — dual HA gateways.
[ ! -f /run/gw/force_backup ]
