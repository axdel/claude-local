"""Minimal FastAPI application factory for the benchmark health tracer."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict

from .db import (
    DEFAULT_DATABASE_PATH,
    DatabasePath,
    connect_database,
    initialize_database,
)


class HealthResponse(BaseModel):
    """Strict response contract returned by the health endpoint."""

    model_config = ConfigDict(strict=True, extra="forbid")

    status: str


def create_app(database_path: DatabasePath = DEFAULT_DATABASE_PATH) -> FastAPI:
    """Create an isolated schedule-manager app with lifespan-owned storage."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        connection = connect_database(database_path)
        app.state.database_connection = connection
        try:
            initialize_database(connection)
            yield
        finally:
            app.state.__delattr__("database_connection")
            connection.close()

    app = FastAPI(title="Schedule Manager", lifespan=lifespan)
    app.state.database_path = database_path

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status="ok")

    return app
