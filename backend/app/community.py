"""Curated catalog of community-scripts.org (ProxmoxVE Helper-Scripts).

These scripts run on the Proxmox *host* and create their own LXC with the app.
We expose a few well-known suggestions; the user may also enter any slug. The
slug is strictly validated (``^[a-z0-9][a-z0-9-]*$``) before being placed into the
download URL, which prevents command injection and path traversal.
"""

from __future__ import annotations

import re
from typing import List

_BASE_URL = "https://github.com/community-scripts/ProxmoxVE/raw/main/ct"
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

# Suggestions shown in the UI (the user may type any other slug from the site).
SUGGESTIONS: List[dict] = [
    {"slug": "docker", "name": "Docker"},
    {"slug": "plex", "name": "Plex"},
    {"slug": "jellyfin", "name": "Jellyfin"},
    {"slug": "pihole", "name": "Pi-hole"},
    {"slug": "adguard", "name": "AdGuard Home"},
    {"slug": "nginxproxymanager", "name": "Nginx Proxy Manager"},
    {"slug": "uptimekuma", "name": "Uptime Kuma"},
    {"slug": "vaultwarden", "name": "Vaultwarden"},
    {"slug": "homeassistant", "name": "Home Assistant"},
    {"slug": "grafana", "name": "Grafana"},
    {"slug": "paperless-ngx", "name": "Paperless-ngx"},
    {"slug": "wikijs", "name": "Wiki.js"},
]


def is_valid_slug(slug: str) -> bool:
    return bool(_SLUG_RE.match(slug))


def script_url(slug: str) -> str:
    """Download URL for a community-script. Slug must be validated beforehand."""
    if not is_valid_slug(slug):
        raise ValueError("Ungültiger Script-Name.")
    return f"{_BASE_URL}/{slug}.sh"


def suggestions() -> List[dict]:
    return SUGGESTIONS
