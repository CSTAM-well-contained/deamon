# All control-plane configuration, read from environment variables (see .env.example).
# Responsible for: names, types and defaults of every setting.
# NOT responsible for: using them — each feature module imports `settings` and reads what it needs.
# Serves all criteria.

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://cstam:cstam@10.20.0.5:5432/cstam"
    driver: str = "docker"  # docker | openstack
    base_domain: str = "cstam.felcloud.tn"

    # Criterion 3 — IPAM
    pool_start: str = "10.20.0.21"
    pool_end: str = "10.20.0.199"

    # Criterion 1 — provisioning
    warm_pool_size: int = 3
    default_ttl_seconds: int = 7200
    max_ttl_seconds: int = 86400

    # Background jobs
    job_interval_seconds: int = 5
    monitor_interval_seconds: int = 2

    # Criteria 2 + 4 — gateways, as "name=url,name=url"
    gateways: str = "gw1=http://10.20.0.11:9000,gw2=http://10.20.0.12:9000"
    agent_token: str = "change-me"

    # If set, mutating endpoints need the header X-API-Key
    api_key: str = ""

    # OpenStack driver only
    os_cloud: str = "felcloud"
    os_sandbox_network: str = "cstam-sandbox-net"
    os_sandbox_subnet: str = "cstam-sandbox-subnet"
    os_sandbox_image: str = "ubuntu-22.04"
    os_sandbox_flavor: str = "m1.small"
    os_gateway_secgroup: str = "cstam-gateway-sg"

    def gateway_urls(self) -> dict[str, str]:
        """'gw1=http://a:9000,gw2=http://b:9000' → {'gw1': 'http://a:9000', 'gw2': 'http://b:9000'}"""
        pairs = (item.split("=", 1) for item in self.gateways.split(",") if "=" in item)
        return {name.strip(): url.strip().rstrip("/") for name, url in pairs}


settings = Settings()
