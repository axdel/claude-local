"""Authentication endpoints: registration and login."""

from __future__ import annotations

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict

from ..dependencies import AuthServiceDependency
from ..schemas import UserCreate, UserCredentials, UserRead

_TOKEN_TYPE = "bearer"  # noqa: S105 — RFC 6750 token_type, not a credential

router = APIRouter(prefix="/auth", tags=["auth"])


class TokenResponse(BaseModel):
    """Bearer-token envelope returned by a successful login."""

    model_config = ConfigDict(strict=True, extra="forbid")

    access_token: str
    token_type: str = _TOKEN_TYPE


@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(credentials: UserCreate, auth_service: AuthServiceDependency) -> UserRead:
    """Register a normal user and return the public user record."""
    user = auth_service.register(credentials)
    return UserRead(id=user.id, username=user.username, role=user.role)


@router.post("/login")
def login(credentials: UserCredentials, auth_service: AuthServiceDependency) -> TokenResponse:
    """Authenticate credentials and return a signed bearer token."""
    user = auth_service.authenticate(credentials)
    return TokenResponse(access_token=auth_service.issue_token(user))
