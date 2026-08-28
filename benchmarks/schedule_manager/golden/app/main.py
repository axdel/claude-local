"""Minimal FastAPI application factory for the benchmark health tracer."""

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict

from .db import DEFAULT_DATABASE_URL


class HealthResponse(BaseModel):
    """Strict response contract returned by the health endpoint."""

    model_config = ConfigDict(strict=True, extra="forbid")

    status: str


def create_app(database_url: str = DEFAULT_DATABASE_URL) -> FastAPI:
    """Create an isolated schedule-manager app exposing its health contract."""
    app = FastAPI(title="Schedule Manager")
    app.state.database_url = database_url

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status="ok")

    return app
