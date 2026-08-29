"""Behavioral oracles for the schedule-manager golden data layer."""

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from benchmarks.schedule_manager.golden.app import main as golden_main
from benchmarks.schedule_manager.golden.app.db import (
    DEFAULT_DATABASE_PATH,
    DatabasePath,
    connect_database,
    initialize_database,
)
from benchmarks.schedule_manager.golden.app.repositories.schedule_repository import (
    ScheduleRecord,
    ScheduleRepository,
)
from benchmarks.schedule_manager.golden.app.repositories.user_repository import (
    DuplicateUsernameError,
    UserRecord,
    UserRepository,
)
from benchmarks.schedule_manager.golden.app.schemas import (
    Role,
    ScheduleCreate,
    ScheduleRead,
    ScheduleUpdate,
    UserCreate,
    UserCredentials,
    UserRead,
)

_CREDENTIAL_TEXT = "correct"
_HASH_TEXT = "pbkdf2$hash"


def _managed_database(database_path: DatabasePath) -> closing[sqlite3.Connection]:
    """Return one explicitly closing test connection for a database path."""
    return closing(connect_database(database_path))


@pytest.mark.parametrize(
    "invalid_fields",
    [
        {"username": b"ada", "password": "correct horse"},
        {"username": "ada", "password": "correct\N{NULL}horse"},
        {"username": "ada", "password": "correct horse", "is_admin": True},
    ],
)
def test_user_create_rejects_coercion_and_unknown_fields(
    invalid_fields: dict[str, object],
) -> None:
    """User input rejects bytes coercion and undeclared privilege fields."""
    with pytest.raises(ValidationError):
        UserCreate.model_validate(invalid_fields)


def test_user_credentials_are_one_strict_registration_and_login_contract() -> None:
    """Registration and login share bounded, UTF-8-encodable credential fields."""
    assert UserCredentials(username="ada", password=_CREDENTIAL_TEXT).model_dump() == {
        "username": "ada",
        "password": _CREDENTIAL_TEXT,
    }
    assert UserCreate(username="ada", password=_CREDENTIAL_TEXT).model_dump() == {
        "username": "ada",
        "password": _CREDENTIAL_TEXT,
    }

    for invalid_credentials in (
        {"username": "", "password": _CREDENTIAL_TEXT},
        {"username": "ada", "password": ""},
        {"username": "a" * 65, "password": _CREDENTIAL_TEXT},
        {"username": "ada", "password": "p" * 129},
        {"username": "\ud800", "password": _CREDENTIAL_TEXT},
        {"username": "ada", "password": "\ud800"},
        {"username": "ada", "password": "password\N{NULL}"},
    ):
        with pytest.raises(ValidationError):
            UserCredentials.model_validate(invalid_credentials)


def test_user_read_is_strict_and_excludes_password_material() -> None:
    """Public user output carries only canonical identity and role fields."""
    assert UserRead(id=7, username="ada", role=Role.ADMIN).model_dump() == {
        "id": 7,
        "username": "ada",
        "role": "admin",
    }

    with pytest.raises(ValidationError):
        UserRead.model_validate({"id": "7", "username": "ada", "role": "admin"})
    with pytest.raises(ValidationError):
        UserRead.model_validate(
            {"id": 7, "username": "ada", "role": "owner", "password_hash": "secret"}
        )


def test_schedule_schemas_share_one_strict_field_contract() -> None:
    """Schedule input, output, and patch fields stay strict and consistently named."""
    assert ScheduleCreate(name="nightly", cron_expression="0 2 * * *").model_dump() == {
        "name": "nightly",
        "cron_expression": "0 2 * * *",
        "enabled": True,
    }
    assert ScheduleRead(
        id=11,
        owner_id=7,
        name="nightly",
        cron_expression="0 2 * * *",
        enabled=False,
    ).model_dump() == {
        "id": 11,
        "owner_id": 7,
        "name": "nightly",
        "cron_expression": "0 2 * * *",
        "enabled": False,
    }
    assert ScheduleUpdate(enabled=False).model_dump(exclude_unset=True) == {"enabled": False}

    with pytest.raises(ValidationError):
        ScheduleCreate.model_validate(
            {"name": "nightly", "cron_expression": "0 2 * * *", "enabled": 1}
        )
    with pytest.raises(ValidationError):
        ScheduleRead.model_validate(
            {
                "id": 11,
                "owner_id": 7,
                "name": "nightly",
                "cron_expression": "0 2 * * *",
                "enabled": True,
                "next_fire_at": "derived",
            }
        )
    with pytest.raises(ValidationError):
        ScheduleUpdate.model_validate({"owner_id": 99})


