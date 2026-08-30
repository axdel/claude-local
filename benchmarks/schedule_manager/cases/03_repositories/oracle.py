"""Immutable behavioral oracle for the schedule persistence-boundary case.

Drives schedule CRUD through the composed app's ``/schedules`` endpoints via
``TestClient`` and asserts persistence by round-trip: what ``create`` stores,
``get``/``list`` return; a partial ``update`` changes only its supplied field;
``delete`` removes the row. Expected values are hand-derived from the spec
(AUTOINCREMENT ids start at 1; a new user owns id 1; insertion order is id
order; ``enabled`` round-trips as a bool), never read back from the
implementation.
"""

from app.main import create_app
from fastapi.testclient import TestClient

_DAILY_CRON = "0 9 * * *"
_WEEKLY_CRON = "30 6 * * 1"
_ALICE = ("alice", "alice-password")


def _auth_headers(client: TestClient, credentials: tuple[str, str] = _ALICE) -> dict[str, str]:
    """Register then log a user in, returning their bearer authorization header."""
    username, password = credentials
    registered = client.post("/auth/register", json={"username": username, "password": password})
    assert registered.status_code == 201
    logged_in = client.post("/auth/login", json={"username": username, "password": password})
    assert logged_in.status_code == 200
    return {"Authorization": f"Bearer {logged_in.json()['access_token']}"}


def _create(client: TestClient, headers: dict[str, str], **body: object) -> dict[str, object]:
    payload = {"name": "daily", "cron_expression": _DAILY_CRON, **body}
    response = client.post("/schedules", headers=headers, json=payload)
    assert response.status_code == 201
    return response.json()


def test_create_persists_and_assigns_sequential_ids() -> None:
    """Two creations persist with the first two AUTOINCREMENT identifiers."""
    with TestClient(create_app()) as client:
        headers = _auth_headers(client)
        first = _create(client, headers, name="a")
        second = _create(client, headers, name="b", cron_expression=_WEEKLY_CRON)

    assert first["id"] == 1
    assert second["id"] == 2


def test_created_schedule_round_trips_by_id_with_boolean_enabled() -> None:
    """A stored schedule is retrievable by id, with ``enabled`` mapped back to a bool."""
    with TestClient(create_app()) as client:
        headers = _auth_headers(client)
        created = _create(client, headers, name="daily", enabled=False)
        schedule_id = created["id"]
        fetched = client.get(f"/schedules/{schedule_id}", headers=headers)

    assert fetched.status_code == 200
    assert fetched.json() == {
        "id": 1,
        "owner_id": 1,
        "name": "daily",
        "cron_expression": _DAILY_CRON,
        "enabled": False,
    }


def test_list_for_owner_returns_schedules_in_insertion_order() -> None:
    """An owner's schedules list back in id (insertion) order."""
    with TestClient(create_app()) as client:
        headers = _auth_headers(client)
        for name in ("first", "second", "third"):
            _create(client, headers, name=name)
        listing = client.get("/schedules", headers=headers)

    assert listing.status_code == 200
    assert [item["name"] for item in listing.json()] == ["first", "second", "third"]


def test_update_changes_only_the_supplied_field() -> None:
    """A partial update rewrites only its supplied field and leaves the rest intact."""
    with TestClient(create_app()) as client:
        headers = _auth_headers(client)
        created = _create(client, headers, name="before", enabled=True)
        schedule_id = created["id"]
        patched = client.patch(
            f"/schedules/{schedule_id}", headers=headers, json={"name": "after"}
        )

    assert patched.status_code == 200
    assert patched.json() == {
        "id": 1,
        "owner_id": 1,
        "name": "after",
        "cron_expression": _DAILY_CRON,
        "enabled": True,
    }


def test_update_missing_schedule_reports_absent() -> None:
    """Updating an absent schedule surfaces as not found."""
    with TestClient(create_app()) as client:
        headers = _auth_headers(client)
        response = client.patch("/schedules/999", headers=headers, json={"name": "x"})

    assert response.status_code == 404


def test_delete_removes_the_persisted_schedule() -> None:
    """A deleted schedule is gone on the next read."""
    with TestClient(create_app()) as client:
        headers = _auth_headers(client)
        created = _create(client, headers)
        schedule_id = created["id"]
        deleted = client.delete(f"/schedules/{schedule_id}", headers=headers)
        fetched = client.get(f"/schedules/{schedule_id}", headers=headers)

    assert deleted.status_code == 204
    assert fetched.status_code == 404


def test_delete_missing_schedule_reports_absent() -> None:
    """Deleting an absent schedule surfaces as not found."""
    with TestClient(create_app()) as client:
        headers = _auth_headers(client)
        response = client.delete("/schedules/999", headers=headers)

    assert response.status_code == 404
