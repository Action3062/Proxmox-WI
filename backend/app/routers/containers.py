"""Container creation and job/status routes."""

from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status

from ..auth import get_current_user
from ..models import ContainerCreateRequest, JobResponse, JobStatus, SoftwarePackage
from ..software import get_catalog, valid_ids
from ..tasks import manager, run_deployment, run_install_updates, spawn

router = APIRouter(tags=["containers"], dependencies=[Depends(get_current_user)])
logger = logging.getLogger(__name__)

# Job statuses during which a follow-up action must not be started.
_BUSY_STATUSES = {
    JobStatus.pending,
    JobStatus.creating,
    JobStatus.starting,
    JobStatus.installing,
    JobStatus.checking_updates,
    JobStatus.installing_updates,
}


@router.get("/software", response_model=List[SoftwarePackage])
def software_catalog() -> List[SoftwarePackage]:
    """Return the selectable software catalog (base + extras)."""
    return get_catalog()


@router.post("/containers", response_model=JobResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_container(request: ContainerCreateRequest) -> JobResponse:
    """Validate the request and kick off the asynchronous deployment workflow."""
    # Defence in depth: only known catalog IDs are accepted (no arbitrary pkgs).
    unknown = set(request.software) - valid_ids()
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unbekannte Softwarepakete: {', '.join(sorted(unknown))}",
        )

    job = manager.create(request.type, request)
    logger.info(
        "Bereitstellung gestartet (Job %s, Typ %s, Host '%s').",
        job.id, request.type, request.hostname,
    )
    # Run the long-lived workflow in the background; the client polls the job.
    spawn(run_deployment(job.id, request))
    return job.to_response()


@router.get("/jobs", response_model=List[JobResponse])
def list_jobs() -> List[JobResponse]:
    return [job.to_response() for job in manager.list()]


@router.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str) -> JobResponse:
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job nicht gefunden.")
    return job.to_response()


@router.post("/jobs/{job_id}/install-updates", response_model=JobResponse)
async def install_updates(job_id: str) -> JobResponse:
    """Install pending updates for a finished deployment (explicit user action)."""
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job nicht gefunden.")
    if job.vmid is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Für diesen Job existiert kein Container.",
        )
    if job.status in _BUSY_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Der Job ist noch aktiv. Bitte warten.",
        )
    logger.info("Update-Installation gestartet (Job %s).", job.id)
    spawn(run_install_updates(job.id))
    return job.to_response()
