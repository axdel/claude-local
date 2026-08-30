"""Immutable behavioral oracle for the strict schema-contracts case.

Drives the composed app through ``TestClient`` and asserts that the boundary
schemas accept valid payloads, reject unknown/mistyped/out-of-bounds fields in
strict mode, return the exact public response shapes, and enforce the
partial-update rule. Expected values are hand-derived from the spec
(AUTOINCREMENT ids start at 1; registration yields role ``user``; the user
response omits password material), never read back from the implementation.
"""

from app.main import create_app
from fastapi.testclient import TestClient

_DAILY_CRON = "0 9 * * *"
_ALICE = ("alice", "alice-password")


def _auth_headers(client: TestClient, credentials: tuple[str, str] = _ALICE) -> dict[str, str]:
    """Register then log a user in, returning their bearer authorization header."""
    username, password = credentials
    registered = client.post("/auth/register", json={"username": username, "password": password})
    assert registered.status_code == 201
    logged_in = client.post("/auth/login", json={"username": username, "password": password})
    assert logged_in.status_code == 200
    return {"Authorization": f"Bearer {logged_in.json()['access_token']}"}


def test_register_returns_public_user_and_hides_password() -> None:
    """A valid registration yields exactly the public user fields, with no password."""
    with TestClient(create_app()) as client:
        response = client.post(
            "/auth/register", json={"username": "alice", "password": "alice-password"}
        )

    assert response.status_code == 201
    assert response.json() == {"id": 1, "username": "alice", "role": "user"}


def test_register_rejects_unknown_field() -> None:
    """Strict credentials forbid an undeclared field such as a self-assigned role."""
    with TestClient(create_app()) as client:
        response = client.post(
            "/auth/register",
            json={"username": "alice", "password": "alice-password", "role": "admin"},
        )

    assert response.status_code == 422


def test_register_rejects_empty_username() -> None:
    """The username lower length bound rejects an empty string."""
    with TestClient(create_app()) as client:
        response = client.post(
            "/auth/register", json={"username": "", "password": "alice-password"}
        )

    assert response.status_code == 422


def test_create_schedule_returns_exact_read_shape() -> None:
    """A created schedule is returned as the full five-field public read model."""
    with TestClient(create_app()) as client:
        headers = _auth_headers(client)
        response = client.post(
            "/schedules",
            headers=headers,
            json={"name": "daily", "cron_expression": _DAILY_CRON, "enabled": True},
        )

    assert response.status_code == 201
    assert response.json() == {
        "id": 1,
        "owner_id": 1,
        "name": "daily",
        "cron_expression": _DAILY_CRON,
        "enabled": True,
    }


def test_create_schedule_defaults_enabled_to_true() -> None:
    """An omitted ``enabled`` field defaults to ``True``."""
    with TestClient(create_app()) as client:
        headers = _auth_headers(client)
        response = client.post(
            "/schedules",
            headers=headers,
            json={"name": "daily", "cron_expression": _DAILY_CRON},
        )

    assert response.status_code == 201
    assert response.json()["enabled"] is True


def test_create_schedule_rejects_unknown_field() -> None:
    """Strict schedule creation forbids an undeclared field such as ``owner_id``."""
    with TestClient(create_app()) as client:
        headers = _auth_headers(client)
        response = client.post(
            "/schedules",
            headers=headers,
            json={"name": "daily", "cron_expression": _DAILY_CRON, "owner_id": 5},
        )

    assert response.status_code == 422


def test_create_schedule_rejects_non_boolean_enabled() -> None:
    """Strict mode refuses to coerce the string ``"true"`` into a boolean."""
    with TestClient(create_app()) as client:
        headers = _auth_headers(client)
        response = client.post(
            "/schedules",
            headers=headers,
            json={"name": "daily", "cron_expression": _DAILY_CRON, "enabled": "true"},
        )

    assert response.status_code == 422


def test_update_rejects_empty_and_all_null_bodies() -> None:
    """An update with no supplied field, or only null fields, violates the one-field rule."""
    with TestClient(create_app()) as client:
        headers = _auth_headers(client)
        created = client.post(
            "/schedules",
            headers=headers,
            json={"name": "daily", "cron_expression": _DAILY_CRON},
        )
        schedule_id = created.json()["id"]
        empty = client.patch(f"/schedules/{schedule_id}", headers=headers, json={})
        all_null = client.patch(
            f"/schedules/{schedule_id}",
            headers=headers,
            json={"name": None, "cron_expression": None, "enabled": None},
        )

    assert empty.status_code == 422
    assert all_null.status_code == 422
