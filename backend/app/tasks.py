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
import re
import urllib.parse
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .models import (
    ContainerCreateRequest,
    IPConfigMode,
    JobResponse,
    JobStatus,
    LogEntry,
    UpdateInfo,
)
from .community import script_url
from .config import get_settings
from .proxmox_client import ProxmoxAPIError, get_proxmox
from .software import APT_PRELUDE, build_install_script, build_unattended_upgrades_script
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

# Strong references to running background tasks. asyncio only keeps weak
# references, so without this a fire-and-forget task could be garbage collected
# mid-flight. Tasks remove themselves on completion.
_background_tasks: set = set()


def spawn(coro) -> "asyncio.Task":
    """Schedule a coroutine as a tracked background task."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


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


def build_ssh_pwauth_script() -> str:
    """Enable SSH password authentication inside the guest.

    Cloud images ship with ``PasswordAuthentication no``. When the user chose a
    password (not just an SSH key) we enable it via a drop-in that sorts before
    the cloud-init drop-in so it wins, and also fix any existing directives.
    """
    return (
        "#!/usr/bin/env bash\n"
        "set -e\n"
        "mkdir -p /etc/ssh/sshd_config.d\n"
        "printf 'PasswordAuthentication yes\\nKbdInteractiveAuthentication yes\\n'"
        " > /etc/ssh/sshd_config.d/00-pwauth.conf\n"
        "sed -ri 's/^[#[:space:]]*PasswordAuthentication.*/PasswordAuthentication yes/'"
        " /etc/ssh/sshd_config 2>/dev/null || true\n"
        "for f in /etc/ssh/sshd_config.d/*cloud-init*.conf; do\n"
        "  [ -e \"$f\" ] && sed -ri"
        " 's/^[#[:space:]]*PasswordAuthentication.*/PasswordAuthentication yes/' \"$f\" || true\n"
        "done\n"
        "systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || true\n"
    )


def build_autologin_script(username: str) -> str:
    """Configure console auto-login for ``username`` (VGA tty, serial, LXC console).

    Overrides the relevant getty units with an ``agetty --autologin`` ExecStart.
    The username is validated by the model (safe character set).
    """
    u = username
    tty = f"ExecStart=-/sbin/agetty --autologin {u} --noclear %I $TERM"
    serial = f"ExecStart=-/sbin/agetty --autologin {u} --keep-baud 115200,57600,38400,9600 %I $TERM"
    console = f"ExecStart=-/sbin/agetty --autologin {u} --noclear --keep-baud console 115200,57600,38400,9600 $TERM"

    def override(unit: str, execline: str) -> str:
        d = f"/etc/systemd/system/{unit}.d"
        return (
            f"mkdir -p {d}\n"
            f"cat > {d}/autologin.conf <<'EOF'\n"
            "[Service]\n"
            "ExecStart=\n"
            f"{execline}\n"
            "EOF\n"
        )

    return (
        "#!/usr/bin/env bash\n"
        "set -e\n"
        + override("getty@tty1.service", tty)  # VM VGA / noVNC console
        + override("serial-getty@ttyS0.service", serial)  # VM serial console
        + override("console-getty.service", console)  # LXC `pct console`
        + "systemctl daemon-reload 2>/dev/null || true\n"
        "systemctl restart getty@tty1 2>/dev/null || true\n"
        "systemctl restart serial-getty@ttyS0 2>/dev/null || true\n"
        "systemctl restart console-getty 2>/dev/null || true\n"
        "echo '==> Autologin konfiguriert'\n"
    )


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


_UPDATE_CHECK_SCRIPT = APT_PRELUDE + (
    "apt-get $APT_OPTS update >/dev/null 2>&1 || apt-get $APT_OPTS update\n"
    "apt list --upgradable 2>/dev/null || true\n"
)
_UPGRADE_SCRIPT = APT_PRELUDE + (
    "apt-get $APT_OPTS update\n"
    "apt-get $APT_OPTS -y upgrade\n"
)


async def _run_guest_script(job: "Job", script: str, timeout: int = 1800):
    """Run a script inside the guest: LXC via ``pct exec``, VM via guest agent.

    Both ``CommandResult`` (SSH) and ``AgentExecResult`` expose ``ok``/``stdout``/
    ``stderr``, so callers can treat the return value uniformly.
    """
    if job.type == "vm":
        proxmox = get_proxmox()
        return await proxmox.agent_exec(job.node, job.vmid, script, timeout=timeout)
    ssh = get_ssh()
    return await ssh.run_in_container(job.vmid, script, timeout=timeout)


async def _check_updates_for_job(job: "Job") -> List[UpdateInfo]:
    """Run apt update + list upgradable inside the guest and parse the result."""
    result = await _run_guest_script(job, _UPDATE_CHECK_SCRIPT, timeout=600)
    return parse_upgradable(result.stdout)


async def _finalize_updates(job: "Job", request: ContainerCreateRequest) -> None:
    """Install pending updates (optional), report what remains, then enable
    automatic security updates (optional).

    Ordering matters: enabling unattended-upgrades is the *last* apt step so the
    apt-daily timers (stopped by the prelude for speed) end up enabled again.
    Shared by the LXC and VM workflows.
    """
    if request.install_updates:
        manager.set_status(job, JobStatus.installing_updates, progress=78)
        manager.log(job, "info", "Installiere verfügbare Updates …")
        result = await _run_guest_script(job, _UPGRADE_SCRIPT, timeout=1800)
        manager.log_output(job, result.stdout)
        if not result.ok:
            raise ProxmoxAPIError(
                "Update-Installation fehlgeschlagen: "
                + (result.stderr.strip()[-300:] or "siehe Logs")
            )
        manager.log(job, "info", "Updates installiert.")

    manager.set_status(job, JobStatus.checking_updates, progress=88)
    job.updates = await _check_updates_for_job(job)
    job.updates_checked = True
    manager.log(job, "info", f"{len(job.updates)} Update(s) verbleibend.")

    if request.auto_security_updates:
        manager.log(job, "info", "Aktiviere automatische Sicherheitsupdates …")
        result = await _run_guest_script(job, build_unattended_upgrades_script(), timeout=600)
        if result.ok:
            manager.log(job, "info", "Automatische Sicherheitsupdates aktiv.")
        else:
            manager.log(
                job, "warning",
                "Automatische Sicherheitsupdates konnten nicht vollständig eingerichtet werden.",
            )


async def _configure_autologin_if_requested(job: "Job", request: ContainerCreateRequest) -> None:
    """Set up console auto-login inside the guest (LXC or VM) if requested."""
    if not request.console_autologin:
        return
    manager.log(job, "info", "Richte Konsolen-Autologin ein …")
    result = await _run_guest_script(job, build_autologin_script(request.username), timeout=120)
    if not result.ok:
        manager.log(job, "warning", "Autologin konnte nicht vollständig eingerichtet werden.")


async def _create_backup_if_requested(job: "Job", request: ContainerCreateRequest) -> None:
    """Optionally create a vzdump backup after provisioning. Non-fatal on failure
    (the guest itself was created successfully)."""
    if not request.backup_after_create or job.vmid is None:
        return
    proxmox = get_proxmox()
    storage = request.backup_storage or get_settings().proxmox_backup_storage
    try:
        manager.log(job, "info", f"Erstelle Backup auf '{storage}' …")
        upid = await proxmox.backup_guest(job.node, job.vmid, storage)
        await _await_task(proxmox, job.node, upid, job, timeout=3600)
        manager.log(job, "info", "Backup erstellt.")
    except (ProxmoxAPIError, SSHError) as exc:
        manager.log(job, "warning", f"Backup fehlgeschlagen: {exc}")


async def _resolve_deployment_node(proxmox, requested: Optional[str]) -> str:
    """Resolve the target node and fail with a clear message if it is unknown.

    Turns Proxmox's cryptic "hostname lookup 'pve' failed" into an understandable
    error and auto-selects the only node in single-node (homelab) setups.
    """
    nodes = [n.get("node") for n in await proxmox.get_nodes() if n.get("node")]
    if not nodes:
        raise ProxmoxAPIError("Keine Proxmox-Nodes gefunden.")
    target = requested or proxmox.default_node
    if target in nodes:
        return target
    # Single-node homelab: the configured default did not match -> use the only
    # node instead of failing. An explicitly requested node is never overridden.
    if not requested and len(nodes) == 1:
        logger.warning(
            "Konfigurierter Node '%s' nicht gefunden – nutze '%s'.", target, nodes[0]
        )
        return nodes[0]
    raise ProxmoxAPIError(
        f"Node '{target}' nicht gefunden. Verfügbare Nodes: {', '.join(nodes)}. "
        "Bitte PROXMOX_NODE anpassen oder einen Node auswählen."
    )


async def _await_task(proxmox, node: str, upid: str, job: "Job", timeout: int) -> dict:
    """Wait for a Proxmox task and surface any warnings into the job log."""
    status = await proxmox.wait_for_task(node, upid, timeout=timeout)
    exit_status = status.get("exitstatus")
    if exit_status and str(exit_status).upper().startswith("WARNINGS"):
        try:
            for line in await proxmox.get_task_log(node, upid):
                text = line.strip()
                if text and "warn" in text.lower():
                    manager.log(job, "warning", f"Proxmox-Warnung: {text}")
        except ProxmoxAPIError:
            pass  # the warning detail is optional; never fail because of it
    return status


# --- Workflows --------------------------------------------------------------
async def run_deployment(job_id: str, request: ContainerCreateRequest) -> None:
    """Dispatch to the LXC or VM provisioning workflow."""
    job = manager.get(job_id)
    if job is None:  # pragma: no cover - should not happen
        return
    if request.type == "vm":
        await _run_vm_deployment(job, request)
    else:
        await _run_lxc_deployment(job, request)


async def _run_lxc_deployment(job: "Job", request: ContainerCreateRequest) -> None:
    """Full LXC provisioning workflow. Updates the job as it progresses."""
    proxmox = get_proxmox()
    ssh = get_ssh()
    try:
        # Resolve and validate the target node up front for a clear error message.
        node = await _resolve_deployment_node(proxmox, request.node)
        job.node = node

        # 1. Create container
        manager.set_status(job, JobStatus.creating, progress=10)
        manager.log(job, "info", f"Erstelle LXC-Container auf Node '{node}'.")
        vmid = await proxmox.next_vmid()
        job.vmid = vmid
        manager.log(job, "info", f"Zugewiesene VMID: {vmid}")
        params = build_lxc_params(request, vmid)
        upid = await proxmox.create_lxc(node, params)
        await _await_task(proxmox, node, upid, job, timeout=600)
        manager.log(job, "info", f"Container {vmid} wurde erstellt.")

        # 2. Start container and wait until it is reachable via pct exec
        manager.set_status(job, JobStatus.starting, progress=40)
        upid = await proxmox.start_lxc(node, vmid)
        await _await_task(proxmox, node, upid, job, timeout=120)
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

        # Enable SSH password login when a password was provided.
        if request.password:
            manager.log(job, "info", "Aktiviere SSH-Passwort-Anmeldung.")
            await ssh.run_in_container(vmid, build_ssh_pwauth_script(), timeout=120)

        await _configure_autologin_if_requested(job, request)

        # 4. Install selected software
        manager.set_status(job, JobStatus.installing, progress=60)
        manager.log(job, "info", "Installiere ausgewählte Software …")
        install_result = await ssh.run_in_container(
            vmid, build_install_script(request.software, request.language), timeout=1800
        )
        manager.log_output(job, install_result.stdout)
        if not install_result.ok:
            raise SSHError(
                "Software-Installation fehlgeschlagen: "
                + (install_result.stderr.strip()[-300:] or "siehe Logs")
            )
        manager.log(job, "info", "Software-Installation abgeschlossen.")

        # 5. Install/check updates + enable automatic security updates
        await _finalize_updates(job, request)

        # 6. Optional backup
        await _create_backup_if_requested(job, request)

        manager.set_status(job, JobStatus.done, progress=100)
        manager.log(job, "info", "Bereitstellung erfolgreich abgeschlossen.")
    except (ProxmoxAPIError, SSHError) as exc:
        _fail(job, str(exc))
    except Exception as exc:  # pragma: no cover - safety net
        logger.exception("Unerwarteter Fehler bei der Bereitstellung")
        _fail(job, f"Unerwarteter Fehler: {exc}")


# --- VM helpers -------------------------------------------------------------
def build_vm_config(request: ContainerCreateRequest) -> dict:
    """Build the cloud-init + resource config applied to the cloned VM."""
    net = f"virtio,bridge={request.bridge}"
    if request.ip_config == IPConfigMode.static:
        ipconfig = f"ip={request.ip_address}"
        if request.gateway:
            ipconfig += f",gw={request.gateway}"
    else:
        ipconfig = "ip=dhcp"

    config: dict = {
        "cores": request.cores,
        "memory": request.memory_mb,
        "agent": 1,  # enable the QEMU guest agent (needed for software/updates)
        # Force a standard display so the Proxmox web console works. Cloud-init
        # templates are often set to "serial0", which leaves the noVNC console
        # blank. Cloud images also output to tty0, so this shows the login prompt.
        "vga": "std",
        # QEMU/VNC keyboard layout for the (no)VNC console. This is what actually
        # determines the layout when typing in the Proxmox console.
        "keyboard": "de" if request.language == "de" else "en-us",
        "net0": net,
        "ciuser": request.username,
        "ipconfig0": ipconfig,
        "onboot": request.autostart,
    }
    if request.password:
        config["cipassword"] = request.password
    if request.ssh_key:
        # Proxmox expects the sshkeys value to be URL-encoded.
        config["sshkeys"] = urllib.parse.quote(request.ssh_key.strip(), safe="")
    if request.description:
        config["description"] = request.description
    return config


def _detect_boot_disk(config: dict) -> Optional[str]:
    """Find the VM's primary disk key (for resizing) from its config."""
    match = re.search(r"order=([^,;]+)", config.get("boot", "") or "")
    if match:
        first = match.group(1).split(";")[0]
        if first in config:
            return first
    bootdisk = config.get("bootdisk")
    if bootdisk and bootdisk in config:
        return bootdisk
    for key in ("scsi0", "virtio0", "sata0", "ide0"):
        value = config.get(key)
        if value and "media=cdrom" not in value and "cloudinit" not in value:
            return key
    return None


