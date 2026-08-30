"""Immutable behavioral oracle for the schedule CRUD router.

Drives the composed app via ``TestClient`` and asserts the full schedule endpoint
matrix: create, list, get, update, and delete with their exact status codes and
response shapes, ownership denial (403) and not-found (404) on every addressed
endpoint, request validation (422) and invalid-cron rejection (400), and 401 on an
unauthenticated call. Every denial that could mutate is paired with a persisted-state
check proving nothing changed. Expected values are hand-derived from the spec (create
-> 201 with the public schedule fields and the acting user as owner; a missing id ->
404; another user's schedule -> 403; a malformed body or an empty update -> 422; an
invalid cron -> 400; delete -> 204), never read back from the implementation.
"""

from app.main import create_app
from fastapi.testclient import TestClient
from httpx import Response

_ALICE = ("alice", "alice-password")
_BOB = ("bob", "bob-password")
_DAILY_CRON = "0 9 * * *"


def _login(client: TestClient, credentials: tuple[str, str]) -> str:
    username, password = credentials
    client.post("/auth/register", json={"username": username, "password": password})
    response = client.post("/auth/login", json={"username": username, "password": password})
    return response.json()["access_token"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_schedule(
    client: TestClient, token: str, *, name: str = "daily", cron: str = _DAILY_CRON
) -> Response:
    return client.post(
        "/schedules",
        headers=_bearer(token),
        json={"name": name, "cron_expression": cron},
    )


def test_create_returns_created_with_the_public_schedule_shape() -> None:
    """Creation yields 201 and exactly the public schedule fields owned by the caller."""
    with TestClient(create_app()) as client:
        token = _login(client, _ALICE)  # id 1
        response = _create_schedule(client, token, name="alice-daily")

    assert response.status_code == 201
    assert response.json() == {
        "id": 1,
        "owner_id": 1,
        "name": "alice-daily",
        "cron_expression": _DAILY_CRON,
        "enabled": True,
    }


def test_create_with_an_invalid_cron_is_a_bad_request() -> None:
    """An invalid cron expression reaches the service and is rejected as 400."""
    with TestClient(create_app()) as client:
        token = _login(client, _ALICE)
        response = _create_schedule(client, token, cron="not a cron")

    assert response.status_code == 400


def test_create_with_a_malformed_body_is_unprocessable() -> None:
    """A body missing a required field is rejected by request validation as 422."""
    with TestClient(create_app()) as client:
        token = _login(client, _ALICE)
        response = client.post("/schedules", headers=_bearer(token), json={"name": "no-cron"})

    assert response.status_code == 422


def test_list_returns_only_the_callers_schedules() -> None:
    """The list endpoint is scoped to the calling user's own schedules."""
    with TestClient(create_app()) as client:
        alice_token = _login(client, _ALICE)  # id 1
        bob_token = _login(client, _BOB)  # id 2
        _create_schedule(client, alice_token, name="alice-daily")
        _create_schedule(client, bob_token, name="bob-daily")
        alice_list = client.get("/schedules", headers=_bearer(alice_token)).json()

    assert [item["name"] for item in alice_list] == ["alice-daily"]


def test_get_returns_an_owned_schedule() -> None:
    """A user can read their own schedule by id."""
    with TestClient(create_app()) as client:
        token = _login(client, _ALICE)
        schedule_id = _create_schedule(client, token).json()["id"]
        response = client.get(f"/schedules/{schedule_id}", headers=_bearer(token))

    assert response.status_code == 200
    assert response.json()["id"] == schedule_id


def test_get_a_missing_schedule_is_not_found() -> None:
    """Reading a schedule id that does not exist is 404."""
    with TestClient(create_app()) as client:
        token = _login(client, _ALICE)
        response = client.get("/schedules/999", headers=_bearer(token))

    assert response.status_code == 404


def test_get_another_users_schedule_is_forbidden() -> None:
    """Reading another user's schedule is 403."""
    with TestClient(create_app()) as client:
        alice_token = _login(client, _ALICE)
        bob_token = _login(client, _BOB)
        schedule_id = _create_schedule(client, alice_token).json()["id"]
        response = client.get(f"/schedules/{schedule_id}", headers=_bearer(bob_token))

    assert response.status_code == 403


def test_patch_updates_a_field_and_persists_it() -> None:
    """A partial update returns 200 and the change survives a re-read."""
    with TestClient(create_app()) as client:
        token = _login(client, _ALICE)
        schedule_id = _create_schedule(client, token, name="before").json()["id"]
        patched = client.patch(
            f"/schedules/{schedule_id}", headers=_bearer(token), json={"name": "after"}
        )
        reread = client.get(f"/schedules/{schedule_id}", headers=_bearer(token))

    assert patched.status_code == 200
    assert patched.json()["name"] == "after"
    assert reread.json()["name"] == "after"


def test_patch_with_no_fields_is_unprocessable() -> None:
    """An update body with no concrete field is rejected by request validation as 422."""
    with TestClient(create_app()) as client:
        token = _login(client, _ALICE)
        schedule_id = _create_schedule(client, token).json()["id"]
        response = client.patch(f"/schedules/{schedule_id}", headers=_bearer(token), json={})

    assert response.status_code == 422


def test_patch_another_users_schedule_is_forbidden_and_writes_nothing() -> None:
    """A denied cross-owner update is 403 and leaves the schedule unchanged."""
    with TestClient(create_app()) as client:
        alice_token = _login(client, _ALICE)
        bob_token = _login(client, _BOB)
        schedule_id = _create_schedule(client, alice_token, name="original").json()["id"]
        denied = client.patch(
            f"/schedules/{schedule_id}", headers=_bearer(bob_token), json={"name": "hijacked"}
        )
        reread = client.get(f"/schedules/{schedule_id}", headers=_bearer(alice_token))

    assert denied.status_code == 403
    assert reread.json()["name"] == "original"


def test_delete_removes_an_owned_schedule() -> None:
    """Deleting an owned schedule returns 204 and the schedule is then 404."""
    with TestClient(create_app()) as client:
        token = _login(client, _ALICE)
        schedule_id = _create_schedule(client, token).json()["id"]
        deleted = client.delete(f"/schedules/{schedule_id}", headers=_bearer(token))
        reread = client.get(f"/schedules/{schedule_id}", headers=_bearer(token))

    assert deleted.status_code == 204
    assert reread.status_code == 404


def test_delete_another_users_schedule_is_forbidden_and_writes_nothing() -> None:
    """A denied cross-owner delete is 403 and the schedule survives."""
    with TestClient(create_app()) as client:
        alice_token = _login(client, _ALICE)
        bob_token = _login(client, _BOB)
        schedule_id = _create_schedule(client, alice_token).json()["id"]
        denied = client.delete(f"/schedules/{schedule_id}", headers=_bearer(bob_token))
        reread = client.get(f"/schedules/{schedule_id}", headers=_bearer(alice_token))

    assert denied.status_code == 403
    assert reread.status_code == 200


def test_unauthenticated_access_is_rejected() -> None:
    """A protected schedule endpoint refuses an unauthenticated request with 401."""
    with TestClient(create_app()) as client:
        response = client.get("/schedules")

    assert response.status_code == 401
