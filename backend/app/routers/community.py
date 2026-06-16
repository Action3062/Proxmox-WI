"""Community-scripts.org integration: list suggestions and run a script.

Running a script executes remote code as root on the Proxmox host (it creates its
own LXC). The slug is strictly validated and only placed into the official repo
URL. This is an explicit, opt-in feature.
"""

from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, Depends, status

from ..auth import get_current_user
from ..community import suggestions
from ..models import CommunityScriptRequest, JobResponse
from ..tasks import manager, run_community_script, spawn

router = APIRouter(
    prefix="/community-scripts", tags=["community"], dependencies=[Depends(get_current_user)]
)
logger = logging.getLogger(__name__)


@router.get("")
def list_suggestions() -> List[dict]:
    """Curated suggestions; the user may also enter any other valid slug."""
    return suggestions()


@router.post("/run", response_model=JobResponse, status_code=status.HTTP_202_ACCEPTED)
async def run(request: CommunityScriptRequest) -> JobResponse:
    """Run the selected community-script on the Proxmox host (tracked as a job)."""
    job = manager.create("community-script")
    job.hostname = request.slug  # shown in the job list
    job.request = {"slug": request.slug}
    logger.info("Community-Script gestartet: '%s' (Job %s).", request.slug, job.id)
    spawn(run_community_script(job.id, request.slug))
    return job.to_response()
