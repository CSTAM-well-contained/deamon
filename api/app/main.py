# Control plane entry point: builds the FastAPI app, mounts every router, starts background jobs.
# Responsible for: startup order (tables → IP pool → jobs → first gateway sync), CORS, /healthz.
# NOT responsible for: any feature logic — see sandboxes/, ipam/, gateways/.
# Serves all criteria.

import asyncio
import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import events
from app.database import init_db, utcnow
from app.gateways import monitor
from app.gateways import routes as gateway_routes
from app.ipam import reclaimer
from app.ipam import routes as ipam_routes
from app.ipam import service as ipam
from app.sandboxes import routes as sandbox_routes
from app.sandboxes import warm_pool
from app.settings import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per agent call is too noisy
logging.getLogger("apscheduler").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await ipam.seed_pool()
    app.state.gateways = {}

    # max_instances=1: a slow run is skipped, never doubled. next_run_time=now: start immediately.
    scheduler = AsyncIOScheduler()
    jobs = [
        (warm_pool.refill, settings.job_interval_seconds, []),
        (reclaimer.run, settings.job_interval_seconds, []),
        (monitor.tick, settings.monitor_interval_seconds, [app]),
    ]
    for job, seconds, args in jobs:
        scheduler.add_job(job, "interval", seconds=seconds, args=args, max_instances=1,
                          coalesce=True, next_run_time=utcnow())
    scheduler.start()
    first_sync = asyncio.create_task(monitor.initial_sync(app))
    yield
    first_sync.cancel()
    scheduler.shutdown(wait=False)


app = FastAPI(title="CSTAM Sandbox Platform", version="1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(sandbox_routes.router)
app.include_router(ipam_routes.router)
app.include_router(gateway_routes.router)
app.include_router(events.router)


@app.get("/healthz", tags=["health"])
async def healthz():
    return {"ok": True}