@pytest.mark.parametrize(
    "schedule_type, fields",
    [
        (ScheduleCreate, {"name": "", "cron_expression": "0 2 * * *"}),
        (ScheduleCreate, {"name": "nightly", "cron_expression": ""}),
        (ScheduleUpdate, {"name": None}),
        (ScheduleUpdate, {"cron_expression": None}),
        (ScheduleUpdate, {"enabled": None}),
        (ScheduleUpdate, {}),
    ],
)
def test_schedule_schemas_reject_empty_or_null_mutations(
    schedule_type: type[ScheduleCreate] | type[ScheduleUpdate],
    fields: dict[str, object],
) -> None:
    """Creates require content and patches require at least one concrete field."""
    with pytest.raises(ValidationError):
        schedule_type.model_validate(fields)


def test_default_database_path_is_consumable_by_the_connection_factory() -> None:
    """The advertised default opens an initialized in-memory database without translation."""
    with _managed_database(DEFAULT_DATABASE_PATH) as connection:
        initialize_database(connection)
        table_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    assert table_names == {"users", "schedules", "sqlite_sequence"}


def test_app_lifespan_initializes_and_closes_one_database_connection(tmp_path: Path) -> None:
    """The application owns initialized storage only for its running lifespan."""
    database_path = tmp_path / "lifespan.db"
    app = golden_main.create_app(database_path)

    assert not hasattr(app.state, "database_connection")
    with TestClient(app):
        connection = cast(sqlite3.Connection, app.state.database_connection)
        created_user = UserRepository(connection).create("ada", _HASH_TEXT)

    assert not hasattr(app.state, "database_connection")
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")
    with _managed_database(database_path) as reopened_connection:
        assert UserRepository(reopened_connection).get_by_id(created_user.id) == created_user


def test_app_lifespan_closes_connection_when_initialization_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A startup error propagates after the app detaches and closes its connection."""
    database_path = tmp_path / "failed-startup.db"
    app = golden_main.create_app(database_path)
    observed_connections: list[sqlite3.Connection] = []

    def fail_initialization(connection: sqlite3.Connection) -> None:
        observed_connections.append(connection)
        raise RuntimeError("schema initialization failed")

    monkeypatch.setattr(golden_main, "initialize_database", fail_initialization)

    with (
        pytest.raises(RuntimeError, match="schema initialization failed"),
        TestClient(app),
    ):
        pytest.fail("startup failure must prevent requests")

    assert len(observed_connections) == 1
    assert not hasattr(app.state, "database_connection")
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        observed_connections[0].execute("SELECT 1")


def test_repository_connection_works_through_fastapi_test_client(tmp_path: Path) -> None:
    """A repository assembled with the app remains usable in TestClient's worker thread."""
    connection = connect_database(tmp_path / "threaded.db")
    try:
        initialize_database(connection)
        users = UserRepository(connection)
        user = users.create("ada", "hash")
        app = FastAPI()

        @app.get("/user-count")
        def user_count() -> int:
            return int(users.get_by_id(user.id) is not None)

        with TestClient(app) as client:
            response = client.get("/user-count")

        assert response.status_code == 200
        assert response.json() == 1
    finally:
        connection.close()


def test_initialize_database_creates_relational_schema_and_enables_foreign_keys(
    tmp_path: Path,
) -> None:
    """One initialized connection exposes both tables and enforces owner references."""
    with _managed_database(tmp_path / "schedule.db") as connection:
        initialize_database(connection)
        table_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        foreign_keys_enabled = connection.execute("PRAGMA foreign_keys").fetchone()

        assert table_names == {"users", "schedules", "sqlite_sequence"}
        assert foreign_keys_enabled[0] == 1


