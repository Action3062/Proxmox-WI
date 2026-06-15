"""Authentication routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from ..auth import authenticate, create_access_token, get_current_user
from ..models import LoginRequest, TokenResponse, User

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest) -> TokenResponse:
    """Validate credentials and issue a JWT access token."""
    user = authenticate(payload.username, payload.password)
    if user is None:
        # Log the attempt (username only) but never the password.
        logger.warning("Fehlgeschlagener Login-Versuch für '%s'.", payload.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Ungültige Zugangsdaten.",
        )
    token, expires_in = create_access_token(user)
    logger.info("Benutzer '%s' hat sich angemeldet.", user.username)
    return TokenResponse(access_token=token, expires_in=expires_in)


@router.get("/me", response_model=User)
def me(user: User = Depends(get_current_user)) -> User:
    return user
