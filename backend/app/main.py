"""FastAPI application entry point.

Wires together configuration, logging, CORS, authentication and the API routers.
The frontend is served separately (by nginx); this app only exposes ``/api``.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import __version__
from .config import get_settings
from .logging_config import setup_logging
from .proxmox_client import ProxmoxAPIError
from .routers import auth, containers, logs, proxmox
from .ssh_client import SSHError

setup_logging()
logger = logging.getLogger(__name__)
settings = get_settings()

app = FastAPI(title=settings.app_name, version=__version__)

# Restrict CORS to the configured origins (never a wildcard with credentials).
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.exception_handler(ProxmoxAPIError)
async def proxmox_error_handler(_: Request, exc: ProxmoxAPIError) -> JSONResponse:
    """Surface Proxmox API failures as a clean 502 with a readable message."""
    logger.error("Proxmox-API-Fehler: %s", exc)
    return JSONResponse(status_code=502, content={"detail": str(exc)})


@app.exception_handler(SSHError)
async def ssh_error_handler(_: Request, exc: SSHError) -> JSONResponse:
    logger.error("SSH-Fehler: %s", exc)
    return JSONResponse(status_code=502, content={"detail": str(exc)})


# All API routes live under /api so a single reverse proxy can route cleanly.
app.include_router(auth.router, prefix="/api")
app.include_router(proxmox.router, prefix="/api")
app.include_router(containers.router, prefix="/api")
app.include_router(logs.router, prefix="/api")


@app.get("/api/health", tags=["health"])
def health() -> dict:
    """Unauthenticated health check used by Docker and the reverse proxy."""
    return {"status": "ok", "version": __version__}


@app.on_event("startup")
async def _startup() -> None:
    logger.info("%s v%s gestartet.", settings.app_name, __version__)
    if not settings.proxmox_host:
        logger.warning("PROXMOX_HOST ist nicht gesetzt – API-Aufrufe schlagen fehl.")
