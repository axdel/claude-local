"""Immutable behavioral oracle for the RBAC (identity + authorization) case.

Drives the composed app via ``TestClient`` and asserts both halves of the
security layer: ``current_user`` resolves a valid token (and rejects a missing
or malformed one), and ``require_admin`` gates the administrator-only user
deletion endpoint while a normal user is scoped to their own schedules. Every
denial is paired with a persisted-state check proving nothing was mutated.
Expected values are hand-derived from the spec (no/invalid token → 401;
non-administrator on an admin route → 403; a normal user reaching another user's
existing schedule → 403; administrators bypass ownership and see all schedules),
never read back from the implementation. An administrator cannot be registered
(registration only mints normal users), so one is seeded directly with the
golden password scheme before logging in.
"""

from app.main import create_app
from app.repositories.user_repository import UserRepository
from app.schemas import Role
from app.services.auth_service import PasswordRecord
from fastapi import FastAPI
from fastapi.testclient import TestClient

_ALICE = ("alice", "alice-password")
_BOB = ("bob", "bob-password")
_ADMIN = ("root", "root-password")
_DAILY_CRON = "0 9 * * *"


def _register_and_login(client: TestClient, credentials: tuple[str, str]) -> str:
    username, password = credentials
    client.post("/auth/register", json={"username": username, "password": password})
    response = client.post("/auth/login", json={"username": username, "password": password})
    return response.json()["access_token"]


def _seed_admin_and_login(
    app: FastAPI, client: TestClient, credentials: tuple[str, str] = _ADMIN
) -> str:
    username, password = credentials
    password_hash = PasswordRecord.from_credentials(username, password).encode()
    UserRepository(app.state.database_connection).create(username, password_hash, Role.ADMIN)
    response = client.post("/auth/login", json={"username": username, "password": password})
    return response.json()["access_token"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _user_exists(app: FastAPI, user_id: int) -> bool:
    row = app.state.database_connection.execute(
        "SELECT 1 FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    return row is not None


def test_valid_user_token_authorizes_a_protected_request() -> None:
    """A resolved user token reaches a protected endpoint."""
    with TestClient(create_app()) as client:
        token = _register_and_login(client, _ALICE)
        response = client.get("/schedules", headers=_bearer(token))

    assert response.status_code == 200


def test_missing_token_is_unauthorized() -> None:
    """A protected endpoint refuses an unauthenticated request."""
    with TestClient(create_app()) as client:
        response = client.get("/schedules")

    assert response.status_code == 401


def test_malformed_token_is_unauthorized() -> None:
    """A protected endpoint refuses a token that resolves to no user."""
    with TestClient(create_app()) as client:
        response = client.get("/schedules", headers=_bearer("not-a-real-token"))

    assert response.status_code == 401


def test_normal_user_is_forbidden_from_admin_deletion_and_writes_nothing() -> None:
    """A non-administrator is denied the admin-only deletion, and no user is removed."""
    app = create_app()
    with TestClient(app) as client:
        alice_token = _register_and_login(client, _ALICE)  # id 1
        _register_and_login(client, _BOB)  # id 2 — the deletion target
        response = client.delete("/users/2", headers=_bearer(alice_token))
        target_present = _user_exists(app, 2)

    assert response.status_code == 403
    assert target_present


def test_administrator_may_delete_a_user() -> None:
    """An administrator passes the role gate and the target is removed."""
    app = create_app()
    with TestClient(app) as client:
        _register_and_login(client, _ALICE)  # id 1 — the deletion target
        admin_token = _seed_admin_and_login(app, client)  # id 2
        response = client.delete("/users/1", headers=_bearer(admin_token))
        target_gone = not _user_exists(app, 1)

    assert response.status_code == 204
    assert target_gone


def test_admin_deletion_requires_authentication() -> None:
    """The admin-only route refuses an unauthenticated caller before any deletion."""
    app = create_app()
    with TestClient(app) as client:
        _register_and_login(client, _ALICE)  # id 1
        response = client.delete("/users/1")
        target_present = _user_exists(app, 1)

    assert response.status_code == 401
    assert target_present


def test_normal_user_cannot_delete_another_users_schedule_and_writes_nothing() -> None:
    """A user is denied another user's schedule, and that schedule survives the denial."""
    app = create_app()
    with TestClient(app) as client:
        alice_token = _register_and_login(client, _ALICE)  # id 1
        bob_token = _register_and_login(client, _BOB)  # id 2
        created = client.post(
            "/schedules",
            headers=_bearer(alice_token),
            json={"name": "alice-daily", "cron_expression": _DAILY_CRON},
        )
        schedule_id = created.json()["id"]
        denied = client.delete(f"/schedules/{schedule_id}", headers=_bearer(bob_token))
        still_present = client.get(f"/schedules/{schedule_id}", headers=_bearer(alice_token))

    assert denied.status_code == 403
    assert still_present.status_code == 200


def test_list_scopes_to_owner_for_users_and_to_all_for_admin() -> None:
    """A normal user lists only their own schedules; an administrator lists everyone's."""
    app = create_app()
    with TestClient(app) as client:
        alice_token = _register_and_login(client, _ALICE)  # id 1
        bob_token = _register_and_login(client, _BOB)  # id 2
        client.post(
            "/schedules",
            headers=_bearer(alice_token),
            json={"name": "alice-daily", "cron_expression": _DAILY_CRON},
        )
        client.post(
            "/schedules",
            headers=_bearer(bob_token),
            json={"name": "bob-daily", "cron_expression": _DAILY_CRON},
        )
        admin_token = _seed_admin_and_login(app, client)  # id 3
        bob_list = client.get("/schedules", headers=_bearer(bob_token)).json()
        admin_list = client.get("/schedules", headers=_bearer(admin_token)).json()

    assert [item["owner_id"] for item in bob_list] == [2]
    assert sorted(item["owner_id"] for item in admin_list) == [1, 2]
