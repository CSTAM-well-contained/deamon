# Criterion 3 — the reclaimer: background job that gives resources back.
# Responsible for: (1) expired leases → teardown, (2) PROVISIONING stuck > 10 min → FAILED,
# (3) QUARANTINED > 60 s → FREE, (4) orphans: leases of dead sandboxes, VMs/containers with no live row.
# NOT responsible for: how teardown works (sandboxes/service.py). Never crashes the scheduler.

import logging
from datetime import timedelta

from sqlalchemy import select

from app.database import LIVE_STATUSES, Sandbox, SessionLocal, utcnow
from app.drivers import get_driver
from app.events import record_event
from app.ipam import service as ipam
from app.sandboxes import service as sandboxes

log = logging.getLogger("reclaimer")
STUCK_PROVISIONING = timedelta(minutes=10)
QUARANTINE_SECONDS = 60


async def run() -> None:
    for step in (expire_leases, fail_stuck_provisioning, free_quarantined, fix_orphans):
        try:
            await step()
        except Exception:
            log.exception("reclaimer step %s failed", step.__name__)  # next step still runs


async def expire_leases() -> None:
    query = select(Sandbox.team).where(Sandbox.status == "ACTIVE", Sandbox.expires_at < utcnow())
    async with SessionLocal() as session:
        teams = list((await session.execute(query)).scalars())
    for team in teams:
        await sandboxes.teardown(team, reason="lease_expired")


async def fail_stuck_provisioning() -> None:
    query = select(Sandbox.id).where(Sandbox.status == "PROVISIONING", Sandbox.created_at < utcnow() - STUCK_PROVISIONING)
    async with SessionLocal() as session:
        stuck = list((await session.execute(query)).scalars())
    for sandbox_id in stuck:
        await sandboxes.fail(sandbox_id, "provisioning took more than 10 minutes")


async def free_quarantined() -> None:
    for ip in await ipam.unquarantine_expired(QUARANTINE_SECONDS):
        await record_event("ipam.unquarantined", f"{ip} back to FREE after quarantine", ip=ip)


async def fix_orphans() -> None:
    for ip in await ipam.release_orphans():
        await record_event("ipam.orphan_fixed", f"{ip} was held by a dead sandbox, released", ip=ip)

    # Ask the driver what really exists, compare with rows that should own something.
    resources = await get_driver().list_managed()
    async with SessionLocal() as session:
        live = {str(i) for i in (await session.execute(select(Sandbox.id).where(Sandbox.status.in_(LIVE_STATUSES)))).scalars()}
    for resource in resources:
        if resource["sandbox_id"] not in live:
            await get_driver().delete(resource["external_id"], resource)
            await record_event("driver.orphan_removed", f"removed orphan {resource['external_id'][:12]}",
                               ip=resource["ip"], sandbox_id=resource["sandbox_id"])