async def _resize_vm_disk(proxmox, node: str, vmid: int, disk_gb: int, job: "Job") -> None:
    """Grow the VM's primary disk to the requested size (best effort)."""
    try:
        config = await proxmox.get_qemu_config(node, vmid)
        disk = _detect_boot_disk(config)
        if not disk:
            manager.log(job, "warning", "Boot-Disk nicht erkannt – Größe unverändert.")
            return
        await proxmox.resize_qemu_disk(node, vmid, disk, f"{disk_gb}G")
        manager.log(job, "info", f"Disk {disk} auf {disk_gb} GB gesetzt.")
    except ProxmoxAPIError as exc:
        # E.g. when the target is smaller than the template disk (cannot shrink).
        manager.log(job, "warning", f"Disk-Größe nicht angepasst: {exc}")


async def _run_vm_deployment(job: "Job", request: ContainerCreateRequest) -> None:
    """Full VM workflow: clone a cloud-init template, configure, install, update."""
    proxmox = get_proxmox()
    try:
        node = await _resolve_deployment_node(proxmox, request.node)
        job.node = node

        # 1. Clone the cloud-init template
        manager.set_status(job, JobStatus.creating, progress=10)
        vmid = await proxmox.next_vmid()
        job.vmid = vmid
        manager.log(
            job, "info",
            f"Klone VM-Template {request.vm_template_id} -> VMID {vmid} auf Node '{node}'.",
        )
        clone_params = {
            "newid": vmid,
            "name": request.hostname,
            "full": True,
            "storage": request.storage,
        }
        upid = await proxmox.clone_qemu(node, request.vm_template_id, clone_params)
        await _await_task(proxmox, node, upid, job, timeout=1800)
        manager.log(job, "info", f"VM {vmid} geklont.")

        # 2. Apply resources + cloud-init configuration, then resize the disk
        await proxmox.set_qemu_config(node, vmid, build_vm_config(request))
        manager.log(job, "info", "VM konfiguriert (Ressourcen + cloud-init).")
        await _resize_vm_disk(proxmox, node, vmid, request.disk_gb, job)

        # 3. Start and wait for the guest agent (cloud-init must finish first)
        manager.set_status(job, JobStatus.starting, progress=40)
        upid = await proxmox.start_qemu(node, vmid)
        await _await_task(proxmox, node, upid, job, timeout=120)
        manager.log(job, "info", "VM gestartet, warte auf Guest-Agent (cloud-init) …")
        if not await proxmox.wait_agent(node, vmid):
            raise ProxmoxAPIError(
                "Guest-Agent nicht erreichbar. Ist 'qemu-guest-agent' im Template "
                "installiert und cloud-init durchgelaufen?"
            )
        manager.log(job, "info", "Guest-Agent bereit.")

        # Enable SSH password login when a password was provided (cloud images
        # disable it by default).
        if request.password:
            manager.log(job, "info", "Aktiviere SSH-Passwort-Anmeldung.")
            await proxmox.agent_exec(node, vmid, build_ssh_pwauth_script(), timeout=120)

        await _configure_autologin_if_requested(job, request)

        # 4. Install selected software via the guest agent
        manager.set_status(job, JobStatus.installing, progress=60)
        manager.log(
            job, "info",
            "Installiere ausgewählte Software … "
            "(über den Guest-Agent ohne Zwischenausgabe)",
        )
        install_result = await proxmox.agent_exec(
            node, vmid, build_install_script(request.software, request.language), timeout=1800
        )
        manager.log_output(job, install_result.stdout)
        if not install_result.ok:
            raise ProxmoxAPIError(
                "Software-Installation fehlgeschlagen: "
                + (install_result.stderr.strip()[-300:] or "siehe Logs")
            )
        manager.log(job, "info", "Software-Installation abgeschlossen.")

        # 5. Install/check updates + enable automatic security updates
        await _finalize_updates(job, request)

        # 6. Optional backup
        await _create_backup_if_requested(job, request)

        manager.set_status(job, JobStatus.done, progress=100)
        manager.log(job, "info", "Bereitstellung erfolgreich abgeschlossen.")
    except (ProxmoxAPIError, SSHError) as exc:
        _fail(job, str(exc))
    except Exception as exc:  # pragma: no cover - safety net
        logger.exception("Unerwarteter Fehler bei der VM-Bereitstellung")
        _fail(job, f"Unerwarteter Fehler: {exc}")