def test_database_paths_are_isolated_and_schema_initialization_is_idempotent(
    tmp_path: Path,
) -> None:
    """Fresh paths share no rows and repeated initialization preserves existing state."""
    first_path = tmp_path / "first.db"
    second_path = tmp_path / "second.db"
    with _managed_database(first_path) as first_connection:
        initialize_database(first_connection)
        first_users = UserRepository(first_connection)
        first_user = first_users.create("ada", "hash")
        initialize_database(first_connection)
        preserved_user = first_users.get_by_id(first_user.id)

    with _managed_database(second_path) as second_connection:
        initialize_database(second_connection)
        second_users = UserRepository(second_connection)

        assert preserved_user == first_user
        assert second_users.get_by_username("ada") is None


def test_user_repository_round_trips_typed_records_by_id_and_username(
    tmp_path: Path,
) -> None:
    """User persistence returns the exact stored authentication and role fields."""
    with _managed_database(tmp_path / "users.db") as connection:
        initialize_database(connection)
        users = UserRepository(connection)

        created_user = users.create("ada", _HASH_TEXT, Role.ADMIN)

        assert created_user == UserRecord(
            id=1,
            username="ada",
            password_hash=_HASH_TEXT,
            role=Role.ADMIN,
        )
        assert users.get_by_id(created_user.id) == created_user
        assert users.get_by_username("ada") == created_user
        assert users.get_by_id(404) is None
        assert users.get_by_username("missing") is None


def test_user_repository_invalid_role_leaves_no_user_row(tmp_path: Path) -> None:
    """Runtime-invalid role input fails before commit and leaves registration retryable."""
    with _managed_database(tmp_path / "users.db") as connection:
        initialize_database(connection)
        users = UserRepository(connection)

        with pytest.raises(ValueError, match="not a valid Role"):
            users.create("ada", "invalid", cast(Role, "owner"))

        assert users.get_by_username("ada") is None
        assert users.create("ada", "valid", Role.USER).username == "ada"


def test_user_repository_duplicate_username_preserves_original_row(tmp_path: Path) -> None:
    """A unique-key failure creates no replacement or partial user row."""
    with _managed_database(tmp_path / "users.db") as connection:
        initialize_database(connection)
        users = UserRepository(connection)
        original_user = users.create("ada", "original", Role.USER)

        with pytest.raises(DuplicateUsernameError, match="ada"):
            users.create("ada", "replacement", Role.ADMIN)

        assert users.get_by_username("ada") == original_user
        assert users.get_by_id(original_user.id) == original_user


def test_schedule_repository_crud_round_trip_preserves_omitted_fields(tmp_path: Path) -> None:
    """Create, read, partial update, and delete expose exact typed persistence state."""
    with _managed_database(tmp_path / "schedules.db") as connection:
        initialize_database(connection)
        owner = UserRepository(connection).create("ada", "hash")
        schedules = ScheduleRepository(connection)

        created_schedule = schedules.create(owner.id, "nightly", "0 2 * * *")

        assert created_schedule == ScheduleRecord(
            id=1,
            owner_id=owner.id,
            name="nightly",
            cron_expression="0 2 * * *",
            enabled=True,
        )
        assert schedules.get_by_id(created_schedule.id) == created_schedule
        updated_schedule = schedules.update(
            created_schedule.id,
            ScheduleUpdate(enabled=False),
        )
        assert updated_schedule == ScheduleRecord(
            id=created_schedule.id,
            owner_id=owner.id,
            name="nightly",
            cron_expression="0 2 * * *",
            enabled=False,
        )
        assert schedules.delete(created_schedule.id) is True
        assert schedules.get_by_id(created_schedule.id) is None
        assert schedules.delete(created_schedule.id) is False
        assert schedules.update(created_schedule.id, ScheduleUpdate(name="gone")) is None


