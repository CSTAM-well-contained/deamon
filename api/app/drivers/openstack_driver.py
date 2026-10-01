# Cloud sandbox driver: one Nova VM per sandbox on the cstam-sandbox-net Neutron network.
# Responsible for: create = security group (HTTP only from the gateways) → Neutron port with OUR IP →
# Nova server; cleanup of partial resources; assign (server metadata); idempotent delete; orphan listing.
# NOT responsible for: choosing IPs (ipam/) or routes (gateways/). Criterion 1, OpenStack mode.

import asyncio
from pathlib import Path

import openstack
from openstack.exceptions import ConflictException

from app.drivers.base import IpConflictError, SandboxHandle
from app.settings import settings

# docker-compose on the control VM mounts infra/openstack/cloud-init here (see heat/control.yaml).
USERDATA_FILE = Path("/srv/cloud-init/sandbox.yaml")
BOOT_TIMEOUT_SECONDS = 600


class OpenStackDriver:
    name = "openstack"

    def __init__(self):
        self.conn = openstack.connect(cloud=settings.os_cloud)  # credentials from clouds.yaml
        self.userdata = USERDATA_FILE.read_text()

    async def create(self, sandbox_id: str, ip: str) -> SandboxHandle:
        return await asyncio.to_thread(self._create, sandbox_id, ip)

    def _create(self, sandbox_id: str, ip: str) -> SandboxHandle:
        created: dict = {}  # filled step by step, so a failure can clean up exactly what exists
        try:
            created["secgroup_id"] = self._create_security_group(sandbox_id)
            created["port_id"] = self._create_port(sandbox_id, ip, created["secgroup_id"])
            server = self.conn.create_server(
                name=f"sbx-{sandbox_id[:8]}",
                image=settings.os_sandbox_image,
                flavor=settings.os_sandbox_flavor,
                nics=[{"port-id": created["port_id"]}],
                userdata=self.userdata,
                meta={
                    "cstam_managed": "true", "cstam_sandbox": sandbox_id, "cstam_ip": ip,
                    "cstam_port": created["port_id"], "cstam_secgroup": created["secgroup_id"],
                },
                wait=True,
                timeout=BOOT_TIMEOUT_SECONDS,
            )
            return SandboxHandle(external_id=server.id, ip=ip, extra=created)
        except Exception:
            server = self.conn.compute.find_server(f"sbx-{sandbox_id[:8]}")
            self._delete(server.id if server else None, created)
            raise

    def _create_security_group(self, sandbox_id: str) -> str:
        """Only the gateways may reach the sandbox, and only on TCP 80."""
        network = self.conn.network
        gateways = network.find_security_group(settings.os_gateway_secgroup, ignore_missing=False)
        group = network.create_security_group(name=f"sbx-{sandbox_id}", description="CSTAM sandbox: HTTP from gateways")
        network.create_security_group_rule(
            security_group_id=group.id, direction="ingress", ethertype="IPv4",
            protocol="tcp", port_range_min=80, port_range_max=80, remote_group_id=gateways.id,
        )
        return group.id

    def _create_port(self, sandbox_id: str, ip: str, secgroup_id: str) -> str:
        """A port with the exact IP our IPAM chose. Neutron says 409 if someone else already has it."""
        network = self.conn.network
        net = network.find_network(settings.os_sandbox_network, ignore_missing=False)
        subnet = network.find_subnet(settings.os_sandbox_subnet, ignore_missing=False)
        try:
            port = network.create_port(
                network_id=net.id, name=f"sbx-{sandbox_id}",
                fixed_ips=[{"subnet_id": subnet.id, "ip_address": ip}],
                security_group_ids=[secgroup_id],
            )
        except ConflictException as error:  # IpAddressAlreadyAllocated
            raise IpConflictError(f"{ip} already allocated in Neutron") from error
        return port.id

    async def assign(self, handle: SandboxHandle, team: str, hostname: str) -> None:
        # The sandbox's own page (cloud-init/sandbox.yaml) reads this metadata to show the team.
        await asyncio.to_thread(
            self.conn.compute.set_server_metadata, handle.external_id, team=team, hostname=hostname
        )

    async def delete(self, external_id: str | None, extra: dict) -> None:
        await asyncio.to_thread(self._delete, external_id, extra)

    def _delete(self, server_id: str | None, extra: dict) -> None:
        """Server first (it uses the port), then port, then security group. NotFound = already gone."""
        server = self.conn.compute.find_server(server_id) if server_id else None  # None = already gone
        if server:
            self.conn.compute.delete_server(server, ignore_missing=True)
            self.conn.compute.wait_for_delete(server, wait=BOOT_TIMEOUT_SECONDS)
        if extra.get("port_id"):
            self.conn.network.delete_port(extra["port_id"], ignore_missing=True)
        if extra.get("secgroup_id"):
            self.conn.network.delete_security_group(extra["secgroup_id"], ignore_missing=True)

    async def list_managed(self) -> list[dict]:
        def list_servers():
            managed = []
            for server in self.conn.compute.servers(details=True):
                meta = server.metadata or {}
                if meta.get("cstam_managed") == "true":
                    managed.append({
                        "sandbox_id": meta.get("cstam_sandbox"), "external_id": server.id, "ip": meta.get("cstam_ip"),
                        "port_id": meta.get("cstam_port"), "secgroup_id": meta.get("cstam_secgroup"),
                    })
            return managed

        return await asyncio.to_thread(list_servers)
