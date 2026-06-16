"""Async client for the Proxmox VE REST API.

Authentication uses an API token (``PVEAPIToken``) so no password/ticket handling
is required and the token never leaves the server. All errors are converted into
``ProxmoxAPIError`` with a human readable message; the token is never logged.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, List, Optional

import httpx

from .config import Settings, get_settings

logger = logging.getLogger(__name__)


class ProxmoxAPIError(Exception):
    """User-facing Proxmox API error with an understandable message."""


@dataclass
class AgentExecResult:
    """Result of a command executed inside a VM via the QEMU guest agent."""

    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def _normalise(params: Dict[str, Any]) -> Dict[str, Any]:
    """Drop None values and convert booleans to Proxmox's 0/1 form."""
    return {
        k: (1 if v is True else 0 if v is False else v)
        for k, v in params.items()
        if v is not None
    }


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
        upid = await self._request("POST", f"/nodes/{node}/lxc", data=_normalise(params))
        return str(upid)

    async def start_lxc(self, node: str, vmid: int) -> str:
        upid = await self._request("POST", f"/nodes/{node}/lxc/{vmid}/status/start")
        return str(upid)

    async def task_status(self, node: str, upid: str) -> dict:
        return await self._request("GET", f"/nodes/{node}/tasks/{upid}/status") or {}

    async def wait_for_task(
        self, node: str, upid: str, timeout: int = 300, interval: float = 2.0
    ) -> dict:
        """Poll a task until it stops; raise only on a real failure or timeout.

        A task may finish with exitstatus "OK" or "WARNINGS: N" (completed *with*
        warnings) — both count as success. Any other non-OK status is an error.
        """
        elapsed = 0.0
        while elapsed < timeout:
            status_data = await self.task_status(node, upid)
            if status_data.get("status") == "stopped":
                exit_status = status_data.get("exitstatus")
                if (
                    exit_status is None
                    or exit_status == "OK"
                    or str(exit_status).upper().startswith("WARNINGS")
                ):
                    return status_data
                raise ProxmoxAPIError(f"Proxmox-Task fehlgeschlagen: {exit_status}")
            await asyncio.sleep(interval)
            elapsed += interval
        raise ProxmoxAPIError("Zeitüberschreitung beim Warten auf einen Proxmox-Task.")

    async def get_task_log(self, node: str, upid: str, limit: int = 100) -> List[str]:
        """Return the task's log lines (used to surface warnings to the user)."""
        data = await self._request(
            "GET", f"/nodes/{node}/tasks/{upid}/log", params={"limit": limit}
        )
        return [entry.get("t", "") for entry in (data or []) if isinstance(entry, dict)]

    # --- VM (QEMU) -------------------------------------------------------
    async def list_qemu(self, node: str) -> List[dict]:
        return await self._request("GET", f"/nodes/{node}/qemu") or []

    async def get_qemu_config(self, node: str, vmid: int) -> dict:
        return await self._request("GET", f"/nodes/{node}/qemu/{vmid}/config") or {}

    async def clone_qemu(self, node: str, template_id: int, params: Dict[str, Any]) -> str:
        """Clone a VM template. Returns the task UPID."""
        upid = await self._request(
            "POST", f"/nodes/{node}/qemu/{template_id}/clone", data=_normalise(params)
        )
        return str(upid)

    async def set_qemu_config(self, node: str, vmid: int, params: Dict[str, Any]) -> None:
        await self._request(
            "PUT", f"/nodes/{node}/qemu/{vmid}/config", data=_normalise(params)
        )

    async def resize_qemu_disk(self, node: str, vmid: int, disk: str, size: str) -> None:
        await self._request(
            "PUT", f"/nodes/{node}/qemu/{vmid}/resize", data={"disk": disk, "size": size}
        )

    async def start_qemu(self, node: str, vmid: int) -> str:
        upid = await self._request("POST", f"/nodes/{node}/qemu/{vmid}/status/start")
        return str(upid)

    # --- Generic guest management (kind = "lxc" | "qemu") -----------------
    async def guest_action(self, node: str, kind: str, vmid: int, action: str) -> str:
        """Run a power action (start/stop/shutdown/reboot) on a guest."""
        upid = await self._request(
            "POST", f"/nodes/{node}/{kind}/{vmid}/status/{action}"
        )
        return str(upid)

    async def delete_guest(self, node: str, kind: str, vmid: int) -> str:
        """Destroy a (stopped) guest, removing its unused disks."""
        upid = await self._request(
            "DELETE", f"/nodes/{node}/{kind}/{vmid}",
            params={"destroy-unreferenced-disks": 1, "purge": 1},
        )
        return str(upid)

    async def backup_guest(
        self, node: str, vmid: int, storage: str,
        mode: str = "snapshot", compress: str = "zstd",
    ) -> str:
        """Create a vzdump backup of a guest. Returns the task UPID."""
        upid = await self._request(
            "POST", f"/nodes/{node}/vzdump",
            data=_normalise({
                "vmid": vmid,
                "storage": storage,
                "mode": mode,
                "compress": compress,
                "remove": 0,  # never prune other backups
            }),
        )
        return str(upid)

    async def get_lxc_ip(self, node: str, vmid: int) -> Optional[str]:
        """Best-effort primary IPv4 of a running container (None on failure)."""
        try:
            data = await self._request("GET", f"/nodes/{node}/lxc/{vmid}/interfaces")
        except ProxmoxAPIError:
            return None
        for iface in data or []:
            name = iface.get("name")
            inet = iface.get("inet")  # e.g. "192.168.1.50/24"
            if name and name != "lo" and inet:
                return inet.split("/")[0]
        return None

    async def get_qemu_ip(self, node: str, vmid: int) -> Optional[str]:
        """Best-effort primary IPv4 of a running VM via the guest agent."""
        try:
            data = await self._request(
                "GET", f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces"
            )
        except ProxmoxAPIError:
            return None
        result = data.get("result") if isinstance(data, dict) else data
        for iface in result or []:
            if iface.get("name") in ("lo", "lo0"):
                continue
            for addr in iface.get("ip-addresses") or []:
                ip = addr.get("ip-address")
                if addr.get("ip-address-type") == "ipv4" and ip and not ip.startswith("127."):
                    return ip
        return None

    # --- QEMU guest agent (used to run software install / update checks) ---
    async def agent_ping(self, node: str, vmid: int) -> Any:
        return await self._request("POST", f"/nodes/{node}/qemu/{vmid}/agent/ping")

    async def wait_agent(self, node: str, vmid: int, attempts: int = 60, delay: float = 5.0) -> bool:
        """Wait until the guest agent answers (cloud-init done, agent running)."""
        for _ in range(attempts):
            try:
                await self.agent_ping(node, vmid)
                return True
            except ProxmoxAPIError:
                pass
            await asyncio.sleep(delay)
        return False

    async def agent_exec(
        self, node: str, vmid: int, script: str, timeout: int = 1800
    ) -> AgentExecResult:
        """Run a bash script inside the VM via the guest agent and return output.

        The script is base64-encoded (safe alphabet only) and decoded inside the
        guest, so no quoting/encoding issues arise on the transport.
        """
        encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
        # Redirect the script's stdin/stdout/stderr to a temp file and only print
        # the captured output at the very end. Without this, long-running daemons
        # started during the script (sshd, dockerd, ...) inherit the guest agent's
        # output pipes and keep them open, so guest-exec-status never reports the
        # command as finished and the dashboard hangs on "installing".
        inner = (
            "out=$(mktemp); "
            f'{{ echo {encoded} | base64 -d | bash; }} >"$out" 2>&1 </dev/null; '
            'rc=$?; cat "$out"; rm -f "$out"; exit $rc'
        )
        started = await self._request(
            "POST",
            f"/nodes/{node}/qemu/{vmid}/agent/exec",
            data={"command": ["/bin/bash", "-c", inner]},
        )
        pid = started.get("pid") if isinstance(started, dict) else None
        if pid is None:
            raise ProxmoxAPIError("Guest-Agent: keine PID erhalten.")

        elapsed = 0.0
        interval = 3.0
        while elapsed < timeout:
            status = await self._request(
                "GET",
                f"/nodes/{node}/qemu/{vmid}/agent/exec-status",
                params={"pid": pid},
            ) or {}
            if status.get("exited"):
                return AgentExecResult(
                    exit_code=int(status.get("exitcode") or 0),
                    stdout=status.get("out-data", "") or "",
                    stderr=status.get("err-data", "") or "",
                )
            await asyncio.sleep(interval)
            elapsed += interval
        raise ProxmoxAPIError("Zeitüberschreitung bei einem Guest-Agent-Befehl.")


@lru_cache
def get_proxmox() -> ProxmoxClient:
    return ProxmoxClient(get_settings())
