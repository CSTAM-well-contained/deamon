# Shared pytest setup for the API tests.
# Responsible for: pointing the app at a separate test database (cstam_test) BEFORE app modules load,
# creating that database and its tables, and closing DB connections after each test.
# NOT responsible for: test logic. Serves Criteria 3 + 4 tests.

import asyncio
import os

import asyncpg
import pytest

MAIN_URL = os.environ.get("DATABASE_URL", "postgresql+asyncpg://cstam:cstam@10.20.0.5:5432/cstam")
SERVER_URL, _, _ = MAIN_URL.rpartition("/")
os.environ["DATABASE_URL"] = f"{SERVER_URL}/cstam_test"  # must happen before `import app...`


async def _create_test_database() -> None:
    conn = await asyncpg.connect(SERVER_URL.replace("+asyncpg", "") + "/postgres")
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = 'cstam_test'")
        if not exists:
            await conn.execute("CREATE DATABASE cstam_test")
    finally:
        await conn.close()


async def _create_tables() -> None:
    from app.database import engine, init_db

    await init_db()
    await engine.dispose()


def pytest_configure(config):
    asyncio.run(_create_test_database())
    asyncio.run(_create_tables())


@pytest.fixture(autouse=True)
async def dispose_engine():
    """Each test has its own event loop; pooled connections must not leak into the next one."""
    yield
    from app.database import engine

    await engine.dispose()
