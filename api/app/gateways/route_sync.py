# Criterion 4a — keep every gateway's hosts.map equal to the ACTIVE sandboxes. No HAProxy reload.
# Responsible for: building {hostname: ip} from the database and pushing the FULL map with a new
# routes_version (agents compute the diff and update HAProxy through its Runtime API).
# NOT responsible for: HTTP details or rollout order (client.py), the HAProxy config (config_sync.py).

import asyncio
import logging
import time

from sqlalchemy import select

from app.database import Sandbox, SessionLocal
from app.gateways.client import get_client

log = logging.getLogger("route_sync")

_lock = asyncio.Lock()  # one push at a time, so versions reach agents in order
_last_pushed = {"routes_version": 0, "routes": {}}


def _next_version() -> int:
    """Milliseconds since 1970: always bigger than before, even after an API restart."""
    return max(_last_pushed["routes_version"] + 1, int(time.time() * 1000))


async def desired_routes() -> dict[str, str]:
    query = select(Sandbox.hostname, Sandbox.ip).where(
        Sandbox.status == "ACTIVE", Sandbox.hostname.is_not(None), Sandbox.ip.is_not(None)
    )
    async with SessionLocal() as session:
        rows = (await session.execute(query)).all()
    return {hostname: ip for hostname, ip in sorted(rows)}


async def push() -> dict[str, dict]:
    """Send the current desired map to all gateways (backup first). Returns per-gateway results."""
    async with _lock:
        routes = await desired_routes()
        payload = {"routes_version": _next_version(), "routes": routes}
        _last_pushed.update(payload)
        results = await get_client().rollout("PUT", "/routes", payload)
    for name, result in results.items():
        if result["outcome"] not in ("applied", "unchanged"):
            log.warning("routes v%s on %s: %s", payload["routes_version"], name, result)
    return results


def current() -> dict:
    """What we last pushed (GET /api/routes)."""
    return dict(_last_pushed)


async def resync(name: str, agent_routes_version: int | None) -> dict | None:
    """Monitor: if this gateway is behind, send it the last map again. None = nothing to do."""
    if _lock.locked() or _last_pushed["routes_version"] == 0:
        return None  # a push is running (it will reach this gateway), or nothing was ever pushed
    if agent_routes_version == _last_pushed["routes_version"]:
        return None
    async with _lock:
        return await get_client().call(name, "PUT", "/routes", dict(_last_pushed))
