# Picks the sandbox driver named by settings.DRIVER (docker | openstack).
# Responsible for: creating the one driver instance the app uses.
# NOT responsible for: driver behaviour (docker_driver.py / openstack_driver.py).
# Serves Criterion 1.

from functools import cache

from app.drivers.base import SandboxDriver
from app.settings import settings


@cache
def get_driver() -> SandboxDriver:
    if settings.driver == "docker":
        from app.drivers.docker_driver import DockerDriver

        return DockerDriver()
    if settings.driver == "openstack":
        from app.drivers.openstack_driver import OpenStackDriver

        return OpenStackDriver()
    raise ValueError(f"unknown DRIVER {settings.driver!r} (expected docker or openstack)")
