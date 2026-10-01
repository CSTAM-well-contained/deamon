# Criterion 1 — HTTP endpoints for sandboxes and the warm pool.
# Responsible for: validating input, calling service.py, mapping errors to HTTP codes. HTTP only.
# NOT responsible for: any provisioning logic (service.py, warm_pool.py).

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field

from app.auth import require_api_key
from app.ipam.service import PoolExhaustedError
from app.sandboxes import service, warm_pool

router = APIRouter(tags=["sandboxes"])


class CreateSandbox(BaseModel):
    team: str = Field(examples=["team1"])
    ttl_seconds: int | None = Field(default=None, gt=0, examples=[7200])


class RenewSandbox(BaseModel):
    ttl_seconds: int = Field(gt=0, examples=[3600])


def http_error(error: Exception) -> HTTPException:
    codes = {
        service.InvalidRequestError: 422,
        service.TeamConflictError: 409,
        service.SandboxNotFoundError: 404,
        PoolExhaustedError: 507,
    }
    return HTTPException(status_code=codes.get(type(error), 500), detail=str(error))


@router.post("/api/sandboxes", dependencies=[Depends(require_api_key)],
             responses={201: {"description": "claimed from the warm pool"}, 202: {"description": "cold build started"}})
async def create_sandbox(body: CreateSandbox):
    try:
        sandbox, from_warm_pool = await service.claim_or_create(body.team, body.ttl_seconds)
    except (service.InvalidRequestError, service.TeamConflictError, PoolExhaustedError) as error:
        raise http_error(error)
    return JSONResponse(status_code=201 if from_warm_pool else 202, content=jsonable_encoder(sandbox))


@router.get("/api/sandboxes")
async def list_sandboxes(all: bool = False):
    return await service.list_sandboxes(include_finished=all)


@router.get("/api/sandboxes/{team}")
async def get_sandbox(team: str):
    try:
        return await service.get_sandbox(team)
    except service.SandboxNotFoundError as error:
        raise http_error(error)


@router.delete("/api/sandboxes/{team}", status_code=202, dependencies=[Depends(require_api_key)])
async def delete_sandbox(team: str):
    try:
        return await service.teardown(team, reason="api_delete")
    except service.SandboxNotFoundError as error:
        raise http_error(error)


@router.post("/api/sandboxes/{team}/renew", dependencies=[Depends(require_api_key)])
async def renew_sandbox(team: str, body: RenewSandbox):
    try:
        return await service.renew(team, body.ttl_seconds)
    except (service.InvalidRequestError, service.SandboxNotFoundError) as error:
        raise http_error(error)


@router.get("/api/warm-pool")
async def get_warm_pool():
    return await warm_pool.counts()
