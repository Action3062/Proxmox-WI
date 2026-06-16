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


def test_install_script_waits_for_apt_lock():
    # Must tolerate the cloud-init/apt-daily dpkg lock and stop competing apt jobs.
    script = build_install_script(["curl"])
    assert "DPkg::Lock::Timeout" in script
    assert "pgrep" in script
    assert "apt-daily" in script


def test_install_script_sets_locale_and_keyboard():
    de = build_install_script(["curl"], "de")
    assert "de_DE.UTF-8" in de and "KEYMAP=de" in de and "Europe/Berlin" in de
    en = build_install_script(["curl"], "en")
    assert "en_US.UTF-8" in en and "KEYMAP=us" in en


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


# --- VM support -------------------------------------------------------------
def test_vm_template_must_be_numeric():
    with pytest.raises(ValidationError):
        ContainerCreateRequest(
            **_valid_payload(type="vm", template="local:vztmpl/x.tar.zst")
        )


def test_vm_request_valid():
    req = ContainerCreateRequest(**_valid_payload(type="vm", template="9000"))
    assert req.type == "vm"
    assert req.vm_template_id == 9000


def test_build_vm_config_cloudinit():
    from app.tasks import build_vm_config

    req = ContainerCreateRequest(
        **_valid_payload(
            type="vm",
            template="9000",
            ip_config="static",
            ip_address="192.168.1.50/24",
            gateway="192.168.1.1",
            ssh_key="ssh-ed25519 AAAAExample",
        )
    )
    cfg = build_vm_config(req)
    assert cfg["ciuser"] == "deploy"
    assert cfg["ipconfig0"] == "ip=192.168.1.50/24,gw=192.168.1.1"
    assert cfg["agent"] == 1
    assert cfg["vga"] == "std"  # ensure a usable graphical console
    assert cfg["keyboard"] == "de"  # German VNC keyboard layout by default
    assert cfg["net0"].startswith("virtio,bridge=")
    assert "%20" in cfg["sshkeys"]  # URL-encoded


def test_agent_exec_isolates_daemons_and_returns_output():
    import asyncio

    client = _client()
    calls = {}

    async def fake_request(method, path, *, data=None, params=None):
        if path.endswith("/agent/exec"):
            calls["command"] = data["command"]
            return {"pid": 42}
        if path.endswith("/agent/exec-status"):
            return {"exited": 1, "exitcode": 0, "out-data": "done", "err-data": ""}
        return None

    client._request = fake_request
    res = asyncio.run(client.agent_exec("node", 1, "echo hi"))
    assert res.ok and res.stdout == "done"
    # The wrapper must isolate daemons from the agent's output pipes.
    cmd = calls["command"]
    assert cmd[0] == "/bin/bash" and cmd[1] == "-c"
    assert "mktemp" in cmd[2] and '>"$out" 2>&1' in cmd[2]


def test_update_defaults_on():
    req = ContainerCreateRequest(**_valid_payload())
    assert req.install_updates is True
    assert req.auto_security_updates is True


def test_unattended_upgrades_script():
    from app.software import build_unattended_upgrades_script

    s = build_unattended_upgrades_script()
    assert "unattended-upgrades" in s
    assert "20auto-upgrades" in s
    assert "apt-daily.timer" in s


def test_community_script_slug_validation():
    from app.models import CommunityScriptRequest

    assert CommunityScriptRequest(slug="jellyfin").slug == "jellyfin"
    for bad in ["bad slug", "../etc", "rm -rf /", "a;b", "UPPER"]:
        with pytest.raises(ValidationError):
            CommunityScriptRequest(slug=bad)


def test_autologin_script():
    from app.tasks import build_autologin_script

    s = build_autologin_script("deploy")
    assert "--autologin deploy" in s
    assert "getty@tty1.service.d" in s
    assert "console-getty.service.d" in s  # LXC console


def test_ssh_pwauth_script():
    from app.tasks import build_ssh_pwauth_script

    s = build_ssh_pwauth_script()
    assert "PasswordAuthentication yes" in s
    assert "00-pwauth.conf" in s


def test_backup_guest_posts_vzdump():
    import asyncio

    client = _client()
    captured = {}

    async def fake_request(method, path, **kwargs):
        captured["path"] = path
        captured["data"] = kwargs.get("data")
        return "UPID:backup"

    client._request = fake_request
    upid = asyncio.run(client.backup_guest("node", 100, "local"))
    assert upid == "UPID:backup"
    assert captured["path"].endswith("/vzdump")
    assert captured["data"]["vmid"] == 100
    assert captured["data"]["storage"] == "local"


def test_get_lxc_ip_parses_interfaces():
    import asyncio

    client = _client()

    async def fake_request(method, path, **kwargs):
        return [
            {"name": "lo", "inet": "127.0.0.1/8"},
            {"name": "eth0", "inet": "192.168.1.50/24"},
        ]

    client._request = fake_request
    assert asyncio.run(client.get_lxc_ip("node", 100)) == "192.168.1.50"


def test_get_qemu_ip_parses_agent():
    import asyncio

    client = _client()

    async def fake_request(method, path, **kwargs):
        return {
            "result": [
                {"name": "lo", "ip-addresses": [
                    {"ip-address-type": "ipv4", "ip-address": "127.0.0.1"}]},
                {"name": "eth0", "ip-addresses": [
                    {"ip-address-type": "ipv4", "ip-address": "192.168.1.51"}]},
            ]
        }

    client._request = fake_request
    assert asyncio.run(client.get_qemu_ip("node", 100)) == "192.168.1.51"


def test_detect_boot_disk():
    from app.tasks import _detect_boot_disk

    assert (
        _detect_boot_disk(
            {"boot": "order=scsi0;net0", "scsi0": "local-zfs:vm-9000-disk-0,size=2G"}
        )
        == "scsi0"
    )
    assert (
        _detect_boot_disk(
            {"virtio0": "local:vm-1-disk-0,size=2G", "ide2": "local:cloudinit,media=cdrom"}
        )
        == "virtio0"
    )
    assert _detect_boot_disk({"ide2": "x,media=cdrom"}) is None
