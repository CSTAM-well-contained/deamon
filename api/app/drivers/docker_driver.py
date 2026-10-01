# Local sandbox driver: one busybox container per sandbox on the cstam-sandbox-net bridge.
# Responsible for: create (fixed IP from our IPAM, labels, tiny web page), assign (rewrite the page),
# delete (idempotent), list_managed (for orphan cleanup).
# NOT responsible for: choosing IPs (ipam/) or routes (gateways/). Criterion 1, local mode.

import asyncio
import html
import logging

import docker
from docker.errors import APIError, ImageNotFound, NotFound

from app.drivers.base import IpConflictError, SandboxHandle

log = logging.getLogger("docker_driver")
NETWORK = "cstam-sandbox-net"
IMAGE = "busybox:stable"
PAGE = "/www/index.html"
# $1 = the first page. Kept if it already exists, so a stopped+started sandbox still shows its team page.
# httpd -f stays in the foreground, so the container lives as long as the web server.
START_SCRIPT = 'mkdir -p /www && { [ -s /www/index.html ] || printf "%s" "$1" > /www/index.html; } && exec httpd -f -p 80 -h /www'


def page(title: str, lines: list[str]) -> str:
    items = "".join(f"<p>{html.escape(line)}</p>" for line in lines)
    return f"<!doctype html><title>{html.escape(title)}</title><h1>{html.escape(title)}</h1>{items}\n"


class DockerDriver:
    name = "docker"

    def __init__(self):
        self.docker = docker.from_env()

    async def create(self, sandbox_id: str, ip: str) -> SandboxHandle:
        return await asyncio.to_thread(self._create, sandbox_id, ip)

    def _create(self, sandbox_id: str, ip: str) -> SandboxHandle:
        api = self.docker.api
        self._ensure_image()
        html_page = page("Warm sandbox", [f"private ip {ip}", "waiting for a team"])
        container = api.create_container(
            IMAGE,
            name=f"sbx-{sandbox_id[:8]}",
            command=["sh", "-c", START_SCRIPT, "sh", html_page],
            labels={"cstam.managed": "true", "cstam.sandbox": sandbox_id, "cstam.ip": ip},
            host_config=api.create_host_config(network_mode=NETWORK),
            networking_config=api.create_networking_config({NETWORK: api.create_endpoint_config(ipv4_address=ip)}),
        )
        try:
            api.start(container["Id"])
        except APIError as error:
            api.remove_container(container["Id"], force=True)
            if "address already in use" in str(error).lower():
                raise IpConflictError(f"{ip} is already used on {NETWORK}") from error
            raise
        return SandboxHandle(external_id=container["Id"], ip=ip, extra={"name": f"sbx-{sandbox_id[:8]}"})

    def _ensure_image(self) -> None:
        try:
            self.docker.images.get(IMAGE)
        except ImageNotFound:
            self.docker.images.pull(IMAGE)

    async def assign(self, handle: SandboxHandle, team: str, hostname: str) -> None:
        html_page = page(f"Sandbox of {team}", [f"hostname {hostname}", f"private ip {handle.ip}"])

        def write_page():
            container = self.docker.containers.get(handle.external_id)
            result = container.exec_run(["sh", "-c", f'printf "%s" "$1" > {PAGE}', "sh", html_page])
            if result.exit_code != 0:
                raise RuntimeError(f"could not write welcome page: {result.output!r}")

        await asyncio.to_thread(write_page)

    async def delete(self, external_id: str | None, extra: dict) -> None:
        if not external_id:
            return

        def remove():
            try:
                self.docker.api.remove_container(external_id, force=True)
            except NotFound:
                log.info("container %s already gone", external_id[:12])  # deleting is idempotent

        await asyncio.to_thread(remove)

    async def list_managed(self) -> list[dict]:
        def list_containers():
            found = self.docker.containers.list(all=True, filters={"label": "cstam.managed=true"})
            return [
                {"sandbox_id": c.labels.get("cstam.sandbox"), "external_id": c.id, "ip": c.labels.get("cstam.ip")}
                for c in found
            ]

        return await asyncio.to_thread(list_containers)
