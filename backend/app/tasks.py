"""Background job management and the LXC deployment workflow.

A deployment is a multi-step, long running process (create -> start -> install
software -> check updates). It runs as an asyncio task and its progress is tracked
in an in-memory job store that the frontend polls. The store is deliberately
abstracted so it can be swapped for Redis/DB-backed persistence later.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from .models import (
    ContainerCreateRequest,
    IPConfigMode,
    JobResponse,
    JobStatus,
    LogEntry,
    UpdateInfo,
)
from .proxmox_client import ProxmoxAPIError, get_proxmox
from .software import build_install_script
from .ssh_client import SSHError, get_ssh

logger = logging.getLogger(__name__)

# Human readable, localized status text for each job status.
STEP_TEXT: Dict[JobStatus, str] = {
    JobStatus.pending: "In Warteschlange",
    JobStatus.creating: "Container wird erstellt",
    JobStatus.starting: "Container wird gestartet",
    JobStatus.installing: "Software wird installiert",
    JobStatus.checking_updates: "Updates werden geprüft",
    JobStatus.installing_updates: "Updates werden installiert",
    JobStatus.done: "Fertig",
    JobStatus.error: "Fehler",
}

_MAX_LOGS = 500  # keep the most recent log lines per job


class Job:
    """In-memory representation of a deployment/maintenance job."""

    def __init__(self, job_type: str, request: Optional[ContainerCreateRequest] = None):
        now = datetime.now(timezone.utc)
        self.id = uuid.uuid4().hex
        self.type = job_type
        self.status = JobStatus.pending
        self.progress = 0
        self.hostname: Optional[str] = request.hostname if request else None
        self.node: Optional[str] = None
        self.vmid: Optional[int] = None
        self.error: Optional[str] = None
        self.updates: List[UpdateInfo] = []
        self.updates_checked = False
        self.logs: List[LogEntry] = []
        self.request = request.safe_dict() if request else None
        self.created_at = now
        self.updated_at = now

    def to_response(self) -> JobResponse:
        return JobResponse(
            id=self.id,
            type=self.type,
            status=self.status,
            step=STEP_TEXT.get(self.status, ""),
            progress=self.progress,
            hostname=self.hostname,
            node=self.node,
            vmid=self.vmid,
            error=self.error,
            updates=self.updates,
            updates_checked=self.updates_checked,
            logs=self.logs,
            request=self.request,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


class JobManager:
    """Stores jobs and exposes simple update helpers."""

    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}

    def create(self, job_type: str, request: Optional[ContainerCreateRequest] = None) -> Job:
        job = Job(job_type, request)
        self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def list(self) -> List[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def set_status(self, job: Job, status: JobStatus, progress: Optional[int] = None) -> None:
        job.status = status
        if progress is not None:
            job.progress = progress
        job.updated_at = datetime.now(timezone.utc)

    def log(self, job: Job, level: str, message: str) -> None:
        # Truncate very long lines and cap the number of stored entries.
        message = message if len(message) <= 2000 else message[:2000] + " …"
        job.logs.append(
            LogEntry(timestamp=datetime.now(timezone.utc), level=level, message=message)
        )
        if len(job.logs) > _MAX_LOGS:
            job.logs = job.logs[-_MAX_LOGS:]
        job.updated_at = datetime.now(timezone.utc)
        logger.log(getattr(logging, level.upper(), logging.INFO), "[job %s] %s", job.id, message)

    def log_output(self, job: Job, output: str, tail: int = 120) -> None:
        """Append the (tail of) a command's output as individual log lines."""
        lines = [ln for ln in output.splitlines() if ln.strip()]
        for line in lines[-tail:]:
            self.log(job, "info", line)


manager = JobManager()


