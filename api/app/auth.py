# Optional API key for mutating endpoints.
# Responsible for: checking X-API-Key when API_KEY is set (no key configured = open, for local demos).
# NOT responsible for: gateway agent auth (that is X-Agent-Token, see gateways/client.py).
# Serves all criteria (protects create/delete/failover/config endpoints).

from fastapi import Header, HTTPException

from app.settings import settings


async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if settings.api_key and x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="missing or wrong X-API-Key")
