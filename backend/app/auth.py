"""Authentication: password hashing, JWT issuing/verification and FastAPI deps.

A single admin user is configured via environment variables for the minimal
version. The ``users`` store and the ``require_role`` dependency are structured so
that a database-backed, role-based user management can be added later.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import get_settings
from .models import User

logger = logging.getLogger(__name__)

# auto_error=False lets us return a clean 401 with our own message.
_bearer = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    """Hash a plaintext password with bcrypt."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time verification of a password against its bcrypt hash."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def _build_user_store() -> Dict[str, dict]:
    """Build the in-memory user store from configuration.

    Prefers a precomputed bcrypt hash; otherwise hashes the plaintext password
    once at startup. Returns an empty store if no admin credentials are set.
    """
    settings = get_settings()
    store: Dict[str, dict] = {}
    password_hash: Optional[str] = None

    if settings.admin_password_hash:
        password_hash = settings.admin_password_hash
    elif settings.admin_password:
        password_hash = hash_password(settings.admin_password)

    if password_hash:
        store[settings.admin_username] = {
            "username": settings.admin_username,
            "password_hash": password_hash,
            "role": "admin",
        }
    else:
        logger.warning(
            "Keine Admin-Zugangsdaten konfiguriert (ADMIN_PASSWORD oder "
            "ADMIN_PASSWORD_HASH) – Login ist nicht möglich."
        )
    return store


# Built once per process. Replace with a DB lookup for full user management.
_users: Dict[str, dict] = _build_user_store()


def authenticate(username: str, password: str) -> Optional[User]:
    """Return the User on valid credentials, otherwise None."""
    record = _users.get(username)
    if not record:
        return None
    if not verify_password(password, record["password_hash"]):
        return None
    return User(username=record["username"], role=record["role"])


def create_access_token(user: User) -> tuple[str, int]:
    """Create a signed JWT for the user. Returns (token, expires_in_seconds)."""
    settings = get_settings()
    expires_in = settings.access_token_expire_minutes * 60
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user.username,
        "role": user.role,
        "iat": now,
        "exp": now + timedelta(seconds=expires_in),
    }
    token = jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)
    return token, expires_in


def _decode_token(token: str) -> User:
    settings = get_settings()
    try:
        payload = jwt.decode(
            token, settings.secret_key, algorithms=[settings.jwt_algorithm]
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sitzung abgelaufen.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Ungültiges Token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    username = payload.get("sub")
    if not username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Ungültiges Token."
        )
    return User(username=username, role=payload.get("role", "admin"))


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> User:
    """FastAPI dependency that resolves the authenticated user from the JWT."""
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Nicht authentifiziert.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return _decode_token(credentials.credentials)


def require_role(*roles: str):
    """Dependency factory for role-based access control (future-proofing)."""

    def _checker(user: User = Depends(get_current_user)) -> User:
        if roles and user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Unzureichende Berechtigung.",
            )
        return user

    return _checker
