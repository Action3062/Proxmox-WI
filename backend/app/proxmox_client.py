"""Async client for the Proxmox VE REST API.

Authentication uses an API token (``PVEAPIToken``) so no password/ticket handling
is required and the token never leaves the server. All errors are converted into
``ProxmoxAPIError`` with a human readable message; the token is never logged.
"""

from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from typing import Any, Dict, List, Optional

import httpx

from .config import Settings, get_settings

logger = logging.getLogger(__name__)


class ProxmoxAPIError(Exception):
    """User-facing Proxmox API error with an understandable message."""


class ProxmoxClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._base_url = settings.proxmox_host.rstrip("/") + "/api2/json"

    @property
    def default_node(self) -> str:
        return self._settings.proxmox_node

    def _headers(self) -> Dict[str, str]:
        token_id = self._settings.proxmox_token_id
        token_secret = self._settings.proxmox_token_secret
        return {"Authorization": f"PVEAPIToken={token_id}={token_secret}"}

    def _client(self) -> httpx.AsyncClient:
        if not self._settings.proxmox_host:
            raise ProxmoxAPIError("Proxmox-Host ist nicht konfiguriert.")
        if not self._settings.proxmox_token_id or not self._settings.proxmox_token_secret:
            raise ProxmoxAPIError("Proxmox-API-Token ist nicht konfiguriert.")
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers(),
            verify=self._settings.proxmox_verify_ssl,
            timeout=httpx.Timeout(30.0, connect=10.0),
        )

    async def _request(
        self, method: str, path: str, *, data: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> Any:
        try:
            async with self._client() as client:
                response = await client.request(method, path, data=data, params=params)
        except httpx.ConnectError as exc:
            raise ProxmoxAPIError(
                "Proxmox-Server nicht erreichbar. Bitte Host und Netzwerk prüfen."
            ) from exc
        except httpx.TimeoutException as exc:
            raise ProxmoxAPIError("Zeitüberschreitung bei der Proxmox-Anfrage.") from exc
        except httpx.HTTPError as exc:
            raise ProxmoxAPIError(f"Proxmox-Verbindungsfehler: {exc}") from exc

        if response.status_code == 401:
            raise ProxmoxAPIError(
                "Authentifizierung bei Proxmox fehlgeschlagen. API-Token prüfen."
            )
        if response.status_code >= 400:
            raise ProxmoxAPIError(self._format_error(response))

        try:
            payload = response.json()
        except ValueError as exc:
            raise ProxmoxAPIError("Unerwartete Antwort von Proxmox.") from exc
        return payload.get("data")

    @staticmethod
    def _format_error(response: httpx.Response) -> str:
        """Build a readable error message from a Proxmox error response."""
        message = f"Proxmox-Fehler (HTTP {response.status_code})"
        try:
            body = response.json()
        except ValueError:
            return message
        errors = body.get("errors")
        if errors:
            details = "; ".join(f"{k}: {v}" for k, v in errors.items())
            return f"{message}: {details}"
        if body.get("message"):
            return f"{message}: {body['message']}"
        return message

    # --- Read endpoints (UI metadata) ------------------------------------
    async def get_nodes(self) -> List[dict]:
        return await self._request("GET", "/nodes") or []

    async def get_storages(self, node: str) -> List[dict]:
        return await self._request("GET", f"/nodes/{node}/storage") or []

    async def get_bridges(self, node: str) -> List[dict]:
        data = await self._request(
            "GET", f"/nodes/{node}/network", params={"type": "any_bridge"}
        )
        return data or []

    async def get_templates(self, node: str, storage: str) -> List[dict]:
        """List LXC templates (vztmpl) available on a storage."""
        data = await self._request(
            "GET",
            f"/nodes/{node}/storage/{storage}/content",
            params={"content": "vztmpl"},
        )
        return data or []

    async def next_vmid(self) -> int:
        data = await self._request("GET", "/cluster/nextid")
        return int(data)

    async def list_lxc(self, node: str) -> List[dict]:
        return await self._request("GET", f"/nodes/{node}/lxc") or []

    # --- Write endpoints --------------------------------------------------
    async def create_lxc(self, node: str, params: Dict[str, Any]) -> str:
        """Create an LXC container. Returns the task UPID."""
        # Proxmox expects form-encoded values; normalise bools to 0/1.
        normalised = {
            k: (1 if v is True else 0 if v is False else v)
            for k, v in params.items()
            if v is not None
        }
        upid = await self._request("POST", f"/nodes/{node}/lxc", data=normalised)
        return str(upid)

    async def start_lxc(self, node: str, vmid: int) -> str:
        upid = await self._request("POST", f"/nodes/{node}/lxc/{vmid}/status/start")
        return str(upid)

    async def task_status(self, node: str, upid: str) -> dict:
        return await self._request("GET", f"/nodes/{node}/tasks/{upid}/status") or {}

    async def wait_for_task(
        self, node: str, upid: str, timeout: int = 300, interval: float = 2.0
    ) -> dict:
        """Poll a task until it stops; raise if it failed or timed out."""
        elapsed = 0.0
        while elapsed < timeout:
            status_data = await self.task_status(node, upid)
            if status_data.get("status") == "stopped":
                exit_status = status_data.get("exitstatus")
                if exit_status not in ("OK", None):
                    raise ProxmoxAPIError(f"Proxmox-Task fehlgeschlagen: {exit_status}")
                return status_data
            await asyncio.sleep(interval)
            elapsed += interval
        raise ProxmoxAPIError("Zeitüberschreitung beim Warten auf einen Proxmox-Task.")


@lru_cache
def get_proxmox() -> ProxmoxClient:
    return ProxmoxClient(get_settings())
