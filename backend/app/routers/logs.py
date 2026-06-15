"""Server log inspection route (admin only)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..auth import get_current_user
from ..logging_config import read_recent_logs

router = APIRouter(tags=["logs"], dependencies=[Depends(get_current_user)])


@router.get("/logs")
def get_logs(lines: int = 200) -> dict:
    """Return the most recent (already redacted) server log lines."""
    lines = max(1, min(lines, 2000))
    return {"lines": read_recent_logs(lines)}
