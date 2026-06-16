"""Unit tests for validation, parsing and log redaction.

Run with: ``cd backend && pip install -r requirements.txt pytest && pytest``
"""

import pytest
from pydantic import ValidationError

from app.logging_config import redact
from app.models import ContainerCreateRequest
from app.software import build_install_script, default_ids, valid_ids
from app.tasks import parse_upgradable


def _valid_payload(**overrides):
    base = dict(
        template="local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst",
        hostname="web01",
        cores=2,
        memory_mb=1024,
        disk_gb=8,
        storage="local-lvm",
        bridge="vmbr0",
        username="deploy",
        password="s3cret-pw",
        software=["curl", "git"],
    )
    base.update(overrides)
    return base


def test_valid_request():
    req = ContainerCreateRequest(**_valid_payload())
    assert req.hostname == "web01"
    # Secrets must be stripped from the loggable representation.
    safe = req.safe_dict()
    assert "password" not in safe
    assert safe["ssh_key"] is None


def test_invalid_hostname():
    with pytest.raises(ValidationError):
        ContainerCreateRequest(**_valid_payload(hostname="invalid host!"))


def test_invalid_template_rejects_path():
    # A filesystem path (not a storage volid) must be rejected before reaching Proxmox.
    with pytest.raises(ValidationError):
        ContainerCreateRequest(
            **_valid_payload(template="/var/lib/vz/template/cache/debian-12.tar.zst")
        )


def test_invalid_template_requires_storage_prefix():
    with pytest.raises(ValidationError):
        ContainerCreateRequest(**_valid_payload(template="debian-12.tar.zst"))


def test_static_ip_requires_address():
    with pytest.raises(ValidationError):
        ContainerCreateRequest(**_valid_payload(ip_config="static"))


def test_static_ip_valid():
    req = ContainerCreateRequest(
        **_valid_payload(
            ip_config="static", ip_address="192.168.1.50/24", gateway="192.168.1.1"
        )
    )
    assert req.ip_address == "192.168.1.50/24"


def test_credentials_required():
    payload = _valid_payload()
    payload.pop("password")
    with pytest.raises(ValidationError):
        ContainerCreateRequest(**payload)


def test_software_catalog_defaults():
    assert "curl" in default_ids()
    assert "docker" in valid_ids()


def test_install_script_resolves_dependencies():
    # portainer depends on docker -> docker must appear in the script.
    script = build_install_script(["portainer"])
    assert "get.docker.com" in script
    assert "portainer" in script


def test_parse_upgradable():
    output = (
        "Listing...\n"
        "htop/stable 3.2.1 amd64 [upgradable from: 3.2.0]\n"
        "curl/stable 8.0.0 amd64 [upgradable from: 7.9.0]\n"
    )
    updates = parse_upgradable(output)
    assert len(updates) == 2
    assert updates[0].name == "htop"
    assert updates[0].candidate == "3.2.1"
    assert updates[0].current == "3.2.0"


def test_redaction():
    assert "s3cret" not in redact("password: s3cret")
    assert "REDACTED" in redact("PVEAPIToken=root@pam!web=abcd-1234")


def test_cors_origins_from_env(monkeypatch):
    # Regression: a comma separated CORS_ORIGINS env value must not crash
    # settings construction (pydantic-settings must not JSON-decode it).
    monkeypatch.setenv("CORS_ORIGINS", "http://a.example,http://b.example:5173")
    from app.config import Settings

    settings = Settings()
    assert settings.cors_origins_list == ["http://a.example", "http://b.example:5173"]


class _FakeProxmox:
    def __init__(self, nodes, default):
        self._nodes = nodes
        self.default_node = default

    async def get_nodes(self):
        return [{"node": n} for n in self._nodes]


def test_resolve_node_match():
    import asyncio
    from app.tasks import _resolve_deployment_node

    px = _FakeProxmox(["pve", "node2"], "pve")
    assert asyncio.run(_resolve_deployment_node(px, None)) == "pve"


def test_resolve_node_single_node_fallback():
    # Configured default is wrong but there is exactly one node -> use it.
    import asyncio
    from app.tasks import _resolve_deployment_node

    px = _FakeProxmox(["Proxmox-WI"], "pve")
    assert asyncio.run(_resolve_deployment_node(px, None)) == "Proxmox-WI"


def test_resolve_node_unknown_raises():
    import asyncio
    from app.proxmox_client import ProxmoxAPIError
    from app.tasks import _resolve_deployment_node

    px = _FakeProxmox(["a", "b"], "pve")
    with pytest.raises(ProxmoxAPIError):
        asyncio.run(_resolve_deployment_node(px, None))


def _client():
    from app.config import get_settings
    from app.proxmox_client import ProxmoxClient

    return ProxmoxClient(get_settings())


def test_wait_for_task_accepts_warnings():
    # "WARNINGS: N" means the task completed (with warnings) -> success.
    import asyncio

    client = _client()

    async def fake_status(node, upid):
        return {"status": "stopped", "exitstatus": "WARNINGS: 1"}

    client.task_status = fake_status
    result = asyncio.run(client.wait_for_task("node", "upid", timeout=5))
    assert result["exitstatus"] == "WARNINGS: 1"


def test_wait_for_task_raises_on_real_error():
    import asyncio
    from app.proxmox_client import ProxmoxAPIError

    client = _client()

    async def fake_status(node, upid):
        return {"status": "stopped", "exitstatus": "unable to create CT"}

    client.task_status = fake_status
    with pytest.raises(ProxmoxAPIError):
        asyncio.run(client.wait_for_task("node", "upid", timeout=5))
