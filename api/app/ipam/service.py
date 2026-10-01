# Criterion 3 — IPAM: our own pool of sandbox IPs with short leases.
# Responsible for: seeding the pool, conflict-free allocate(), assign(), release(), quarantine(), summary.
# NOT responsible for: deciding WHEN to release (sandboxes/service.py and ipam/reclaimer.py do).
# Lease life: FREE → RESERVED (allocate) → ALLOCATED (assign to a team) → FREE (release).

import ipaddress
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import IpLease, Sandbox, SessionLocal, utcnow
from app.settings import settings


class PoolExhaustedError(Exception):
    """No FREE IP left in the pool (HTTP 507)."""


def pool_ips() -> list[ipaddress.IPv4Address]:
    first = ipaddress.IPv4Address(settings.pool_start)
    last = ipaddress.IPv4Address(settings.pool_end)
    return [ipaddress.IPv4Address(n) for n in range(int(first), int(last) + 1)]


async def seed_pool() -> None:
    """Insert every pool IP that is not in the table yet (existing leases are left alone)."""
    rows = [{"ip": str(ip), "ip_int": int(ip), "state": "FREE"} for ip in pool_ips()]
    async with SessionLocal() as session:
        await session.execute(pg_insert(IpLease).values(rows).on_conflict_do_nothing())
        await session.commit()


async def allocate(sandbox_id: uuid.UUID) -> str:
    """Reserve one FREE IP for a sandbox. Safe under concurrency:
    FOR UPDATE SKIP LOCKED makes parallel callers skip rows another transaction is taking.
    Least recently released IPs go first, so a just-freed IP is not reused immediately."""
    async with SessionLocal() as session, session.begin():
        query = (
            select(IpLease)
            .where(IpLease.state == "FREE")
            .order_by(IpLease.released_at.asc().nulls_first(), IpLease.ip_int)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        lease = (await session.execute(query)).scalar_one_or_none()
        if lease is None:
            raise PoolExhaustedError("no free IP left in the pool")
        lease.state = "RESERVED"
        lease.sandbox_id = sandbox_id
        lease.team = None
        lease.leased_at = utcnow()
        lease.expires_at = None
        return lease.ip


async def assign(ip: str, team: str, ttl_seconds: int) -> None:
    """The reserved IP now belongs to a team, until expires_at."""
    await _update(ip, state="ALLOCATED", team=team, expires_at=utcnow() + timedelta(seconds=ttl_seconds))


async def set_expiry(ip: str, expires_at: datetime) -> None:
    await _update(ip, expires_at=expires_at)


async def release(ip: str, sandbox_id: uuid.UUID | None = None) -> None:
    """Back to FREE. With sandbox_id, only release if the lease still belongs to that sandbox."""
    await _update(
        ip, sandbox_id_guard=sandbox_id,
        state="FREE", sandbox_id=None, team=None, expires_at=None, released_at=utcnow(),
    )


async def quarantine(ip: str) -> None:
    """The driver says this IP is already used by something else: park it for a while."""
    await _update(ip, state="QUARANTINED", sandbox_id=None, team=None, quarantined_at=utcnow())


async def _update(ip: str, sandbox_id_guard: uuid.UUID | None = None, **values) -> None:
    query = update(IpLease).where(IpLease.ip == ip).values(**values)
    if sandbox_id_guard is not None:
        query = query.where(IpLease.sandbox_id == sandbox_id_guard)
    async with SessionLocal() as session:
        await session.execute(query)
        await session.commit()


async def unquarantine_expired(older_than_seconds: int = 60) -> list[str]:
    """QUARANTINED for longer than the delay → FREE again. Returns the freed IPs."""
    limit = utcnow() - timedelta(seconds=older_than_seconds)
    query = (
        update(IpLease)
        .where(IpLease.state == "QUARANTINED", IpLease.quarantined_at < limit)
        .values(state="FREE", released_at=utcnow(), quarantined_at=None)
        .returning(IpLease.ip)
    )
    async with SessionLocal() as session:
        freed = list((await session.execute(query)).scalars())
        await session.commit()
    return freed


async def release_orphans() -> list[str]:
    """Leases still held although their sandbox is DELETED/FAILED (e.g. crash mid-teardown),
    or held for over a minute by a sandbox id that has no row at all."""
    one_minute_ago = utcnow() - timedelta(minutes=1)
    dead = select(Sandbox.id).where(Sandbox.status.in_(("DELETED", "FAILED")))
    known = select(Sandbox.id)
    query = (
        update(IpLease)
        .where(IpLease.state.in_(("RESERVED", "ALLOCATED")))
        .where(
            IpLease.sandbox_id.in_(dead)
            | (IpLease.sandbox_id.not_in(known) & (IpLease.leased_at < one_minute_ago))
        )
        .values(state="FREE", sandbox_id=None, team=None, expires_at=None, released_at=utcnow())
        .returning(IpLease.ip)
    )
    async with SessionLocal() as session:
        freed = list((await session.execute(query)).scalars())
        await session.commit()
    return freed


async def summary() -> dict:
    async with SessionLocal() as session:
        rows = (await session.execute(select(IpLease.state, func.count()).group_by(IpLease.state))).all()
    counts = {"FREE": 0, "RESERVED": 0, "ALLOCATED": 0, "QUARANTINED": 0} | dict(rows)
    size = sum(counts.values())
    used = size - counts["FREE"]
    return {
        "pool_start": settings.pool_start,
        "pool_end": settings.pool_end,
        "size": size,
        "counts": counts,
        "utilisation_percent": round(100 * used / size, 1) if size else 0.0,
    }


async def held_leases() -> list[dict]:
    """Every lease that is not FREE, lowest IP first."""
    async with SessionLocal() as session:
        query = select(IpLease).where(IpLease.state != "FREE").order_by(IpLease.ip_int)
        leases = (await session.execute(query)).scalars().all()
    return [
        {
            "ip": lease.ip, "state": lease.state, "team": lease.team, "sandbox_id": lease.sandbox_id,
            "leased_at": lease.leased_at, "expires_at": lease.expires_at, "quarantined_at": lease.quarantined_at,
        }
        for lease in leases
    ]