async def run_install_updates(job_id: str) -> None:
    """Install available updates inside the guest (LXC or VM) of an existing job."""
    job = manager.get(job_id)
    if job is None or job.vmid is None:
        return
    try:
        manager.set_status(job, JobStatus.installing_updates, progress=50)
        manager.log(job, "info", "Installiere verfügbare Updates …")
        result = await _run_guest_script(job, _UPGRADE_SCRIPT, timeout=1800)
        manager.log_output(job, result.stdout)
        if not result.ok:
            raise ProxmoxAPIError(
                "Update-Installation fehlgeschlagen: "
                + (result.stderr.strip()[-300:] or "siehe Logs")
            )
        # Re-check remaining updates.
        job.updates = await _check_updates_for_job(job)
        job.updates_checked = True
        manager.set_status(job, JobStatus.done, progress=100)
        manager.log(job, "info", "Updates installiert.")
    except (ProxmoxAPIError, SSHError) as exc:
        _fail(job, str(exc))
    except Exception as exc:  # pragma: no cover
        logger.exception("Unerwarteter Fehler bei der Update-Installation")
        _fail(job, f"Unerwarteter Fehler: {exc}")


_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"           # CSI sequences (colors, cursor)
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"   # OSC sequences
    r"|\x1b[()*+][A-Za-z0-9]"               # charset selection (e.g. ESC ( B)
    r"|\x1b[@-Z\\-_]"                        # other 2-char escapes
)
_SPINNER_PREFIX = re.compile(r"^[^0-9A-Za-zÄÖÜäöüß]+")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


