# Criteria 2 + 4 — HTTP endpoints for gateways, routes and config versions.
# Responsible for: input validation and calling client / route_sync / config_sync. HTTP only.
# NOT responsible for: rollout order (client.py), config pipeline (config_sync.py), polling (monitor.py).

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.auth import require_api_key
from app.gateways import config_sync, route_sync
from app.gateways.client import get_client

router = APIRouter(tags=["gateways"])


class RollbackRequest(BaseModel):
    version: int


class InjectBadRequest(BaseModel):
    mode: Literal["syntax", "semantic"]


@router.get("/api/gateways")
async def list_gateways(request: Request):
    gateways = request.app.state.gateways
    masters = [name for name, gw in gateways.items() if gw.get("vrrp_state") == "MASTER"]
    return {"master": masters[0] if masters else None, "gateways": gateways}


async def _agent_call(name: str, path: str) -> dict:
    if name not in get_client().gateways:
        raise HTTPException(status_code=404, detail=f"unknown gateway {name}")
    result = await get_client().call(name, "POST", path)
    if result["outcome"] in ("unreachable", "error"):
        raise HTTPException(status_code=502, detail=result)
    return {"gateway": name} | result


@router.post("/api/gateways/{name}/failover", dependencies=[Depends(require_api_key)])
async def force_failover(name: str):
    return await _agent_call(name, "/failover")


@router.post("/api/gateways/{name}/failover/clear", dependencies=[Depends(require_api_key)])
async def clear_failover(name: str):
    return await _agent_call(name, "/failover/clear")


@router.get("/api/routes")
async def get_routes():
    return route_sync.current()


@router.get("/api/config/versions")
async def config_versions(full: bool = False):
    return await config_sync.versions(full)


@router.post("/api/config/apply", dependencies=[Depends(require_api_key)])
async def apply_config():
    return await config_sync.apply("manual apply")


@router.post("/api/config/rollback", dependencies=[Depends(require_api_key)])
async def rollback_config(body: RollbackRequest):
    try:
        return await config_sync.rollback(body.version)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error))


@router.post("/api/config/inject-bad", dependencies=[Depends(require_api_key)])
async def inject_bad_config(body: InjectBadRequest):
    return await config_sync.inject_bad(body.mode)
