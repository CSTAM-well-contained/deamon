# Criterion 4b — full HAProxy config changes: render, version, store, push (backup first).
# Responsible for: rendering haproxy.cfg.j2, the config_versions table, apply / rollback / inject_bad,
# and re-sending the latest ACTIVE config to a gateway that is behind.
# NOT responsible for: validate/reload/probe/rollback ON the gateway (gw-agent release.rs does it).

import asyncio
import hashlib
from pathlib import Path
from typing import Callable

import jinja2
from sqlalchemy import select

from app.database import ConfigVersion, SessionLocal
from app.events import record_event
from app.gateways.client import get_client

_templates = jinja2.Environment(
    loader=jinja2.FileSystemLoader(Path(__file__).parent),
    undefined=jinja2.StrictUndefined,
    keep_trailing_newline=True,
)
_lock = asyncio.Lock()  # one config release at a time

BAD_SYNTAX_LINE = "this is not haproxy {{{"
BAD_SEMANTIC_PORT = 8099  # valid config, but nobody listens on :80 any more → probe fails


def render(version: int, bind_port: int = 80) -> str:
    return _templates.get_template("haproxy.cfg.j2").render(version=version, bind_port=bind_port)


def checksum(config: str) -> str:
    return hashlib.sha256(config.encode()).hexdigest()


def restamp(config: str, old: int, new: int) -> str:
    """Re-use an old config under a new version number (the version appears in 4 known places)."""
    for template in ("Criteria 2 + 4 (version {})", "# config version {}\n", 'string "{}" if', 'X-Config-Version "{}"'):
        config = config.replace(template.format(old), template.format(new))
    return config


async def apply(reason: str) -> dict:
    """Render the template with the next version and roll it out."""
    return await _release(reason, lambda version: render(version))


async def rollback(version: int) -> dict:
    """Re-push an old ACTIVE config as a NEW version (versions only move forward)."""
    async with SessionLocal() as session:
        old = await session.get(ConfigVersion, version)
    if old is None or old.status != "ACTIVE":
        raise LookupError(f"config v{version} does not exist or was never ACTIVE")
    return await _release(f"rollback to v{version}", lambda new: restamp(old.config, version, new))


async def inject_bad(mode: str) -> dict:
    """Demo: prove that a broken config never breaks traffic.
    syntax   → the backup's `haproxy -c` rejects it; the master never receives it.
    semantic → passes `haproxy -c`, but the probe fails after reload; the backup rolls back in < 1 s."""
    if mode == "syntax":
        return await _release("inject-bad: syntax", lambda v: render(v) + f"\n{BAD_SYNTAX_LINE}\n")
    if mode == "semantic":
        return await _release("inject-bad: semantic", lambda v: render(v, bind_port=BAD_SEMANTIC_PORT))
    raise ValueError("mode must be 'syntax' or 'semantic'")


async def _release(reason: str, make_config: Callable[[int], str]) -> dict:
    async with _lock:
        async with SessionLocal() as session:
            row = ConfigVersion(config="", checksum="", reason=reason, status="PENDING", results={})
            session.add(row)
            await session.flush()  # the database picks the next version number
            row.config = make_config(row.version)
            row.checksum = checksum(row.config)
            await session.commit()

        payload = {"version": row.version, "config": row.config, "checksum": row.checksum}
        results = await get_client().rollout("POST", "/config", payload)
        status = _status_from(results)

        async with SessionLocal() as session:
            saved = await session.get(ConfigVersion, row.version)
            saved.status, saved.results = status, results
            await session.commit()

    kind = {"ACTIVE": "config.applied", "REJECTED": "config.rejected", "ROLLED_BACK": "config.rolled_back"}[status]
    await record_event(kind, f"config v{row.version} ({reason}) → {status}", version=row.version, results=results)
    return {"version": row.version, "reason": reason, "status": status, "results": results}


def _status_from(results: dict[str, dict]) -> str:
    outcomes = {result["outcome"] for result in results.values()}
    if "rolled_back" in outcomes:
        return "ROLLED_BACK"
    if outcomes & {"rejected", "error"}:
        return "REJECTED"
    # applied / unchanged / unreachable: unreachable gateways get it later from the monitor.
    return "ACTIVE"


async def latest_active() -> ConfigVersion | None:
    query = select(ConfigVersion).where(ConfigVersion.status == "ACTIVE").order_by(ConfigVersion.version.desc())
    async with SessionLocal() as session:
        return (await session.execute(query.limit(1))).scalar_one_or_none()


async def resync(name: str, agent_config_version: int | None) -> dict | None:
    """Monitor: a gateway runs another version than the latest ACTIVE one → send it. None = nothing to do."""
    if _lock.locked():
        return None  # a release is in progress; versions are expected to differ for a moment
    row = await latest_active()
    if row is None or agent_config_version == row.version:
        return None
    payload = {"version": row.version, "config": row.config, "checksum": row.checksum}
    async with _lock:
        return await get_client().call(name, "POST", "/config", payload)


async def versions(full: bool) -> list[dict]:
    async with SessionLocal() as session:
        rows = (await session.execute(select(ConfigVersion).order_by(ConfigVersion.version.desc()))).scalars().all()
    return [
        {
            "version": row.version, "status": row.status, "reason": row.reason, "checksum": row.checksum,
            "created_at": row.created_at, "results": row.results, **({"config": row.config} if full else {}),
        }
        for row in rows
    ]
