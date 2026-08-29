"""Immutable behavioral oracle for the authentication-service case.

Drives registration, login, and token use through the composed app via
``TestClient`` and asserts the full credential round-trip: a registered user can
log in and use the returned token, wrong or duplicate credentials are refused,
and an unsigned, tampered, or malformed token is rejected. Expected values are
hand-derived from the spec (registration yields role ``user`` and an id starting
at 1; wrong or unknown credentials → 401; duplicate registration → 409; a valid
token authorizes and a forged one does not; the signing key is exactly 32 bytes)
and from the stored-password rule (a registered password is never persisted in
plaintext), never read back from the implementation.
"""

import sqlite3

import pytest
from app.main import create_app
from app.repositories.user_repository import UserRepository
from app.services.auth_service import SIGNING_KEY_BYTES, AuthService
from fastapi.testclient import TestClient
from httpx import Response

_ALICE = ("alice", "alice-password")


def _register(client: TestClient, credentials: tuple[str, str] = _ALICE) -> Response:
    username, password = credentials
    return client.post("/auth/register", json={"username": username, "password": password})


def _login(client: TestClient, credentials: tuple[str, str] = _ALICE) -> Response:
    username, password = credentials
    return client.post("/auth/login", json={"username": username, "password": password})


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_registration_returns_public_user_with_first_id_and_user_role() -> None:
    """A valid registration yields exactly the public user fields with the user role."""
    with TestClient(create_app()) as client:
        response = _register(client)

    assert response.status_code == 201
    assert response.json() == {"id": 1, "username": "alice", "role": "user"}


def test_registered_password_is_not_stored_in_plaintext() -> None:
    """The persisted password material must not equal the plaintext password."""
    app = create_app()
    with TestClient(app) as client:
        _register(client)
        stored = app.state.database_connection.execute(
            "SELECT password_hash FROM users WHERE username = ?", ("alice",)
        ).fetchone()[0]

    assert stored != "alice-password"


def test_duplicate_registration_is_rejected_as_conflict() -> None:
    """Re-registering an existing username is a conflict, not a second user."""
    with TestClient(create_app()) as client:
        _register(client)
        second = _register(client)

    assert second.status_code == 409


def test_login_with_correct_password_returns_a_bearer_token() -> None:
    """Correct credentials yield a non-empty bearer access token."""
    with TestClient(create_app()) as client:
        _register(client)
        response = _login(client)

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"  # noqa: S105 — RFC 6750 token_type, not a credential
    assert body["access_token"]


def test_login_with_wrong_password_is_unauthorized() -> None:
    """A registered user with the wrong password is refused."""
    with TestClient(create_app()) as client:
        _register(client)
        response = _login(client, ("alice", "wrong-password"))

    assert response.status_code == 401


def test_login_for_unknown_user_is_unauthorized() -> None:
    """Credentials for a user who never registered are refused."""
    with TestClient(create_app()) as client:
        response = _login(client, ("ghost", "any-password"))

    assert response.status_code == 401


def test_issued_token_authorizes_a_protected_request() -> None:
    """A freshly issued token resolves to its user on a protected endpoint."""
    with TestClient(create_app()) as client:
        _register(client)
        token = _login(client).json()["access_token"]
        protected = client.get("/schedules", headers=_bearer(token))

    assert protected.status_code == 200
    assert protected.json() == []


def test_unsigned_identifier_is_not_accepted_as_a_token() -> None:
    """A bare user id with no signature must not authenticate (tokens are signed)."""
    with TestClient(create_app()) as client:
        _register(client)
        forged = client.get("/schedules", headers=_bearer("1"))

    assert forged.status_code == 401


def test_tampered_token_is_rejected() -> None:
    """Mutating one character of a valid token breaks its signature."""
    with TestClient(create_app()) as client:
        _register(client)
        token = _login(client).json()["access_token"]
        tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
        protected = client.get("/schedules", headers=_bearer(tampered))

    assert protected.status_code == 401


def test_malformed_token_is_rejected() -> None:
    """An arbitrary non-token string is refused rather than trusted."""
    with TestClient(create_app()) as client:
        response = client.get("/schedules", headers=_bearer("not-a-real-token"))

    assert response.status_code == 401


def test_signing_key_length_must_be_exactly_thirty_two_bytes() -> None:
    """The signing-key length is fixed at 32 bytes and enforced at construction."""
    assert SIGNING_KEY_BYTES == 32
    repository = UserRepository(sqlite3.connect(":memory:"))
    for wrong_length in (SIGNING_KEY_BYTES - 1, SIGNING_KEY_BYTES + 1):
        with pytest.raises(ValueError):
            AuthService(repository, b"\x00" * wrong_length)
