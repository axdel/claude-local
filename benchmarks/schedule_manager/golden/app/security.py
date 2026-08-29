"""Identity resolution and role authorization for the golden app."""

from __future__ import annotations

from .repositories.user_repository import UserRecord
from .schemas import Role
from .services.auth_service import AuthService


class AdminRequiredError(PermissionError):
    """Raised when a non-administrator requests an administrator operation."""


def current_user(token: str, auth_service: AuthService) -> UserRecord:
    """Resolve a signed token to the current repository-owned user record."""
    return auth_service.verify_token(token)


def require_admin(user: UserRecord) -> UserRecord:
    """Return an administrator or reject a normal user."""
    if user.role is not Role.ADMIN:
        raise AdminRequiredError("administrator role required")
    return user
