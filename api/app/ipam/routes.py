# Criterion 3 — HTTP endpoints for the IP pool.
# Responsible for: GET /api/ipam (pool summary + every lease that is not FREE). HTTP only.
# NOT responsible for: allocation logic (ipam/service.py).

from fastapi import APIRouter

from app.ipam import service

router = APIRouter(tags=["ipam"])


@router.get("/api/ipam")
async def get_ipam():
    return {"summary": await service.summary(), "leases": await service.held_leases()}
