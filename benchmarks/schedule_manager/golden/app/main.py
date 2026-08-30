"""FastAPI application factory for the composed schedule-manager golden app.

Wires the data, service, security, and router layers into one app, and owns the
single domain-error-to-HTTP-status mapping: services raise domain errors, and
the exception handlers registered here translate each to its status at the HTTP
boundary, keeping routers pure delegation.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from .cron import InvalidCronExpressionError
from .db import (
    DEFAULT_DATABASE_PATH,
    DatabasePath,
    connect_database,
    initialize_database,
)
from .routers import auth, schedules, users
from .security import AdminRequiredError
from .services.auth_service import (
    SIGNING_KEY_BYTES,
    InvalidCredentialsError,
    UsernameAlreadyExistsError,
)
from .services.schedule_service import ScheduleAccessDeniedError, ScheduleNotFoundError


class HealthResponse(BaseModel):
    """Strict response contract returned by the health endpoint."""

    model_config = ConfigDict(strict=True, extra="forbid")

    status: str


_DOMAIN_ERROR_STATUS: tuple[tuple[type[Exception], int], ...] = (
    (InvalidCredentialsError, 401),
    (AdminRequiredError, 403),
    (ScheduleAccessDeniedError, 403),
    (ScheduleNotFoundError, 404),
    (InvalidCronExpressionError, 400),
    (UsernameAlreadyExistsError, 409),
)


def _domain_error_handler(status_code: int) -> Callable[[Request, Exception], JSONResponse]:
    """Build a handler that renders one domain error as a consistent envelope."""

    def handle(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=status_code, content={"detail": str(exc)})

    return handle


def create_app(database_path: DatabasePath = DEFAULT_DATABASE_PATH) -> FastAPI:
    """Create an isolated, fully wired schedule-manager application."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        connection = connect_database(database_path)
        app.state.database_connection = connection
        try:
            initialize_database(connection)
            yield
        finally:
            delattr(app.state, "database_connection")
            connection.close()

    app = FastAPI(title="Schedule Manager", lifespan=lifespan)
    app.state.database_path = database_path
    app.state.signing_key = secrets.token_bytes(SIGNING_KEY_BYTES)

    for error_type, status_code in _DOMAIN_ERROR_STATUS:
        app.add_exception_handler(error_type, _domain_error_handler(status_code))

    app.include_router(auth.router)
    app.include_router(schedules.router)
    app.include_router(users.router)

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status="ok")

    return app
