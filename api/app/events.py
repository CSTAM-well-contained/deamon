# The event log: one helper to write an event, one endpoint to read them.
# Responsible for: record_event() (used by every feature on important state changes) + GET /api/events.
# NOT responsible for: deciding which changes matter — callers decide.
# Serves all criteria (the demo and the dashboard read this table as the platform's history).

import json
import logging

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.database import Event, SessionLocal

log = logging.getLogger("events")
router = APIRouter(tags=["events"])


async def record_event(kind: str, message: str, team: str | None = None, **data) -> None:
    """Write one row in `events`. Never raises: losing a log line must not break a provisioning."""
    safe_data = json.loads(json.dumps(data, default=str))  # UUIDs, datetimes → strings
    log.info("[%s] %s %s", kind, message, safe_data or "")
    try:
        async with SessionLocal() as session:
            session.add(Event(kind=kind, team=team, message=message, data=safe_data))
            await session.commit()
    except Exception:
        log.exception("could not record event %s", kind)


@router.get("/api/events")
async def list_events(limit: int = Query(100, ge=1, le=1000), kind: str | None = None):
    query = select(Event).order_by(Event.id.desc()).limit(limit)
    if kind:
        query = query.where(Event.kind == kind)
    async with SessionLocal() as session:
        rows = (await session.execute(query)).scalars().all()
    return [
        {"id": e.id, "ts": e.ts, "kind": e.kind, "team": e.team, "message": e.message, "data": e.data}
        for e in rows
    ]
