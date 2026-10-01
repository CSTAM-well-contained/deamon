# Database: engine, sessions, and the 4 tables (sandboxes, ip_leases, config_versions, events).
# Responsible for: table definitions and creating them at startup.
# NOT responsible for: queries about a feature — those live in that feature's service.py.
# Serves all criteria (state of sandboxes, IP leases, config history, event log).

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, Text, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.settings import settings

log = logging.getLogger(__name__)

engine = create_async_engine(settings.database_url, pool_size=10, max_overflow=20)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

# A team may own at most one sandbox in these states (enforced by a partial unique index).
TEAM_LIVE_STATUSES = ("PROVISIONING", "ACTIVE", "TEARING_DOWN")
# Rows that still own a VM/container (used for orphan cleanup).
LIVE_STATUSES = ("PROVISIONING", "WARM", "ACTIVE", "TEARING_DOWN")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Parent class of the 4 tables below (SQLAlchemy collects them here for create_all)."""


class Sandbox(Base):
    __tablename__ = "sandboxes"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    team: Mapped[str | None] = mapped_column(String)  # NULL while WARM (nobody owns it yet)
    hostname: Mapped[str | None] = mapped_column(String)
    ip: Mapped[str | None] = mapped_column(String)
    # PROVISIONING | WARM | ACTIVE | TEARING_DOWN | DELETED | FAILED
    status: Mapped[str] = mapped_column(String, index=True)
    driver: Mapped[str] = mapped_column(String)
    external_id: Mapped[str | None] = mapped_column(String)
    extra: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provision_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index(
            "uq_sandboxes_live_team",
            "team",
            unique=True,
            postgresql_where=text("status IN ('PROVISIONING', 'ACTIVE', 'TEARING_DOWN')"),
        ),
    )


class IpLease(Base):
    __tablename__ = "ip_leases"
    ip: Mapped[str] = mapped_column(String, primary_key=True)
    ip_int: Mapped[int] = mapped_column(BigInteger, unique=True)  # for ordering 10.20.0.9 before 10.20.0.10
    # FREE | RESERVED | ALLOCATED | QUARANTINED
    state: Mapped[str] = mapped_column(String, default="FREE", index=True)
    sandbox_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    team: Mapped[str | None] = mapped_column(String)
    leased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quarantined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConfigVersion(Base):
    __tablename__ = "config_versions"
    version: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    config: Mapped[str] = mapped_column(Text)
    checksum: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(String)
    # PENDING | ACTIVE | REJECTED | ROLLED_BACK
    status: Mapped[str] = mapped_column(String)
    results: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    kind: Mapped[str] = mapped_column(String, index=True)
    team: Mapped[str | None] = mapped_column(String)
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSONB, default=dict)


async def init_db(timeout_seconds: int = 60) -> None:
    """Create the tables. Postgres may still be starting, so retry for a while."""
    for attempt in range(timeout_seconds):
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            return
        except (OSError, DBAPIError) as error:  # refused / "starting up" while Postgres boots
            log.info("database not ready (%s), retry %d", error, attempt + 1)
            await asyncio.sleep(1)
    raise RuntimeError("database never became ready")
