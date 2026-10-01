# Criteria 2 + 4 — rollout order: the BACKUP gets every change before the MASTER,
# and a change the backup rejects / rolls back is never sent to the master.
# Responsible for: testing gateways/client.py with fake agents (httpx.MockTransport), no network.
# NOT responsible for: the agent's own pipeline (cargo tests in agent/).

import json

import httpx

from app.gateways.client import GatewayClient

GATEWAYS = {"gw1": "http://gw1:9000", "gw2": "http://gw2:9000"}


def fake_agents(vrrp: dict[str, str], config_outcome: dict[str, str], calls: list[str]):
    """Each fake agent answers /status with its VRRP state and /config with a chosen outcome."""
    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.host
        assert request.headers["X-Agent-Token"] == "secret"
        if request.url.path == "/status":
            return httpx.Response(200, json={"gw_name": name, "vrrp_state": vrrp[name]})
        calls.append(name)
        if name not in config_outcome:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"outcome": config_outcome[name], "body": json.loads(request.content)})
    return httpx.MockTransport(handler)


async def test_backup_is_updated_before_master():
    calls: list[str] = []
    transport = fake_agents({"gw1": "MASTER", "gw2": "BACKUP"}, {"gw1": "applied", "gw2": "applied"}, calls)
    client = GatewayClient(GATEWAYS, "secret", transport=transport)
    results = await client.rollout("POST", "/config", {"version": 1})
    assert calls == ["gw2", "gw1"]
    assert list(results) == ["gw2", "gw1"]
    assert all(r["outcome"] == "applied" for r in results.values())


async def test_order_follows_vrrp_state_not_names():
    calls: list[str] = []
    transport = fake_agents({"gw1": "BACKUP", "gw2": "MASTER"}, {"gw1": "applied", "gw2": "applied"}, calls)
    await GatewayClient(GATEWAYS, "secret", transport=transport).rollout("PUT", "/routes", {})
    assert calls == ["gw1", "gw2"]


async def test_rejected_on_backup_stops_rollout():
    for bad in ("rejected", "rolled_back"):
        calls: list[str] = []
        transport = fake_agents({"gw1": "MASTER", "gw2": "BACKUP"}, {"gw1": "applied", "gw2": bad}, calls)
        results = await GatewayClient(GATEWAYS, "secret", transport=transport).rollout("POST", "/config", {})
        assert calls == ["gw2"], "the master must never receive a change the backup refused"
        assert results["gw2"]["outcome"] == bad
        assert results["gw1"]["outcome"] == "skipped"


async def test_unreachable_gateway_is_recorded_and_rollout_continues():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "gw2":
            raise httpx.ConnectError("connection refused")
        if request.url.path == "/status":
            return httpx.Response(200, json={"vrrp_state": "MASTER"})
        return httpx.Response(200, json={"outcome": "applied"})

    client = GatewayClient(GATEWAYS, "secret", transport=httpx.MockTransport(handler))
    results = await client.rollout("PUT", "/routes", {})
    assert results["gw2"]["outcome"] == "unreachable"
    assert results["gw1"]["outcome"] == "applied"
