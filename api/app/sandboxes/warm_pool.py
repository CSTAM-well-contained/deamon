# Criterion 1 — the warm pool: keep WARM_POOL_SIZE sandboxes booted and waiting, so a team's
# request is answered in seconds (claim = relabel + add route) instead of a full VM boot.
# Responsible for: the background refill job and GET /api/warm-pool numbers.
# NOT responsible for: claiming a warm sandbox (service.claim_or_create does it).

import logging
import time
import uuid

from sqlalchemy import func, select

from app.database import Sandbox, SessionLocal
from app.drivers import get_driver
from app.events import record_event
from app.ipam import service as ipam
from app.sandboxes.service import save_fields, create_with_retry, fail
from app.settings import settings

log = logging.getLogger("warm_pool")


async def counts() -> dict:
    """WARM = ready to claim; booting = warm sandboxes still PROVISIONING (no team yet)."""
    query = (
        select(Sandbox.status, func.count())
        .where(Sandbox.team.is_(None), Sandbox.status.in_(("WARM", "PROVISIONING")))
        .group_by(Sandbox.status)
    )
    async with SessionLocal() as session:
        rows = dict((await session.execute(query)).all())
    return {"target": settings.warm_pool_size, "warm": rows.get("WARM", 0), "booting": rows.get("PROVISIONING", 0)}


async def refill() -> None:
    """Background job: create sandboxes one by one until warm + booting reaches the target."""
    try:
        while True:
            numbers = await counts()
            if numbers["warm"] + numbers["booting"] >= settings.warm_pool_size:
                return
            if not await _create_one():
                return  # pool exhausted or driver failing: try again next run, do not spin
    except Exception:
        log.exception("warm pool refill failed")  # never crash the scheduler


async def _create_one() -> bool:
    sandbox_id = uuid.uuid4()
    started = time.monotonic()
    try:
        ip = await ipam.allocate(sandbox_id)
    except ipam.PoolExhaustedError:
        log.warning("warm pool: IP pool exhausted")
        return False
    async with SessionLocal() as session:
        session.add(Sandbox(id=sandbox_id, status="PROVISIONING", driver=get_driver().name, ip=ip, extra={}))
        await session.commit()
    try:
        handle = await create_with_retry(sandbox_id, ip)
    except Exception as error:
        await fail(sandbox_id, error)
        return False
    provision_ms = int((time.monotonic() - started) * 1000)
    await save_fields(sandbox_id, status="WARM", ip=handle.ip, external_id=handle.external_id,
                extra=handle.extra, provision_ms=provision_ms)
    await record_event("warm_pool.refilled", f"warm sandbox ready at {handle.ip}", ip=handle.ip,
                       boot_ms=provision_ms)
    return True
