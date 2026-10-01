# Criterion 1 — sandbox provisioning logic.
# Responsible for: claim_or_create() (warm path 201 / cold path 202), teardown(), renew(), failure cleanup,
# and create_with_retry() shared with the warm pool.
# NOT responsible for: HTTP (routes.py), keeping the warm pool full (warm_pool.py), IP choice (ipam/).

import asyncio
import logging
import re
import time
import uuid
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.database import TEAM_LIVE_STATUSES, Sandbox, SessionLocal, utcnow
from app.drivers import get_driver
from app.drivers.base import IpConflictError, SandboxHandle
from app.events import record_event
from app.gateways import route_sync
from app.ipam import service as ipam
from app.settings import settings

log = logging.getLogger("sandboxes")
TEAM_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")
_background_tasks: set[asyncio.Task] = set()  # keep references so tasks are not garbage-collected


class InvalidRequestError(ValueError):
    """Bad team name or TTL (HTTP 422)."""


class TeamConflictError(Exception):
    """The team already has a live sandbox (HTTP 409)."""


class SandboxNotFoundError(LookupError):
    """No sandbox for this team (HTTP 404)."""


def to_json(sandbox: Sandbox) -> dict:
    return {
        "id": sandbox.id, "team": sandbox.team, "hostname": sandbox.hostname,
        "url": f"http://{sandbox.hostname}/" if sandbox.hostname else None,
        "ip": sandbox.ip, "status": sandbox.status, "driver": sandbox.driver,
        "created_at": sandbox.created_at, "expires_at": sandbox.expires_at,
        "provision_ms": sandbox.provision_ms, "error": sandbox.error,
    }


def check_ttl(ttl_seconds: int | None) -> int:
    ttl = ttl_seconds or settings.default_ttl_seconds
    if not 0 < ttl <= settings.max_ttl_seconds:
        raise InvalidRequestError(f"ttl_seconds must be between 1 and {settings.max_ttl_seconds}")
    return ttl


async def claim_or_create(team: str, ttl_seconds: int | None) -> tuple[dict, bool]:
    """Give `team` a sandbox. Returns (sandbox json, True if served from the warm pool)."""
    if not TEAM_PATTERN.match(team):
        raise InvalidRequestError("team must match ^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")
    ttl = check_ttl(ttl_seconds)
    started = time.monotonic()
    hostname = f"{team}.{settings.base_domain}"
    try:
        async with SessionLocal() as session, session.begin():
            if await _live_sandbox(session, team):
                raise TeamConflictError(f"team {team} already has a sandbox")
            warm = await _lock_one_warm(session)
            sandbox = warm or Sandbox(status="PROVISIONING", driver=get_driver().name, extra={})
            sandbox.team, sandbox.hostname = team, hostname
            sandbox.expires_at = utcnow() + timedelta(seconds=ttl)
            if warm:
                sandbox.status, sandbox.claimed_at = "ACTIVE", utcnow()
            session.add(sandbox)
    except IntegrityError as error:  # two requests for the same team at the same moment
        raise TeamConflictError(f"team {team} already has a sandbox") from error

    if warm:
        return await _finish_warm_claim(sandbox, ttl, started), True
    await _start_cold(sandbox, ttl, started)
    return to_json(sandbox), False


async def _live_sandbox(session, team: str, for_update: bool = False) -> Sandbox | None:
    query = select(Sandbox).where(Sandbox.team == team, Sandbox.status.in_(TEAM_LIVE_STATUSES))
    if for_update:
        query = query.with_for_update()
    return (await session.execute(query)).scalar_one_or_none()


async def _lock_one_warm(session) -> Sandbox | None:
    """SKIP LOCKED: two teams claiming at once get two different warm sandboxes."""
    query = (
        select(Sandbox).where(Sandbox.status == "WARM").order_by(Sandbox.created_at)
        .limit(1).with_for_update(skip_locked=True)
    )
    return (await session.execute(query)).scalar_one_or_none()


async def _finish_warm_claim(sandbox: Sandbox, ttl: int, started: float) -> dict:
    try:
        await ipam.assign(sandbox.ip, sandbox.team, ttl)
        handle = SandboxHandle(sandbox.external_id, sandbox.ip, sandbox.extra)
        await get_driver().assign(handle, sandbox.team, sandbox.hostname)
        await route_sync.push()
    except Exception as error:
        await fail(sandbox.id, error)
        raise
    sandbox.provision_ms = int((time.monotonic() - started) * 1000)
    await save_fields(sandbox.id, provision_ms=sandbox.provision_ms)
    await record_event("sandbox.claimed", f"{sandbox.team} got warm sandbox {sandbox.ip}", team=sandbox.team,
                       ip=sandbox.ip, provision_ms=sandbox.provision_ms)
    return to_json(sandbox)


async def _start_cold(sandbox: Sandbox, ttl: int, started: float) -> None:
    """Reserve an IP now (so 'pool exhausted' is answered synchronously), build the VM in the background."""
    try:
        sandbox.ip = await ipam.allocate(sandbox.id)
    except ipam.PoolExhaustedError as error:
        await fail(sandbox.id, error)
        raise
    await save_fields(sandbox.id, ip=sandbox.ip)
    task = asyncio.create_task(_provision_cold(sandbox.id, sandbox.ip, sandbox.team, sandbox.hostname, ttl, started))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _provision_cold(sandbox_id, ip: str, team: str, hostname: str, ttl: int, started: float) -> None:
    try:
        handle = await create_with_retry(sandbox_id, ip)
        provision_ms = int((time.monotonic() - started) * 1000)
        activated = await save_fields(
            sandbox_id, only_if_status="PROVISIONING", status="ACTIVE", ip=handle.ip,
            external_id=handle.external_id, extra=handle.extra, claimed_at=utcnow(), provision_ms=provision_ms,
        )
        if not activated:  # deleted while we were building it
            await get_driver().delete(handle.external_id, handle.extra)
            await ipam.release(handle.ip, sandbox_id)
            return
        await ipam.assign(handle.ip, team, ttl)
        await get_driver().assign(handle, team, hostname)
        await route_sync.push()
        await record_event("sandbox.active", f"{team} got cold sandbox {handle.ip}", team=team,
                           ip=handle.ip, provision_ms=provision_ms)
    except Exception as error:
        await fail(sandbox_id, error)


