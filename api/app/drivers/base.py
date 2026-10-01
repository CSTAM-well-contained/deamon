# The SandboxDriver interface: how a sandbox is physically created, labelled and destroyed.
# Responsible for: the contract every driver follows (Docker locally, OpenStack in the cloud).
# NOT responsible for: IPs (the IPAM picks them and passes them in) or routes (gateways/).
# Serves Criterion 1 — sandbox provisioning.

from dataclasses import dataclass, field
from typing import Protocol


class IpConflictError(Exception):
    """The IP we were given is already used by something outside our IPAM (it gets quarantined)."""


@dataclass
class SandboxHandle:
    external_id: str  # container id / Nova server id
    ip: str
    extra: dict = field(default_factory=dict)  # e.g. {"port_id": ..., "secgroup_id": ...}


class SandboxDriver(Protocol):
    name: str

    async def create(self, sandbox_id: str, ip: str) -> SandboxHandle:
        """Start a sandbox with exactly this private IP. Clean up after itself if it fails."""

    async def assign(self, handle: SandboxHandle, team: str, hostname: str) -> None:
        """Mark the sandbox as owned by a team (welcome page / metadata)."""

    async def delete(self, external_id: str | None, extra: dict) -> None:
        """Remove everything the sandbox owns. Idempotent: deleting twice is fine."""

    async def list_managed(self) -> list[dict]:
        """[{sandbox_id, external_id, ip}] for everything this platform created (orphan cleanup)."""
