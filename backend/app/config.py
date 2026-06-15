"""Application configuration.

All sensitive values (secret key, Proxmox token, SSH credentials, ...) are read
exclusively from environment variables / the ``.env`` file. Nothing is hardcoded.
"""

from __future__ import annotations

import secrets
from functools import lru_cache
from typing import List, Optional
from urllib.parse import urlparse

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly typed application settings loaded from the environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Application ---------------------------------------------------------
    app_name: str = "Proxmox Web Interface"
    environment: str = "production"
    log_level: str = "INFO"
    log_dir: str = "/app/logs"
    log_file: str = "app.log"

    # --- Security / Authentication ------------------------------------------
    # The JWT signing key. If left empty a random ephemeral key is generated at
    # startup (sessions will not survive a restart) and a warning is logged.
    secret_key: str = ""
    access_token_expire_minutes: int = 60
    jwt_algorithm: str = "HS256"

    admin_username: str = "admin"
    # Provide EITHER a plaintext password (hashed once at startup) OR, preferred,
    # a precomputed bcrypt hash. Never commit either of these.
    admin_password: Optional[str] = None
    admin_password_hash: Optional[str] = None

    # --- CORS ----------------------------------------------------------------
    # Comma separated list of allowed origins. Stored as a plain string on
    # purpose: pydantic-settings tries to JSON-decode env values of complex types
    # (e.g. List[str]), which would crash on a comma separated value. The parsed
    # list is exposed via `cors_origins_list`.
    cors_origins: str = "http://localhost,http://localhost:5173"

    # --- Proxmox VE API ------------------------------------------------------
    proxmox_host: str = ""  # e.g. https://192.168.1.10:8006
    proxmox_node: str = "pve"
    proxmox_token_id: str = ""  # e.g. root@pam!webui
    proxmox_token_secret: str = ""
    proxmox_verify_ssl: bool = False  # homelab Proxmox often uses self-signed certs

    # --- Proxmox defaults presented in the UI -------------------------------
    proxmox_default_storage: str = "local-lvm"
    proxmox_template_storage: str = "local"
    proxmox_default_bridge: str = "vmbr0"

    # --- Proxmox SSH (used for `pct exec`: software install + update checks) --
    # If the SSH host is not given it is derived from ``proxmox_host``.
    proxmox_ssh_host: Optional[str] = None
    proxmox_ssh_port: int = 22
    proxmox_ssh_user: str = "root"
    proxmox_ssh_password: Optional[str] = None
    proxmox_ssh_key: Optional[str] = None  # private key contents (PEM)
    proxmox_ssh_key_file: Optional[str] = None  # path to a private key file

    @property
    def cors_origins_list(self) -> List[str]:
        """Allowed CORS origins as a list (parsed from the comma separated value)."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def effective_ssh_host(self) -> str:
        """SSH target host, derived from the Proxmox API URL when not set."""
        if self.proxmox_ssh_host:
            return self.proxmox_ssh_host
        if self.proxmox_host:
            parsed = urlparse(self.proxmox_host)
            if parsed.hostname:
                return parsed.hostname
        return ""

    def ensure_secret_key(self) -> None:
        """Generate an ephemeral secret key if none was configured."""
        if not self.secret_key:
            self.secret_key = secrets.token_urlsafe(48)


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (read once per process)."""
    settings = Settings()
    settings.ensure_secret_key()
    return settings