# --- Script builders --------------------------------------------------------
def build_lxc_params(request: ContainerCreateRequest, vmid: int) -> dict:
    """Translate a validated request into Proxmox ``create lxc`` parameters."""
    net = f"name=eth0,bridge={request.bridge}"
    if request.ip_config == IPConfigMode.static:
        net += f",ip={request.ip_address}"
        if request.gateway:
            net += f",gw={request.gateway}"
    else:
        net += ",ip=dhcp"

    # Running Docker inside an (unprivileged) LXC needs nesting + keyctl features.
    features = []
    if "docker" in request.software or "portainer" in request.software:
        features = ["nesting=1", "keyctl=1"]

    params: dict = {
        "vmid": vmid,
        "hostname": request.hostname,
        "ostemplate": request.template,
        "cores": request.cores,
        "memory": request.memory_mb,
        "swap": min(512, request.memory_mb),
        "rootfs": f"{request.storage}:{request.disk_gb}",
        "net0": net,
        "onboot": request.autostart,
        "unprivileged": 1,
        "start": 0,  # we start explicitly to track the task
    }
    if features:
        params["features"] = ",".join(features)
    if request.password:
        params["password"] = request.password
    if request.ssh_key:
        params["ssh-public-keys"] = request.ssh_key
    if request.description:
        params["description"] = request.description
    return params


def build_user_script(request: ContainerCreateRequest) -> str:
    """Create the requested (sudo) user inside the container.

    Secrets (password, SSH key) are base64-encoded inside the script and decoded
    at runtime, so they are never exposed to shell word-splitting or logs. The
    username is validated by the model (safe character set).
    """
    user = request.username
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        f"id -u {user} >/dev/null 2>&1 || useradd -m -s /bin/bash {user}",
        f"getent group sudo >/dev/null 2>&1 && usermod -aG sudo {user} || true",
    ]
    if request.password:
        cred = base64.b64encode(f"{user}:{request.password}".encode()).decode()
        lines.append(f"echo {cred} | base64 -d | chpasswd")
    if request.ssh_key:
        key = base64.b64encode((request.ssh_key.strip() + "\n").encode()).decode()
        lines += [
            f"mkdir -p /home/{user}/.ssh",
            f"echo {key} | base64 -d >> /home/{user}/.ssh/authorized_keys",
            f"chown -R {user}:{user} /home/{user}/.ssh",
            f"chmod 700 /home/{user}/.ssh",
            f"chmod 600 /home/{user}/.ssh/authorized_keys",
        ]
    return "\n".join(lines) + "\n"


def parse_upgradable(output: str) -> List[UpdateInfo]:
    """Parse the output of ``apt list --upgradable`` into structured entries."""
    updates: List[UpdateInfo] = []
    marker = "[upgradable from:"
    for line in output.splitlines():
        if marker not in line:
            continue
        try:
            name = line.split("/", 1)[0].strip()
            parts = line.split()
            candidate = parts[1] if len(parts) > 1 else None
            idx = line.find(marker)
            current = line[idx + len(marker):].strip().rstrip("]").strip()
            updates.append(UpdateInfo(name=name, current=current, candidate=candidate))
        except Exception:  # pragma: no cover - defensive parsing
            continue
    return updates


async def _check_updates(vmid: int) -> Tuple[List[UpdateInfo], str]:
    """Run ``apt update`` + ``apt list --upgradable`` inside the container."""
    ssh = get_ssh()
    script = (
        "export DEBIAN_FRONTEND=noninteractive\n"
        "apt-get update >/dev/null 2>&1 || apt-get update\n"
        "apt list --upgradable 2>/dev/null || true\n"
    )
    result = await ssh.run_in_container(vmid, script, timeout=300)
    return parse_upgradable(result.stdout), result.stdout


