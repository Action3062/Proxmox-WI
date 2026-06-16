"""Pydantic models: request/response schemas with input validation.

Validation here is the security boundary for user input (hostname, IP, resource
sizes, package selection). Package names are never taken as free text — only IDs
from the fixed software catalog are accepted, which prevents command injection.
"""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime
from enum import Enum
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# RFC 1123 host name (one or more labels). Used for the container hostname.
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)"
    r"(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$"
)
# Linux user name rules.
_USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


# --- Auth -------------------------------------------------------------------
class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class User(BaseModel):
    username: str
    role: str = "admin"


# --- Software catalog -------------------------------------------------------
class SoftwarePackage(BaseModel):
    id: str
    label: str
    category: str  # "base" or an extra category like "container", "web", ...
    description: str = ""
    default: bool = False


# --- Container creation -----------------------------------------------------
class IPConfigMode(str, Enum):
    dhcp = "dhcp"
    static = "static"


class ContainerCreateRequest(BaseModel):
    """Validated payload to create an LXC container.

    The structure is intentionally generic so that VMs (``type="vm"``) can be
    added later without breaking the API contract.
    """

    type: Literal["lxc"] = "lxc"  # "vm" reserved for a future iteration
    node: Optional[str] = None  # falls back to the configured default node

    # Template / OS
    template: str = Field(min_length=3, max_length=256)  # Proxmox volid
    os: Optional[Literal["debian", "ubuntu"]] = None

    # Identity
    hostname: str = Field(min_length=1, max_length=253)
    description: str = Field(default="", max_length=1024)

    # Resources
    cores: int = Field(ge=1, le=128)
    memory_mb: int = Field(ge=128, le=1_048_576)
    disk_gb: int = Field(ge=1, le=8192)
    storage: str = Field(min_length=1, max_length=64)

    # Network
    bridge: str = Field(min_length=1, max_length=32)
    ip_config: IPConfigMode = IPConfigMode.dhcp
    ip_address: Optional[str] = None  # CIDR notation for static, e.g. 192.168.1.50/24
    gateway: Optional[str] = None

    # Credentials (at least one of password / ssh_key is required)
    username: str = Field(min_length=1, max_length=32)
    password: Optional[str] = Field(default=None, max_length=256)
    ssh_key: Optional[str] = Field(default=None, max_length=4096)

    # Options
    autostart: bool = False
    software: List[str] = Field(default_factory=list)

    @field_validator("hostname")
    @classmethod
    def _validate_hostname(cls, value: str) -> str:
        if not _HOSTNAME_RE.match(value):
            raise ValueError("Ungültiger Hostname.")
        return value

    @field_validator("template")
    @classmethod
    def _validate_template(cls, value: str) -> str:
        # Must be a Proxmox volume id ("storage:vztmpl/file"), never a raw path.
        # Proxmox rejects arbitrary filesystem paths for non-root API tokens with
        # "Only root can pass arbitrary filesystem paths", so we catch it early.
        if value.startswith("/") or ":" not in value:
            raise ValueError(
                "Ungültiges Template. Bitte ein Template aus der Liste wählen "
                "(Format 'storage:vztmpl/datei'), keinen Dateipfad."
            )
        return value

    @field_validator("username")
    @classmethod
    def _validate_username(cls, value: str) -> str:
        if not _USERNAME_RE.match(value):
            raise ValueError(
                "Ungültiger Benutzername (nur Kleinbuchstaben, Ziffern, '-' und '_')."
            )
        return value

    @field_validator("storage", "bridge")
    @classmethod
    def _validate_identifier(cls, value: str) -> str:
        # Proxmox identifiers are alphanumeric plus a small set of separators.
        if not re.match(r"^[a-zA-Z0-9._-]+$", value):
            raise ValueError("Ungültiger Bezeichner.")
        return value

    @model_validator(mode="after")
    def _validate_network_and_credentials(self) -> "ContainerCreateRequest":
        # Static IP requires a valid CIDR address (and optionally a gateway).
        if self.ip_config == IPConfigMode.static:
            if not self.ip_address:
                raise ValueError("Bei statischer IP ist eine IP-Adresse erforderlich.")
            try:
                ipaddress.ip_interface(self.ip_address)
            except ValueError as exc:  # invalid CIDR
                raise ValueError(
                    "Ungültige IP-Adresse (CIDR-Notation erwartet, z. B. 192.168.1.50/24)."
                ) from exc
            if self.gateway:
                try:
                    ipaddress.ip_address(self.gateway)
                except ValueError as exc:
                    raise ValueError("Ungültiges Gateway.") from exc

        # Require a way to log in to the new container.
        if not self.password and not self.ssh_key:
            raise ValueError("Entweder Passwort oder SSH-Key muss angegeben werden.")
        return self

    def safe_dict(self) -> dict:
        """Return the request without secrets, suitable for logging/status."""
        data = self.model_dump()
        data.pop("password", None)
        data["ssh_key"] = "***" if self.ssh_key else None
        return data


# --- Jobs / status ----------------------------------------------------------
class JobStatus(str, Enum):
    pending = "pending"
    creating = "creating"  # Erstellung läuft
    starting = "starting"  # Container/VM wird gestartet
    installing = "installing"  # Software wird installiert
    checking_updates = "checking_updates"  # Updates werden geprüft
    installing_updates = "installing_updates"
    done = "done"  # Fertig
    error = "error"  # Fehler


class UpdateInfo(BaseModel):
    name: str
    current: Optional[str] = None
    candidate: Optional[str] = None


class LogEntry(BaseModel):
    timestamp: datetime
    level: str
    message: str


class JobResponse(BaseModel):
    id: str
    type: str
    status: JobStatus
    step: str  # human readable, localized status text
    progress: int  # 0..100
    hostname: Optional[str] = None
    node: Optional[str] = None
    vmid: Optional[int] = None
    error: Optional[str] = None
    updates: List[UpdateInfo] = Field(default_factory=list)
    updates_checked: bool = False
    logs: List[LogEntry] = Field(default_factory=list)
    request: Optional[dict] = None
    created_at: datetime
    updated_at: datetime