async def run_community_script(job_id: str, slug: str) -> None:
    """Run a community-scripts.org helper script on the Proxmox host via SSH.

    The script creates its own LXC. Output is streamed into the job log. This is
    best effort: many scripts are interactive and run remote code as root.
    """
    job = manager.get(job_id)
    if job is None:
        return
    ssh = get_ssh()
    try:
        url = script_url(slug)
        command = f'bash -c "$(wget -qLO - {url})"'
        manager.set_status(job, JobStatus.installing, progress=10)
        manager.log(job, "info", f"Starte Community-Script '{slug}' auf dem Proxmox-Host …")
        manager.log(job, "info", f"Befehl: {command}")
        manager.log(
            job, "warning",
            "Experimentell: interaktive Abfragen werden best-effort mit "
            "Standardeinstellungen beantwortet. Bei Problemen den Befehl manuell "
            "in der Proxmox-Shell ausführen.",
        )

        # Collapse repeated spinner frames (same message, rotating glyph) and skip
        # blank/escape-only lines, so the log stays readable.
        last_key = {"v": ""}

        def _on_output(text: str) -> None:
            line = _strip_ansi(text).rstrip()
            key = _SPINNER_PREFIX.sub("", line).strip()
            if not key or key == last_key["v"]:
                return
            last_key["v"] = key
            manager.log(job, "info", line.strip())

        exit_status = await ssh.run_pty_stream(command, _on_output, timeout=3600)
        if exit_status != 0:
            raise SSHError(f"Community-Script endete mit Exit-Code {exit_status}.")
        manager.set_status(job, JobStatus.done, progress=100)
        manager.log(job, "info", "Community-Script abgeschlossen.")
    except (ProxmoxAPIError, SSHError, ValueError) as exc:
        _fail(job, str(exc))
    except Exception as exc:  # pragma: no cover
        logger.exception("Unerwarteter Fehler beim Community-Script")
        _fail(job, f"Unerwarteter Fehler: {exc}")


def _fail(job: Job, message: str) -> None:
    job.error = message
    manager.set_status(job, JobStatus.error)
    manager.log(job, "error", message)
