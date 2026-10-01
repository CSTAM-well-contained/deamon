# Criteria 2 + 4 — the gateway monitor (background job).
# Responsible for: polling every agent's /status into app.state.gateways, recording VRRP changes
# (= failovers) as events, re-pushing config/routes to a gateway that is behind (e.g. after a restart),
# and the first push at startup. NOT responsible for: VRRP itself (Keepalived on the gateways).

import asyncio
import logging

from fastapi import FastAPI

from app.database import utcnow
from app.events import record_event
from app.gateways import config_sync, route_sync
from app.gateways.client import get_client

log = logging.getLogger("monitor")


async def tick(app: FastAPI) -> None:
    try:
        statuses = await get_client().statuses()
        previous = app.state.gateways
        current = {}
        for name, status in statuses.items():
            vrrp_state = status["vrrp_state"] if status else "UNREACHABLE"
            old_state = previous.get(name, {}).get("vrrp_state")
            if old_state and old_state != vrrp_state:
                await record_event("gateway.state_changed", f"{name}: {old_state} → {vrrp_state}",
                                   gateway=name, old=old_state, new=vrrp_state)
            current[name] = (status or {}) | {"name": name, "vrrp_state": vrrp_state,
                                              "reachable": status is not None, "checked_at": utcnow()}
            if status:
                await _resync_if_behind(name, status)
        app.state.gateways = current
    except Exception:
        log.exception("monitor tick failed")  # never crash the scheduler


async def _resync_if_behind(name: str, status: dict) -> None:
    fixed = {}
    config_result = await config_sync.resync(name, status.get("config_version"))
    if config_result:
        fixed["config"] = config_result.get("outcome")
    routes_result = await route_sync.resync(name, status.get("routes_version"))
    if routes_result:
        fixed["routes"] = routes_result.get("outcome")
    if fixed:
        await record_event("gateway.resynced", f"{name} was behind, re-pushed {', '.join(fixed)}",
                           gateway=name, **fixed)


async def initial_sync(app: FastAPI, wait_seconds: int = 60) -> None:
    """At API start: once at least one agent answers, push the config and the routes."""
    for _ in range(wait_seconds):
        statuses = await get_client().statuses()
        if any(statuses.values()):
            break
        await asyncio.sleep(1)
    else:
        log.error("no gateway answered within %s s; the monitor will push when they appear", wait_seconds)
    await config_sync.apply("startup")
    await route_sync.push()
    await tick(app)
