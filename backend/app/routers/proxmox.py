"""Proxmox metadata routes (nodes, storages, bridges, templates).

These power the dropdowns in the creation form. All routes require auth.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..auth import get_current_user
from ..config import get_settings
from ..proxmox_client import ProxmoxAPIError, get_proxmox

router = APIRouter(
    prefix="/proxmox", tags=["proxmox"], dependencies=[Depends(get_current_user)]
)
logger = logging.getLogger(__name__)


def _resolve_node(node: Optional[str]) -> str:
    return node or get_settings().proxmox_node


def _parse_template(volid: str, size: Optional[int] = None) -> dict:
    """Extract OS/version metadata from a vztmpl volid."""
    filename = volid.split("/")[-1]
    parts = filename.split("-")
    os_name = parts[0].lower() if parts else "unknown"
    version = parts[1] if len(parts) > 1 else ""
    label = f"{os_name.capitalize()} {version}".strip()
    return {
        "volid": volid,
        "filename": filename,
        "os": os_name,
        "version": version,
        "label": label,
        "size": size,
    }


@router.get("/defaults")
def defaults() -> dict:
    """Configured default values used to pre-fill the creation form."""
    s = get_settings()
    return {
        "node": s.proxmox_node,
        "storage": s.proxmox_default_storage,
        "template_storage": s.proxmox_template_storage,
        "bridge": s.proxmox_default_bridge,
    }


@router.get("/nodes")
async def list_nodes() -> List[dict]:
    proxmox = get_proxmox()
    nodes = await proxmox.get_nodes()
    return [
        {"node": n.get("node"), "status": n.get("status")}
        for n in nodes
        if n.get("node")
    ]


@router.get("/storages")
async def list_storages(node: Optional[str] = None) -> List[dict]:
    proxmox = get_proxmox()
    storages = await proxmox.get_storages(_resolve_node(node))
    return [
        {
            "storage": s.get("storage"),
            "type": s.get("type"),
            "content": s.get("content", ""),
        }
        for s in storages
        if s.get("storage")
    ]


@router.get("/bridges")
async def list_bridges(node: Optional[str] = None) -> List[dict]:
    proxmox = get_proxmox()
    bridges = await proxmox.get_bridges(_resolve_node(node))
    return [
        {"name": b.get("iface"), "active": b.get("active")}
        for b in bridges
        if b.get("iface")
    ]


@router.get("/templates")
async def list_templates(
    node: Optional[str] = None, storage: Optional[str] = Query(default=None)
) -> List[dict]:
    """List LXC templates. If no storage is given, scan all vztmpl-capable ones."""
    proxmox = get_proxmox()
    resolved_node = _resolve_node(node)

    if storage:
        storages = [storage]
    else:
        all_storages = await proxmox.get_storages(resolved_node)
        storages = [
            s["storage"]
            for s in all_storages
            if "vztmpl" in (s.get("content") or "")
        ] or [get_settings().proxmox_template_storage]

    templates: List[dict] = []
    for store in storages:
        try:
            for item in await proxmox.get_templates(resolved_node, store):
                if item.get("volid"):
                    templates.append(_parse_template(item["volid"], item.get("size")))
        except ProxmoxAPIError:
            # A single unreadable storage should not break the whole listing.
            logger.warning("Templates konnten für Storage '%s' nicht gelesen werden.", store)
    return templates


@router.get("/next-vmid")
async def next_vmid() -> dict:
    proxmox = get_proxmox()
    return {"vmid": await proxmox.next_vmid()}


@router.get("/lxc")
async def list_lxc(node: Optional[str] = None) -> List[dict]:
    proxmox = get_proxmox()
    containers = await proxmox.list_lxc(_resolve_node(node))
    return [
        {
            "vmid": c.get("vmid"),
            "name": c.get("name"),
            "status": c.get("status"),
            "uptime": c.get("uptime"),
        }
        for c in containers
    ]