def test_schedule_partial_update_preserves_a_concurrent_unrelated_change(tmp_path: Path) -> None:
    """Updating enabled never rewrites a name committed concurrently by another connection."""
    database_path = tmp_path / "concurrent.db"
    first_connection = connect_database(database_path)
    second_connection = connect_database(database_path)
    try:
        initialize_database(first_connection)
        owner = UserRepository(first_connection).create("ada", "hash")
        first_schedules = ScheduleRepository(first_connection)
        second_schedules = ScheduleRepository(second_connection)
        schedule = first_schedules.create(owner.id, "before", "0 2 * * *")

        def commit_name_before_first_update(statement: str) -> None:
            if statement.startswith("UPDATE schedules SET"):
                first_connection.set_trace_callback(None)
                second_schedules.update(schedule.id, ScheduleUpdate(name="concurrent"))

        first_connection.set_trace_callback(commit_name_before_first_update)
        updated_schedule = first_schedules.update(
            schedule.id,
            ScheduleUpdate(enabled=False),
        )

        assert updated_schedule == ScheduleRecord(
            id=schedule.id,
            owner_id=owner.id,
            name="concurrent",
            cron_expression="0 2 * * *",
            enabled=False,
        )
        assert second_schedules.get_by_id(schedule.id) == updated_schedule
    finally:
        first_connection.close()
        second_connection.close()


def test_schedule_repository_lists_all_and_owner_scoped_in_id_order(tmp_path: Path) -> None:
    """Owner filtering excludes other users while global listing remains deterministic."""
    with _managed_database(tmp_path / "schedules.db") as connection:
        initialize_database(connection)
        users = UserRepository(connection)
        first_owner = users.create("ada", "hash")
        second_owner = users.create("grace", "hash")
        schedules = ScheduleRepository(connection)
        first_schedule = schedules.create(first_owner.id, "first", "0 1 * * *")
        other_schedule = schedules.create(second_owner.id, "other", "0 2 * * *")
        second_schedule = schedules.create(
            first_owner.id,
            "second",
            "0 3 * * *",
            enabled=False,
        )

        assert schedules.list_for_owner(first_owner.id) == [first_schedule, second_schedule]
        assert schedules.list_for_owner(404) == []
        assert schedules.list_all() == [first_schedule, other_schedule, second_schedule]


def test_schedule_repository_rejects_missing_owner_without_writing(tmp_path: Path) -> None:
    """Foreign-key rejection leaves the schedule table unchanged."""
    with _managed_database(tmp_path / "schedules.db") as connection:
        initialize_database(connection)
        schedules = ScheduleRepository(connection)

        with pytest.raises(sqlite3.IntegrityError):
            schedules.create(404, "orphan", "0 2 * * *")

        assert schedules.list_all() == []


def test_deleting_user_cascades_schedules_and_reports_missing_ids(tmp_path: Path) -> None:
    """The relational owner lifecycle removes dependent schedules atomically."""
    with _managed_database(tmp_path / "cascade.db") as connection:
        initialize_database(connection)
        users = UserRepository(connection)
        schedules = ScheduleRepository(connection)
        owner = users.create("ada", "hash")
        schedules.create(owner.id, "nightly", "0 2 * * *")

        assert users.delete(owner.id) is True
        assert users.delete(owner.id) is False
        assert users.get_by_id(owner.id) is None
        assert schedules.list_all() == []


def test_repository_values_are_data_not_sql(tmp_path: Path) -> None:
    """Injection-shaped user and schedule values round-trip without altering tables."""
    username = "ada'; DROP TABLE schedules; --"
    schedule_name = "nightly'; DELETE FROM users; --"
    with _managed_database(tmp_path / "parameters.db") as connection:
        initialize_database(connection)
        users = UserRepository(connection)
        schedules = ScheduleRepository(connection)
        owner = users.create(username, "hash")
        schedule = schedules.create(owner.id, schedule_name, "0 2 * * *")

        assert users.get_by_username(username) == owner
        assert users.get_by_id(owner.id) == owner
        assert schedules.get_by_id(schedule.id) == schedule
        assert schedules.list_all() == [schedule]
