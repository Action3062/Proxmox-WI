"""Guest management routes: list existing LXC/VMs and control them.

Provides a merged view of all containers and VMs across nodes plus power
actions (start/shutdown/reboot) and deletion.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status

from ..auth import get_current_user
from ..config import get_settings
from ..proxmox_client import get_proxmox

router = APIRouter(prefix="/guests", tags=["guests"], dependencies=[Depends(get_current_user)])
logger = logging.getLogger(__name__)

# Allowed power actions and the mapping from our type to the Proxmox path segment.
_ACTIONS = {"start", "stop", "shutdown", "reboot"}
_KIND_MAP = {"lxc": "lxc", "vm": "qemu"}


def _proxmox_kind(kind: str) -> str:
    if kind not in _KIND_MAP:
        raise HTTPException(status_code=400, detail="Ungültiger Gast-Typ.")
    return _KIND_MAP[kind]


def _resolve_node(node: Optional[str]) -> str:
    return node or get_settings().proxmox_node


def _format(gtype: str, node: str, data: dict) -> dict:
    return {
        "type": gtype,
        "node": node,
        "vmid": data.get("vmid"),
        "name": data.get("name"),
        "status": data.get("status"),
        "cpus": data.get("cpus"),
        "maxmem": data.get("maxmem"),
        "mem": data.get("mem"),
        "uptime": data.get("uptime"),
    }


@router.get("")
async def list_guests() -> List[dict]:
    """List all LXC containers and VMs (templates excluded) across all nodes."""
    proxmox = get_proxmox()
    guests: List[dict] = []
    for node_info in await proxmox.get_nodes():
        node = node_info.get("node")
        if not node:
            continue
        for container in await proxmox.list_lxc(node):
            guests.append(_format("lxc", node, container))
        for vm in await proxmox.list_qemu(node):
            if vm.get("template"):
                continue  # don't list templates as manageable guests
            guests.append(_format("vm", node, vm))
    guests.sort(key=lambda g: (g.get("vmid") or 0))

    # Resolve the live IP for running guests in parallel (best effort).
    async def _fill_ip(guest: dict) -> None:
        if guest.get("status") != "running":
            guest["ip"] = None
            return
        if guest["type"] == "lxc":
            guest["ip"] = await proxmox.get_lxc_ip(guest["node"], guest["vmid"])
        else:
            guest["ip"] = await proxmox.get_qemu_ip(guest["node"], guest["vmid"])

    await asyncio.gather(*(_fill_ip(g) for g in guests))
    return guests


@router.post("/{kind}/{vmid}/{action}", status_code=status.HTTP_202_ACCEPTED)
async def guest_action(
    kind: str, vmid: int, action: str, node: Optional[str] = None
) -> dict:
    """Run a power action on a guest. The action is issued asynchronously."""
    if action not in _ACTIONS:
        raise HTTPException(status_code=400, detail="Ungültige Aktion.")
    proxmox = get_proxmox()
    resolved = _resolve_node(node)
    logger.info("Gast-Aktion '%s' auf %s/%s (Node %s).", action, kind, vmid, resolved)
    upid = await proxmox.guest_action(resolved, _proxmox_kind(kind), vmid, action)
    return {"upid": upid}


@router.delete("/{kind}/{vmid}")
async def delete_guest(kind: str, vmid: int, node: Optional[str] = None) -> dict:
    """Delete a guest (must be stopped). Proxmox surfaces an error otherwise."""
    proxmox = get_proxmox()
    resolved = _resolve_node(node)
    logger.info("Gast löschen: %s/%s (Node %s).", kind, vmid, resolved)
    upid = await proxmox.delete_guest(resolved, _proxmox_kind(kind), vmid)
    return {"upid": upid}