# --- Workflows --------------------------------------------------------------
async def run_deployment(job_id: str, request: ContainerCreateRequest) -> None:
    """Full LXC provisioning workflow. Updates the job as it progresses."""
    job = manager.get(job_id)
    if job is None:  # pragma: no cover - should not happen
        return
    proxmox = get_proxmox()
    ssh = get_ssh()
    node = request.node or proxmox.default_node
    job.node = node

    try:
        # 1. Create container
        manager.set_status(job, JobStatus.creating, progress=10)
        manager.log(job, "info", f"Erstelle LXC-Container auf Node '{node}'.")
        vmid = await proxmox.next_vmid()
        job.vmid = vmid
        manager.log(job, "info", f"Zugewiesene VMID: {vmid}")
        params = build_lxc_params(request, vmid)
        upid = await proxmox.create_lxc(node, params)
        await proxmox.wait_for_task(node, upid, timeout=600)
        manager.log(job, "info", f"Container {vmid} wurde erstellt.")

        # 2. Start container and wait until it is reachable via pct exec
        manager.set_status(job, JobStatus.starting, progress=40)
        upid = await proxmox.start_lxc(node, vmid)
        await proxmox.wait_for_task(node, upid, timeout=120)
        if not await ssh.wait_container_ready(vmid):
            raise SSHError("Container reagiert nicht (pct exec nicht verfügbar).")
        manager.log(job, "info", "Container läuft.")

        # 3. Create the requested user
        manager.log(job, "info", f"Lege Benutzer '{request.username}' an.")
        user_result = await ssh.run_in_container(vmid, build_user_script(request), timeout=120)
        if not user_result.ok:
            raise SSHError(
                "Benutzer konnte nicht angelegt werden: "
                + (user_result.stderr.strip()[:300] or "unbekannter Fehler")
            )

        # 4. Install selected software
        manager.set_status(job, JobStatus.installing, progress=60)
        manager.log(job, "info", "Installiere ausgewählte Software …")
        install_result = await ssh.run_in_container(
            vmid, build_install_script(request.software), timeout=1800
        )
        manager.log_output(job, install_result.stdout)
        if not install_result.ok:
            raise SSHError(
                "Software-Installation fehlgeschlagen: "
                + (install_result.stderr.strip()[-300:] or "siehe Logs")
            )
        manager.log(job, "info", "Software-Installation abgeschlossen.")

        # 5. Check for available updates (do not install automatically)
        manager.set_status(job, JobStatus.checking_updates, progress=85)
        updates, _ = await _check_updates(vmid)
        job.updates = updates
        job.updates_checked = True
        manager.log(job, "info", f"{len(updates)} Update(s) verfügbar.")

        manager.set_status(job, JobStatus.done, progress=100)
        manager.log(job, "info", "Bereitstellung erfolgreich abgeschlossen.")
    except (ProxmoxAPIError, SSHError) as exc:
        _fail(job, str(exc))
    except Exception as exc:  # pragma: no cover - safety net
        logger.exception("Unerwarteter Fehler bei der Bereitstellung")
        _fail(job, f"Unerwarteter Fehler: {exc}")


async def run_install_updates(job_id: str) -> None:
    """Install available updates inside the container of an existing job."""
    job = manager.get(job_id)
    if job is None or job.vmid is None:
        return
    ssh = get_ssh()
    try:
        manager.set_status(job, JobStatus.installing_updates, progress=50)
        manager.log(job, "info", "Installiere verfügbare Updates …")
        script = (
            "export DEBIAN_FRONTEND=noninteractive\n"
            "apt-get update\n"
            "apt-get -y upgrade\n"
        )
        result = await ssh.run_in_container(job.vmid, script, timeout=1800)
        manager.log_output(job, result.stdout)
        if not result.ok:
            raise SSHError(
                "Update-Installation fehlgeschlagen: "
                + (result.stderr.strip()[-300:] or "siehe Logs")
            )
        # Re-check remaining updates.
        updates, _ = await _check_updates(job.vmid)
        job.updates = updates
        job.updates_checked = True
        manager.set_status(job, JobStatus.done, progress=100)
        manager.log(job, "info", "Updates installiert.")
    except (ProxmoxAPIError, SSHError) as exc:
        _fail(job, str(exc))
    except Exception as exc:  # pragma: no cover
        logger.exception("Unerwarteter Fehler bei der Update-Installation")
        _fail(job, f"Unerwarteter Fehler: {exc}")


def _fail(job: Job, message: str) -> None:
    job.error = message
    manager.set_status(job, JobStatus.error)
    manager.log(job, "error", message)
