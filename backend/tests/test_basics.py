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
