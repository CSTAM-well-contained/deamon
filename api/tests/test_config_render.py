# Criterion 4 — the HAProxy template renders a valid, deterministic config.
# Responsible for: same input → same text; version appears where the agent's probe looks for it;
# `haproxy -c` accepts the result (haproxy is installed in the API image for this test).
# NOT responsible for: pushing configs (test_push_order.py).

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.gateways import config_sync


def test_render_is_deterministic_and_versioned():
    first = config_sync.render(7)
    assert first == config_sync.render(7)
    assert first != config_sync.render(8)
    assert 'string "7" if { path /__gw_version }' in first
    assert 'X-Config-Version "7"' in first
    assert "bind :80\n" in first
    assert "map(/etc/haproxy/hosts.map)" in first


def test_semantic_bad_only_changes_the_port():
    good, bad = config_sync.render(3), config_sync.render(3, bind_port=8099)
    assert bad == good.replace("bind :80\n", "bind :8099\n")


def test_restamp_moves_every_version_marker():
    restamped = config_sync.restamp(config_sync.render(4), old=4, new=9)
    assert restamped == config_sync.render(9)


@pytest.mark.skipif(shutil.which("haproxy") is None, reason="haproxy binary not installed")
@pytest.mark.parametrize("bind_port, bad_line, valid", [(80, "", True), (8099, "", True), (80, "this is not haproxy {{{", False)])
def test_haproxy_accepts_the_rendered_config(tmp_path: Path, bind_port, bad_line, valid):
    # The config references these two files; create them like the gateway entrypoint does.
    Path("/etc/haproxy").mkdir(exist_ok=True)
    Path("/etc/haproxy/hosts.map").touch()
    Path("/etc/haproxy/maintenance.http").write_text(
        "HTTP/1.0 503 Service Unavailable\r\nContent-Type: text/plain\r\nContent-Length: 4\r\n\r\ndown"
    )
    config = tmp_path / "haproxy.cfg"
    config.write_text(config_sync.render(1, bind_port=bind_port) + bad_line + "\n")
    check = subprocess.run(
        ["haproxy", "-c", "-f", str(config)], capture_output=True, text=True, env={**os.environ, "GW_NAME": "test"}
    )
    assert (check.returncode == 0) == valid, check.stderr
