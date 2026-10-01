# Criteria 2 + 4 — HTTP client for the gateway agents (gw-agent on :9000).
# Responsible for: calling agents with X-Agent-Token, and the ROLLOUT ORDER: BACKUP(s) first, MASTER last;
# if a gateway rejects or rolls back a change, STOP — the master never receives it.
# NOT responsible for: building what is sent (route_sync.py / config_sync.py) or retrying later (monitor.py).

import asyncio
from functools import cache

import httpx

from app.settings import settings

TIMEOUT_SECONDS = 5.0
# Any of these from a gateway means "this change is bad": do not send it to the next gateway.
STOP_OUTCOMES = {"rejected", "rolled_back", "error"}


class GatewayClient:
    def __init__(self, gateways: dict[str, str], token: str, transport: httpx.AsyncBaseTransport | None = None):
        self.gateways = gateways  # {"gw1": "http://10.20.0.11:9000", ...}
        # `transport` lets tests replace the network with fake agents.
        self._http = httpx.AsyncClient(
            timeout=TIMEOUT_SECONDS, headers={"X-Agent-Token": token}, transport=transport
        )

    async def call(self, name: str, method: str, path: str, payload: dict | None = None) -> dict:
        """One request to one agent. Never raises: problems come back as an `outcome`."""
        try:
            response = await self._http.request(method, self.gateways[name] + path, json=payload)
        except httpx.HTTPError as error:
            return {"outcome": "unreachable", "error": str(error) or type(error).__name__}
        try:
            body = response.json()
        except ValueError:
            body = {"error": response.text}
        if not isinstance(body, dict):
            body = {"body": body}
        body.setdefault("outcome", "ok" if response.is_success else "error")
        body["http_status"] = response.status_code
        return body

    async def status(self, name: str) -> dict | None:
        """GET /status of one agent, or None if it does not answer properly."""
        result = await self.call(name, "GET", "/status")
        return None if result["outcome"] in ("unreachable", "error") else result

    async def statuses(self) -> dict[str, dict | None]:
        names = list(self.gateways)
        results = await asyncio.gather(*(self.status(name) for name in names))
        return dict(zip(names, results))

    @staticmethod
    def rollout_order(statuses: dict[str, dict | None]) -> list[str]:
        """Everything that is not MASTER first (BACKUP, FAULT, UNKNOWN...), the MASTER last."""
        def is_master(name: str) -> bool:
            return (statuses.get(name) or {}).get("vrrp_state") == "MASTER"
        return sorted(statuses, key=is_master)

    async def rollout(self, method: str, path: str, payload: dict) -> dict[str, dict]:
        """Send the same change to every gateway, backup first. Results keep the order they were sent in."""
        statuses = await self.statuses()
        results: dict[str, dict] = {}
        failed_on: str | None = None
        for name in self.rollout_order(statuses):
            if failed_on:
                vrrp_state = (statuses[name] or {}).get("vrrp_state", "UNREACHABLE")
                results[name] = {"outcome": "skipped", "vrrp_state": vrrp_state,
                                 "error": f"not sent: {failed_on} refused it first"}
                continue
            if statuses[name] is None:
                # Recorded here; the monitor pushes the change once the gateway is back.
                results[name] = {"outcome": "unreachable", "vrrp_state": "UNREACHABLE"}
                continue
            result = await self.call(name, method, path, payload)
            result["vrrp_state"] = statuses[name]["vrrp_state"]
            results[name] = result
            if result["outcome"] in STOP_OUTCOMES:
                failed_on = name
        return results


@cache
def get_client() -> GatewayClient:
    """The one client the app uses (created on first use, from settings)."""
    return GatewayClient(settings.gateway_urls(), settings.agent_token)