async def create_with_retry(sandbox_id: uuid.UUID, ip: str, attempts: int = 3) -> SandboxHandle:
    """driver.create(); if the IP turns out to be taken outside our IPAM, quarantine it and try another."""
    for attempt in range(1, attempts + 1):
        try:
            return await get_driver().create(str(sandbox_id), ip)
        except IpConflictError as error:
            await ipam.quarantine(ip)
            await record_event("ipam.conflict", f"{ip} already in use, quarantined", ip=ip, error=str(error))
            if attempt == attempts:
                raise
            ip = await ipam.allocate(sandbox_id)
            await save_fields(sandbox_id, ip=ip)
    raise RuntimeError("unreachable")  # the loop always returns or raises


async def fail(sandbox_id: uuid.UUID, error: Exception | str) -> None:
    """Any failure: remove what exists (best effort), give the IP back, mark FAILED."""
    async with SessionLocal() as session:
        sandbox = await session.get(Sandbox, sandbox_id)
    if sandbox is None:
        return
    try:
        await get_driver().delete(sandbox.external_id, sandbox.extra or {})
    except Exception:
        log.exception("cleanup of failed sandbox %s", sandbox_id)
    if sandbox.ip:
        await ipam.release(sandbox.ip, sandbox_id)
    await save_fields(sandbox_id, status="FAILED", error=str(error) or type(error).__name__, deleted_at=utcnow())
    if sandbox.status == "ACTIVE":
        await route_sync.push()
    await record_event("sandbox.failed", f"sandbox {sandbox_id} failed: {error}", team=sandbox.team, ip=sandbox.ip)


async def teardown(team: str, reason: str) -> dict:
    """Idempotent: TEARING_DOWN → remove the route FIRST → delete the VM → free the IP → DELETED."""
    started = time.monotonic()
    async with SessionLocal() as session, session.begin():
        sandbox = await _live_sandbox(session, team, for_update=True)
        if sandbox is None:
            raise SandboxNotFoundError(f"team {team} has no live sandbox")
        sandbox.status = "TEARING_DOWN"

    await route_sync.push()  # users stop reaching it before it disappears
    try:
        await get_driver().delete(sandbox.external_id, sandbox.extra or {})
    except Exception as error:
        await fail(sandbox.id, f"teardown failed: {error}")
        raise
    if sandbox.ip:
        await ipam.release(sandbox.ip, sandbox.id)
    sandbox.status, sandbox.deleted_at = "DELETED", utcnow()
    await save_fields(sandbox.id, status="DELETED", deleted_at=sandbox.deleted_at)
    teardown_ms = int((time.monotonic() - started) * 1000)
    await record_event("sandbox.deleted", f"{team} sandbox deleted ({reason})", team=team,
                       ip=sandbox.ip, reason=reason, teardown_ms=teardown_ms)
    return to_json(sandbox) | {"teardown_ms": teardown_ms}


async def renew(team: str, ttl_seconds: int) -> dict:
    ttl = check_ttl(ttl_seconds)
    async with SessionLocal() as session, session.begin():
        sandbox = await _live_sandbox(session, team, for_update=True)
        if sandbox is None or sandbox.status != "ACTIVE":
            raise SandboxNotFoundError(f"team {team} has no ACTIVE sandbox")
        sandbox.expires_at = utcnow() + timedelta(seconds=ttl)
    await ipam.set_expiry(sandbox.ip, sandbox.expires_at)
    await record_event("sandbox.renewed", f"{team} renewed until {sandbox.expires_at:%H:%M:%S}", team=team,
                       expires_at=sandbox.expires_at)
    return to_json(sandbox)


async def save_fields(sandbox_id: uuid.UUID, only_if_status: str | None = None, **values) -> bool:
    """UPDATE one sandbox row. Returns False if `only_if_status` did not match."""
    query = update(Sandbox).where(Sandbox.id == sandbox_id).values(**values)
    if only_if_status:
        query = query.where(Sandbox.status == only_if_status)
    async with SessionLocal() as session:
        result = await session.execute(query)
        await session.commit()
    return result.rowcount == 1


async def list_sandboxes(include_finished: bool) -> list[dict]:
    query = select(Sandbox).where(Sandbox.team.is_not(None)).order_by(Sandbox.created_at)
    if not include_finished:
        query = query.where(Sandbox.status.in_(TEAM_LIVE_STATUSES))
    async with SessionLocal() as session:
        return [to_json(s) for s in (await session.execute(query)).scalars()]


async def get_sandbox(team: str) -> dict:
    """The team's newest sandbox (live, or the last DELETED/FAILED one)."""
    query = select(Sandbox).where(Sandbox.team == team).order_by(Sandbox.created_at.desc()).limit(1)
    async with SessionLocal() as session:
        sandbox = (await session.execute(query)).scalar_one_or_none()
    if sandbox is None:
        raise SandboxNotFoundError(f"team {team} has no sandbox")
    return to_json(sandbox)
