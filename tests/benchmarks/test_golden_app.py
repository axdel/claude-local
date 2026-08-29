"""Behavioral oracle for the whole composed golden schedule-manager app.

Drives the real ASGI app in-process through ``TestClient`` and asserts the full
contract every case oracle specializes: each endpoint across its success and
{400, 401, 403, 404, 409, 422} failure statuses, the persisted SQLite state
after every mutation (including that denied requests write nothing), the
response schema field-by-field, and the owner/admin RBAC boundaries. Expected
values are hand-derived from the spec (AUTOINCREMENT ids start at 1; register
always yields role ``user``; the error envelope is ``{"detail": ...}``), never
read back from the implementation under test.
"""

from collections.abc import Generator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from benchmarks.schedule_manager.golden.app.main import create_app
from benchmarks.schedule_manager.golden.app.repositories.user_repository import UserRepository
from benchmarks.schedule_manager.golden.app.schemas import Role
from benchmarks.schedule_manager.golden.app.services.auth_service import PasswordRecord

_ALICE = ("alice", "alice-password")
_BOB = ("bob", "bob-password")
_ADMIN = ("root", "root-password")

_DAILY_CRON = "0 9 * * *"
_WEEKLY_CRON = "30 6 * * 1"
_INVALID_CRON = "invalid"


@pytest.fixture
def app() -> FastAPI:
    """Return a fresh app backed by its own in-memory database."""
    return create_app()


@pytest.fixture
def client(app: FastAPI) -> Generator[TestClient]:
    """Yield a TestClient whose lifespan owns one isolated database."""
    with TestClient(app) as test_client:
        yield test_client


def _register(client: TestClient, credentials: tuple[str, str]) -> None:
    username, password = credentials
    response = client.post("/auth/register", json={"username": username, "password": password})
    assert response.status_code == 201


