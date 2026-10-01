# Criterion 3 — IPAM tests against a real Postgres (row locks are what we are testing).
# Responsible for: proving parallel allocations never hand out the same IP, release/reuse order,
# and the "pool exhausted" error. NOT responsible for: sandboxes or drivers.

import asyncio
import uuid

import pytest
from sqlalchemy import delete, select, update

from app.database import IpLease, SessionLocal
from app.ipam import service


@pytest.fixture(autouse=True)
async def fresh_pool():
    async with SessionLocal() as session:
        await session.execute(delete(IpLease))
        await session.commit()
    await service.seed_pool()


async def test_30_parallel_allocations_get_30_distinct_ips():
    ids = [uuid.uuid4() for _ in range(30)]
    ips = await asyncio.gather(*(service.allocate(sandbox_id) for sandbox_id in ids))
    assert len(set(ips)) == 30

    async with SessionLocal() as session:
        reserved = (await session.execute(select(IpLease).where(IpLease.state == "RESERVED"))).scalars().all()
    assert {lease.ip for lease in reserved} == set(ips)
    assert {lease.sandbox_id for lease in reserved} == set(ids)


async def test_assign_release_and_reuse_order():
    first = await service.allocate(uuid.uuid4())
    await service.assign(first, "team1", ttl_seconds=60)
    await service.release(first)

    # Never-used IPs (released_at NULL) go before a just-released one.
    second = await service.allocate(uuid.uuid4())
    assert second != first
    summary = await service.summary()
    assert summary["counts"]["RESERVED"] == 1
    assert summary["size"] == len(service.pool_ips())


async def test_release_with_wrong_owner_does_nothing():
    owner = uuid.uuid4()
    ip = await service.allocate(owner)
    await service.release(ip, sandbox_id=uuid.uuid4())
    assert (await service.summary())["counts"]["RESERVED"] == 1
    await service.release(ip, sandbox_id=owner)
    assert (await service.summary())["counts"]["RESERVED"] == 0


async def test_exhausted_pool_raises():
    async with SessionLocal() as session:
        await session.execute(update(IpLease).values(state="ALLOCATED"))
        await session.commit()
    with pytest.raises(service.PoolExhaustedError):
        await service.allocate(uuid.uuid4())


async def test_quarantine_is_freed_after_delay():
    ip = await service.allocate(uuid.uuid4())
    await service.quarantine(ip)
    assert await service.unquarantine_expired(older_than_seconds=60) == []
    assert await service.unquarantine_expired(older_than_seconds=0) == [ip]
