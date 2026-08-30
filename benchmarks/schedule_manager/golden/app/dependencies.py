"""FastAPI dependency-injection adapters bridging HTTP requests to services.

This is the composition layer: it reads the lifespan-owned connection and
signing key off ``app.state``, builds repositories and services per request,
and resolves the bearer token to the current repository-owned user. Auth
failures raise the domain errors the application's exception handlers translate
to 401/403, so routers stay pure delegation.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated, cast

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .repositories.schedule_repository import ScheduleRepository
from .repositories.user_repository import UserRecord, UserRepository
from .security import current_user, require_admin
from .services.auth_service import AuthService, InvalidCredentialsError
from .services.schedule_service import ScheduleService

_bearer_scheme = HTTPBearer(auto_error=False)


def get_connection(request: Request) -> sqlite3.Connection:
    """Return the lifespan-owned SQLite connection for this request."""
    return cast(sqlite3.Connection, request.app.state.database_connection)


DatabaseConnection = Annotated[sqlite3.Connection, Depends(get_connection)]


def get_user_repository(connection: DatabaseConnection) -> UserRepository:
    """Provide a user repository over the request connection."""
    return UserRepository(connection)


UserRepositoryDependency = Annotated[UserRepository, Depends(get_user_repository)]


def get_schedule_repository(connection: DatabaseConnection) -> ScheduleRepository:
    """Provide a schedule repository over the request connection."""
    return ScheduleRepository(connection)


ScheduleRepositoryDependency = Annotated[ScheduleRepository, Depends(get_schedule_repository)]


def get_auth_service(request: Request, users: UserRepositoryDependency) -> AuthService:
    """Provide the auth service bound to the app's stable signing key."""
    signing_key = cast(bytes, request.app.state.signing_key)
    return AuthService(users, signing_key)


AuthServiceDependency = Annotated[AuthService, Depends(get_auth_service)]


def get_schedule_service(schedules: ScheduleRepositoryDependency) -> ScheduleService:
    """Provide the role-aware schedule service."""
    return ScheduleService(schedules)


ScheduleServiceDependency = Annotated[ScheduleService, Depends(get_schedule_service)]


def get_current_user(
    auth_service: AuthServiceDependency,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> UserRecord:
    """Resolve the bearer token to the current repository-owned user record."""
    if credentials is None:
        raise InvalidCredentialsError("authentication required")
    return current_user(credentials.credentials, auth_service)


CurrentUser = Annotated[UserRecord, Depends(get_current_user)]


def get_admin_user(user: CurrentUser) -> UserRecord:
    """Require that the current user holds the administrator role."""
    return require_admin(user)


AdminUser = Annotated[UserRecord, Depends(get_admin_user)]
