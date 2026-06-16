"""Async SSH client for the Proxmox host.

Used to run commands inside LXC containers via ``pct exec`` (software install,
update checks, user creation). Scripts are transferred base64-encoded and piped
into ``pct exec ... bash -s`` so that script content never appears on a shell
command line — this avoids any quoting/injection issues entirely.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Optional

import asyncssh

from .config import Settings, get_settings

logger = logging.getLogger(__name__)


class SSHError(Exception):
    """Raised when the SSH connection or a remote command fails fatally."""


@dataclass
class CommandResult:
    exit_status: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_status == 0


class ProxmoxSSH:
    """Thin async wrapper around asyncssh for the Proxmox host."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _connect_kwargs(self) -> dict:
        settings = self._settings
        host = settings.effective_ssh_host
        if not host:
            raise SSHError(
                "Kein SSH-Host konfiguriert (PROXMOX_SSH_HOST oder PROXMOX_HOST)."
            )
        kwargs: dict = {
            "host": host,
            "port": settings.proxmox_ssh_port,
            "username": settings.proxmox_ssh_user,
            # Host key checking is disabled for homelab use; document & harden
            # via a known_hosts file in production if desired.
            "known_hosts": None,
        }
        if settings.proxmox_ssh_key:
            kwargs["client_keys"] = [asyncssh.import_private_key(settings.proxmox_ssh_key)]
        elif settings.proxmox_ssh_key_file:
            kwargs["client_keys"] = [settings.proxmox_ssh_key_file]
        elif settings.proxmox_ssh_password:
            kwargs["password"] = settings.proxmox_ssh_password
        else:
            raise SSHError(
                "Keine SSH-Zugangsdaten konfiguriert (Key oder Passwort erforderlich)."
            )
        return kwargs

    async def run(self, command: str, timeout: int = 600) -> CommandResult:
        """Run a command on the Proxmox host and return its result."""
        try:
            async with asyncssh.connect(**self._connect_kwargs()) as conn:
                result = await conn.run(command, check=False, timeout=timeout)
        except asyncssh.Error as exc:
            raise SSHError(f"SSH-Fehler: {exc}") from exc
        except OSError as exc:
            raise SSHError(f"SSH-Verbindung fehlgeschlagen: {exc}") from exc
        return CommandResult(
            exit_status=result.exit_status or 0,
            stdout=str(result.stdout or ""),
            stderr=str(result.stderr or ""),
        )

    async def run_in_container(
        self, vmid: int, script: str, timeout: int = 1800
    ) -> CommandResult:
        """Execute a bash script inside the given LXC container.

        The script is base64-encoded and piped into ``pct exec``; only the
        (safe) base64 alphabet and the integer vmid appear on the command line.
        """
        encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
        command = (
            f"echo {encoded} | base64 -d | pct exec {int(vmid)} -- bash -s"
        )
        return await self.run(command, timeout=timeout)

    async def run_pty_stream(
        self,
        command: str,
        on_output: Callable[[str], None],
        autoaccept: bool = True,
        timeout: int = 3600,
    ) -> int:
        """Run a command on the host in a PTY, streaming output line by line.

        Used for the (interactive) community-scripts. ``on_output`` is called with
        each output line. When ``autoaccept`` is set, a few Enter keystrokes are
        sent early as a best-effort way to accept the "Use Default Settings"
        prompt. Returns the command's exit status.
        """

        async def _run() -> int:
            async with asyncssh.connect(**self._connect_kwargs()) as conn:
                proc = await conn.create_process(
                    command,
                    term_type="xterm",
                    term_size=(200, 50),
                    stderr=asyncssh.STDOUT,
                    encoding="utf-8",
                    errors="replace",
                )

                async def _feed() -> None:
                    # Best-effort: accept the default-settings whiptail prompt.
                    try:
                        for _ in range(3):
                            await asyncio.sleep(4)
                            proc.stdin.write("\r")
                    except Exception:  # pragma: no cover - stdin may be closed
                        pass

                feeder = asyncio.create_task(_feed()) if autoaccept else None
                buffer = ""
                try:
                    # Read in chunks (not lines): the community-scripts use \r
                    # based spinners for running steps, so line iteration would
                    # appear frozen. Convert \r to \n so each update is surfaced.
                    while True:
                        chunk = await proc.stdout.read(4096)
                        if not chunk:
                            break
                        buffer += chunk.replace("\r", "\n")
                        parts = buffer.split("\n")
                        buffer = parts.pop()  # keep the incomplete tail
                        for part in parts:
                            on_output(part)
                    if buffer:
                        on_output(buffer)
                finally:
                    if feeder:
                        feeder.cancel()
                await proc.wait_closed()
                return proc.exit_status or 0

        try:
            return await asyncio.wait_for(_run(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise SSHError("Zeitüberschreitung beim Ausführen des Scripts.") from exc
        except asyncssh.Error as exc:
            raise SSHError(f"SSH-Fehler: {exc}") from exc
        except OSError as exc:
            raise SSHError(f"SSH-Verbindung fehlgeschlagen: {exc}") from exc

    async def wait_container_ready(
        self, vmid: int, attempts: int = 30, delay: float = 2.0
    ) -> bool:
        """Wait until ``pct exec`` succeeds inside the container."""
        import asyncio

        for _ in range(attempts):
            try:
                result = await self.run(f"pct exec {int(vmid)} -- true", timeout=30)
                if result.ok:
                    return True
            except SSHError:
                pass
            await asyncio.sleep(delay)
        return False

    async def test_connection(self) -> CommandResult:
        """Lightweight connectivity check used by diagnostics."""
        return await self.run("pveversion || true", timeout=30)


@lru_cache
def get_ssh() -> ProxmoxSSH:
    return ProxmoxSSH(get_settings())
