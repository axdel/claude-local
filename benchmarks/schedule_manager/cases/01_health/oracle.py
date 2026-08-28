"""Immutable behavioral oracle for the minimal health application case."""

from inspect import signature

import app.db as database_defaults
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError


class _OracleDatabaseUrl(str):
    """Unique identity sentinel proving the factory default comes from the db neighbor."""


_ORACLE_DATABASE_URL = _OracleDatabaseUrl("sqlite:///oracle-health.db")
database_defaults.DEFAULT_DATABASE_URL = _ORACLE_DATABASE_URL

from app.main import HealthResponse, create_app


def test_health_factory_integrates_database_neighbor_and_isolates_apps() -> None:
    """The factory derives its default from app.db and keeps per-app state isolated."""
    default_parameter = signature(create_app).parameters["database_url"]
    assert default_parameter.annotation in {"str", str}
    assert signature(create_app).return_annotation in {"FastAPI", FastAPI}
    assert default_parameter.default is _ORACLE_DATABASE_URL

    default_app = create_app()
    overridden_app = create_app("sqlite:///second-health.db")

    assert default_app is not overridden_app
    assert default_app.state.database_url == _ORACLE_DATABASE_URL
    assert overridden_app.state.database_url == "sqlite:///second-health.db"


def test_health_endpoint_returns_declared_payload() -> None:
    """The in-process ASGI client observes the exact public health contract."""
    app = create_app()
    health_route = next(route for route in app.routes if getattr(route, "path", None) == "/health")
    assert getattr(health_route, "response_model", None) is HealthResponse

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_response_rejects_coercion_and_extra_fields() -> None:
    """The boundary schema rejects lax bytes coercion and an undeclared field."""
    with pytest.raises(ValidationError):
        HealthResponse.model_validate({"status": b"ok"})
    with pytest.raises(ValidationError):
        HealthResponse.model_validate({"status": "ok", "detail": "hidden"})
