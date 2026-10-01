#!/bin/sh
# Gateway container entrypoint: prepares files, starts HAProxy + Keepalived, then runs gw-agent.
# Responsible for: first-boot bootstrap (v0 config, empty map), rendering keepalived.conf from env.
# NOT responsible for: config changes after boot (gw-agent does them via releases + symlink).
# Criteria 2 + 4.
set -eu

: "${GW_NAME:?}" "${ROLE:?}" "${PRIORITY:?}" "${SELF_IP:?}" "${PEER_IP:?}" "${VIP:?}"
: "${IFACE:=eth0}" "${ADVERT_INT:=1}"
export GW_NAME ROLE PRIORITY SELF_IP PEER_IP VIP IFACE ADVERT_INT

mkdir -p /run/gw /etc/haproxy/releases
# After "docker compose stop/start" old pid files remain; Keepalived refuses to start if it sees them.
rm -f /run/keepalived.pid /run/vrrp.pid /run/haproxy.pid
echo BACKUP > /run/gw/vrrp_state

# First boot: version 0 config. Later boots keep the last release the agent applied.
if [ ! -e /etc/haproxy/current.cfg ]; then
    cp /usr/local/share/cstam/bootstrap.cfg /etc/haproxy/releases/v0.cfg
    ln -sfn /etc/haproxy/releases/v0.cfg /etc/haproxy/current.cfg
fi
cp /usr/local/share/cstam/maintenance.http /etc/haproxy/maintenance.http
touch /etc/haproxy/hosts.map

# Render keepalived.conf: replace every ${VAR} with its value from the environment.
sed -e "s|\${GW_NAME}|$GW_NAME|g" -e "s|\${ROLE}|$ROLE|g" -e "s|\${PRIORITY}|$PRIORITY|g" \
    -e "s|\${SELF_IP}|$SELF_IP|g" -e "s|\${PEER_IP}|$PEER_IP|g" -e "s|\${VIP}|$VIP|g" \
    -e "s|\${IFACE}|$IFACE|g" -e "s|\${ADVERT_INT}|$ADVERT_INT|g" \
    /usr/local/share/cstam/keepalived.conf.tmpl > /etc/keepalived/keepalived.conf

# -W master-worker (reload = USR2, old workers finish their requests), -db = stay in foreground.
# (The pid file /run/haproxy.pid is set by "pidfile" in the config itself.)
haproxy -W -db -f /etc/haproxy/current.cfg &
keepalived --dont-fork --log-console --vrrp &

exec gw-agent