def _token(client: TestClient, credentials: tuple[str, str]) -> str:
    username, password = credentials
    response = client.post("/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200
    return response.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _register_and_auth(client: TestClient, credentials: tuple[str, str]) -> dict[str, str]:
    _register(client, credentials)
    return _auth(_token(client, credentials))


def _seed_admin(app: FastAPI, credentials: tuple[str, str]) -> int:
    """Persist an administrator directly; no endpoint creates one."""
    username, password = credentials
    password_hash = PasswordRecord.from_credentials(username, password).encode()
    record = UserRepository(app.state.database_connection).create(
        username,
        password_hash,
        Role.ADMIN,
    )
    return record.id


def _create_schedule(
    client: TestClient,
    headers: dict[str, str],
    *,
    name: str = "daily",
    cron_expression: str = _DAILY_CRON,
    enabled: bool = True,
) -> int:
    response = client.post(
        "/schedules",
        headers=headers,
        json={"name": name, "cron_expression": cron_expression, "enabled": enabled},
    )
    assert response.status_code == 201
    return response.json()["id"]


# --- health ----------------------------------------------------------------


def test_health_reports_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# --- registration ----------------------------------------------------------


def test_register_returns_public_user_without_password(client: TestClient) -> None:
    response = client.post(
        "/auth/register",
        json={"username": "alice", "password": "alice-password"},
    )
    assert response.status_code == 201
    assert response.json() == {"id": 1, "username": "alice", "role": "user"}
    assert "password_hash" not in response.json()


def test_register_second_user_gets_next_identifier(client: TestClient) -> None:
    _register(client, _ALICE)
    response = client.post("/auth/register", json={"username": "bob", "password": "bob-password"})
    assert response.status_code == 201
    assert response.json()["id"] == 2


def test_register_duplicate_username_conflicts_without_second_row(
    app: FastAPI,
    client: TestClient,
) -> None:
    _register(client, _ALICE)
    response = client.post(
        "/auth/register",
        json={"username": "alice", "password": "other-password"},
    )
    assert response.status_code == 409
    row = app.state.database_connection.execute("SELECT COUNT(*) FROM users").fetchone()
    assert row[0] == 1


def test_register_rejects_unknown_field(client: TestClient) -> None:
    response = client.post(
        "/auth/register",
        json={"username": "alice", "password": "alice-password", "role": "admin"},
    )
    assert response.status_code == 422


def test_register_rejects_missing_password(client: TestClient) -> None:
    response = client.post("/auth/register", json={"username": "alice"})
    assert response.status_code == 422


def test_register_rejects_empty_username(client: TestClient) -> None:
    response = client.post("/auth/register", json={"username": "", "password": "alice-password"})
    assert response.status_code == 422


# --- login -----------------------------------------------------------------


def test_login_issues_usable_bearer_token(client: TestClient) -> None:
    _register(client, _ALICE)
    response = client.post(
        "/auth/login",
        json={"username": "alice", "password": "alice-password"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"  # noqa: S105 — RFC 6750 token_type, not a credential
    assert isinstance(body["access_token"], str) and body["access_token"]
    listing = client.get("/schedules", headers=_auth(body["access_token"]))
    assert listing.status_code == 200


def test_login_wrong_password_is_unauthorized(client: TestClient) -> None:
    _register(client, _ALICE)
    response = client.post("/auth/login", json={"username": "alice", "password": "wrong"})
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid username or password"}


def test_login_unknown_user_is_unauthorized(client: TestClient) -> None:
    response = client.post("/auth/login", json={"username": "ghost", "password": "whatever"})
    assert response.status_code == 401


# --- create schedule -------------------------------------------------------


def test_create_schedule_persists_owned_by_current_user(client: TestClient) -> None:
    headers = _register_and_auth(client, _ALICE)
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
    fetched = client.get("/schedules/1", headers=headers)
    assert fetched.status_code == 200
    assert fetched.json()["id"] == 1


def test_create_schedule_honors_enabled_false(client: TestClient) -> None:
    headers = _register_and_auth(client, _ALICE)
    response = client.post(
        "/schedules",
        headers=headers,
        json={"name": "paused", "cron_expression": _WEEKLY_CRON, "enabled": False},
    )
    assert response.status_code == 201
    assert response.json()["enabled"] is False


def test_create_schedule_invalid_cron_is_rejected_and_not_persisted(client: TestClient) -> None:
    headers = _register_and_auth(client, _ALICE)
    response = client.post(
        "/schedules",
        headers=headers,
        json={"name": "broken", "cron_expression": _INVALID_CRON, "enabled": True},
    )
    assert response.status_code == 400
    listing = client.get("/schedules", headers=headers)
    assert listing.json() == []


def test_create_schedule_requires_authentication(client: TestClient) -> None:
    response = client.post(
        "/schedules",
        json={"name": "daily", "cron_expression": _DAILY_CRON, "enabled": True},
    )
    assert response.status_code == 401


def test_create_schedule_rejects_unknown_field(client: TestClient) -> None:
    headers = _register_and_auth(client, _ALICE)
    response = client.post(
        "/schedules",
        headers=headers,
        json={"name": "daily", "cron_expression": _DAILY_CRON, "owner_id": 5},
    )
    assert response.status_code == 422


# --- list schedules --------------------------------------------------------


def test_list_returns_only_the_callers_schedules(client: TestClient) -> None:
    alice = _register_and_auth(client, _ALICE)
    bob = _register_and_auth(client, _BOB)
    _create_schedule(client, alice, name="a1")
    _create_schedule(client, alice, name="a2")
    _create_schedule(client, bob, name="b1")
    response = client.get("/schedules", headers=alice)
    assert response.status_code == 200
    body = response.json()
    assert [item["name"] for item in body] == ["a1", "a2"]
    assert {item["owner_id"] for item in body} == {1}


def test_list_is_empty_for_a_new_user(client: TestClient) -> None:
    headers = _register_and_auth(client, _ALICE)
    response = client.get("/schedules", headers=headers)
    assert response.status_code == 200
    assert response.json() == []


def test_list_admin_sees_every_schedule(app: FastAPI, client: TestClient) -> None:
    alice = _register_and_auth(client, _ALICE)
    bob = _register_and_auth(client, _BOB)
    _create_schedule(client, alice, name="a1")
    _create_schedule(client, bob, name="b1")
    _seed_admin(app, _ADMIN)
    admin = _auth(_token(client, _ADMIN))
    response = client.get("/schedules", headers=admin)
    assert response.status_code == 200
    assert {item["name"] for item in response.json()} == {"a1", "b1"}


def test_list_requires_authentication(client: TestClient) -> None:
    assert client.get("/schedules").status_code == 401


# --- get schedule ----------------------------------------------------------


def test_get_missing_schedule_is_not_found(client: TestClient) -> None:
    headers = _register_and_auth(client, _ALICE)
    assert client.get("/schedules/999", headers=headers).status_code == 404


def test_get_other_users_schedule_is_forbidden(client: TestClient) -> None:
    alice = _register_and_auth(client, _ALICE)
    bob = _register_and_auth(client, _BOB)
    schedule_id = _create_schedule(client, alice)
    response = client.get(f"/schedules/{schedule_id}", headers=bob)
    assert response.status_code == 403


def test_get_admin_reads_any_schedule(app: FastAPI, client: TestClient) -> None:
    alice = _register_and_auth(client, _ALICE)
    schedule_id = _create_schedule(client, alice)
    _seed_admin(app, _ADMIN)
    admin = _auth(_token(client, _ADMIN))
    response = client.get(f"/schedules/{schedule_id}", headers=admin)
    assert response.status_code == 200
    assert response.json()["id"] == schedule_id


# --- patch schedule --------------------------------------------------------


def test_patch_updates_only_supplied_fields(client: TestClient) -> None:
    headers = _register_and_auth(client, _ALICE)
    schedule_id = _create_schedule(client, headers, name="before", cron_expression=_DAILY_CRON)
    response = client.patch(f"/schedules/{schedule_id}", headers=headers, json={"name": "after"})
    assert response.status_code == 200
    assert response.json() == {
        "id": schedule_id,
        "owner_id": 1,
        "name": "after",
        "cron_expression": _DAILY_CRON,
        "enabled": True,
    }


def test_patch_invalid_cron_is_rejected_and_leaves_state_unchanged(client: TestClient) -> None:
    headers = _register_and_auth(client, _ALICE)
    schedule_id = _create_schedule(client, headers, cron_expression=_DAILY_CRON)
    response = client.patch(
        f"/schedules/{schedule_id}",
        headers=headers,
        json={"cron_expression": _INVALID_CRON},
    )
    assert response.status_code == 400
    fetched = client.get(f"/schedules/{schedule_id}", headers=headers)
    assert fetched.json()["cron_expression"] == _DAILY_CRON


def test_patch_empty_body_is_rejected(client: TestClient) -> None:
    headers = _register_and_auth(client, _ALICE)
    schedule_id = _create_schedule(client, headers)
    assert client.patch(f"/schedules/{schedule_id}", headers=headers, json={}).status_code == 422


def test_patch_all_null_body_is_rejected(client: TestClient) -> None:
    headers = _register_and_auth(client, _ALICE)
    schedule_id = _create_schedule(client, headers)
    response = client.patch(
        f"/schedules/{schedule_id}",
        headers=headers,
        json={"name": None, "cron_expression": None, "enabled": None},
    )
    assert response.status_code == 422


def test_patch_missing_schedule_is_not_found(client: TestClient) -> None:
    headers = _register_and_auth(client, _ALICE)
    assert client.patch("/schedules/999", headers=headers, json={"name": "x"}).status_code == 404


def test_patch_other_users_schedule_is_forbidden_and_writes_nothing(client: TestClient) -> None:
    alice = _register_and_auth(client, _ALICE)
    bob = _register_and_auth(client, _BOB)
    schedule_id = _create_schedule(client, alice, name="original")
    response = client.patch(f"/schedules/{schedule_id}", headers=bob, json={"name": "hijacked"})
    assert response.status_code == 403
    fetched = client.get(f"/schedules/{schedule_id}", headers=alice)
    assert fetched.json()["name"] == "original"


def test_patch_admin_updates_any_schedule(app: FastAPI, client: TestClient) -> None:
    alice = _register_and_auth(client, _ALICE)
    schedule_id = _create_schedule(client, alice, name="original")
    _seed_admin(app, _ADMIN)
    admin = _auth(_token(client, _ADMIN))
    response = client.patch(f"/schedules/{schedule_id}", headers=admin, json={"name": "curated"})
    assert response.status_code == 200
    assert response.json()["name"] == "curated"


# --- delete schedule -------------------------------------------------------


def test_delete_removes_the_schedule(client: TestClient) -> None:
    headers = _register_and_auth(client, _ALICE)
    schedule_id = _create_schedule(client, headers)
    assert client.delete(f"/schedules/{schedule_id}", headers=headers).status_code == 204
    assert client.get(f"/schedules/{schedule_id}", headers=headers).status_code == 404


def test_delete_missing_schedule_is_not_found(client: TestClient) -> None:
    headers = _register_and_auth(client, _ALICE)
    assert client.delete("/schedules/999", headers=headers).status_code == 404


def test_delete_other_users_schedule_is_forbidden_and_writes_nothing(client: TestClient) -> None:
    alice = _register_and_auth(client, _ALICE)
    bob = _register_and_auth(client, _BOB)
    schedule_id = _create_schedule(client, alice)
    assert client.delete(f"/schedules/{schedule_id}", headers=bob).status_code == 403
    assert client.get(f"/schedules/{schedule_id}", headers=alice).status_code == 200


def test_delete_admin_removes_any_schedule(app: FastAPI, client: TestClient) -> None:
    alice = _register_and_auth(client, _ALICE)
    schedule_id = _create_schedule(client, alice)
    _seed_admin(app, _ADMIN)
    admin = _auth(_token(client, _ADMIN))
    assert client.delete(f"/schedules/{schedule_id}", headers=admin).status_code == 204
    assert client.get(f"/schedules/{schedule_id}", headers=alice).status_code == 404


def test_delete_schedule_requires_authentication(client: TestClient) -> None:
    assert client.delete("/schedules/1").status_code == 401


# --- admin: delete user ----------------------------------------------------


def test_admin_deletes_user_and_cascades_their_schedules(app: FastAPI, client: TestClient) -> None:
    alice = _register_and_auth(client, _ALICE)
    _create_schedule(client, alice)
    _seed_admin(app, _ADMIN)
    admin = _auth(_token(client, _ADMIN))
    response = client.delete("/users/1", headers=admin)
    assert response.status_code == 204
    assert (
        client.post(
            "/auth/login", json={"username": "alice", "password": "alice-password"}
        ).status_code
        == 401
    )
    remaining = client.get("/schedules", headers=admin)
    assert remaining.json() == []


def test_delete_user_requires_admin_and_writes_nothing(client: TestClient) -> None:
    alice = _register_and_auth(client, _ALICE)
    _register(client, _BOB)
    response = client.delete("/users/2", headers=alice)
    assert response.status_code == 403
    assert (
        client.post(
            "/auth/login", json={"username": "bob", "password": "bob-password"}
        ).status_code
        == 200
    )


def test_delete_user_missing_is_not_found(app: FastAPI, client: TestClient) -> None:
    _seed_admin(app, _ADMIN)
    admin = _auth(_token(client, _ADMIN))
    assert client.delete("/users/999", headers=admin).status_code == 404


def test_delete_user_requires_authentication(client: TestClient) -> None:
    assert client.delete("/users/1").status_code == 401


# --- token integrity at the HTTP boundary ----------------------------------


def test_malformed_token_is_unauthorized(client: TestClient) -> None:
    _register(client, _ALICE)
    response = client.get("/schedules", headers=_auth("not-a-real-token"))
    assert response.status_code == 401


def test_tampered_token_signature_is_unauthorized(client: TestClient) -> None:
    _register(client, _ALICE)
    token = _token(client, _ALICE)
    payload, signature = token.split(".")
    flipped = "0" if signature[0] != "0" else "1"
    tampered = f"{payload}.{flipped}{signature[1:]}"
    response = client.get("/schedules", headers=_auth(tampered))
    assert response.status_code == 401
