"""Behavioral and property oracles for golden services and security."""

from collections.abc import Generator
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from benchmarks.schedule_manager.golden.app.cron import (
    CronSearchExhaustedError,
    InvalidCronExpressionError,
    is_due,
    next_fire_at,
)
from benchmarks.schedule_manager.golden.app.db import connect_database, initialize_database
from benchmarks.schedule_manager.golden.app.repositories.schedule_repository import (
    ScheduleRepository,
)
from benchmarks.schedule_manager.golden.app.repositories.user_repository import UserRepository
from benchmarks.schedule_manager.golden.app.schemas import (
    Role,
    ScheduleCreate,
    ScheduleUpdate,
    UserCreate,
    UserCredentials,
)
from benchmarks.schedule_manager.golden.app.security import (
    AdminRequiredError,
    current_user,
    require_admin,
)
from benchmarks.schedule_manager.golden.app.services.auth_service import (
    AuthService,
    InvalidCredentialsError,
    UsernameAlreadyExistsError,
)
from benchmarks.schedule_manager.golden.app.services.schedule_service import (
    ScheduleAccessDeniedError,
    ScheduleNotFoundError,
    ScheduleService,
)

_SIGNING_KEY = bytes(range(32))
_CREDENTIAL_TEXT = "correct"
_REPLACEMENT_CREDENTIAL_TEXT = "replacement"
_EXPECTED_PBKDF2_RECORD = (
    "pbkdf2_sha256$10000$616461$0b6b2a39f1b92cc9834b0725ff65c3b434d2ac6b5917fe9e0953b117830bf8d4"
)
_ALTERNATE_SALT_PBKDF2_RECORD = (
    "pbkdf2_sha256$10000$6576696c$8feb2ebab4be2e5e9b3be38d661c42582fa5e48f5ec5bf59360ef935ddfd922c"
)
_EMPTY_SALT_PBKDF2_RECORD = (
    "pbkdf2_sha256$10000$$8c0522eddf27db47c1ba9bcb6d09f5a952100feb94bd73d3bfcf57930920eedb"
)
_EXPECTED_SIGNED_IDENTITY = "1.7761b1cc25227dfca0bd6d972acc52abb62f24ce50ad5a7a430b05c5a6f5497b"
_UNICODE_CHARACTERS = st.characters(exclude_categories=("Cs",))
_PASSWORD_CHARACTERS = st.characters(
    exclude_categories=("Cs",),
    exclude_characters=("\N{NULL}",),
)
_USERNAMES = st.text(_UNICODE_CHARACTERS, min_size=1, max_size=16)
_CREDENTIALS = st.text(_PASSWORD_CHARACTERS, min_size=1, max_size=16)


@contextmanager
def _auth_service(database_path: Path) -> Generator[AuthService]:
    """Yield one authentication service over an initialized closing repository."""
    with closing(connect_database(database_path)) as connection:
        initialize_database(connection)
        yield AuthService(UserRepository(connection), _SIGNING_KEY)


@contextmanager
def _schedule_services(
    database_path: Path,
) -> Generator[tuple[AuthService, ScheduleService]]:
    """Yield authentication and schedule services sharing one initialized database."""
    with closing(connect_database(database_path)) as connection:
        initialize_database(connection)
        yield (
            AuthService(UserRepository(connection), _SIGNING_KEY),
            ScheduleService(ScheduleRepository(connection)),
        )


def test_auth_registration_persists_independent_pbkdf2_vector_as_normal_user(
    tmp_path: Path,
) -> None:
    """Registration uses the published deterministic PBKDF2 format and cannot escalate role."""
    with _auth_service(tmp_path / "auth.db") as auth:
        registered_user = auth.register(UserCreate(username="ada", password=_CREDENTIAL_TEXT))

        assert registered_user.id == 1
        assert registered_user.username == "ada"
        assert registered_user.role is Role.USER
        assert registered_user.password_hash == _EXPECTED_PBKDF2_RECORD
        assert auth.users.get_by_username("ada") == registered_user


def test_auth_registration_rejects_duplicates_without_replacing_user(tmp_path: Path) -> None:
    """A duplicate registration remains a domain error and preserves persisted credentials."""
    with _auth_service(tmp_path / "auth.db") as auth:
        original_user = auth.register(UserCreate(username="ada", password=_CREDENTIAL_TEXT))

        with pytest.raises(UsernameAlreadyExistsError, match="ada"):
            auth.register(UserCreate(username="ada", password=_REPLACEMENT_CREDENTIAL_TEXT))

        assert auth.users.get_by_username("ada") == original_user


def test_authentication_uses_one_error_for_missing_user_and_wrong_password(
    tmp_path: Path,
) -> None:
    """Credential rejection does not reveal whether a username is registered."""
    with _auth_service(tmp_path / "auth.db") as auth:
        registered_user = auth.register(UserCreate(username="ada", password=_CREDENTIAL_TEXT))

        assert (
            auth.authenticate(UserCredentials(username="ada", password=_CREDENTIAL_TEXT))
            == registered_user
        )
        for username, credential_text in (
            ("missing", _CREDENTIAL_TEXT),
            ("ada", "wrong"),
        ):
            with pytest.raises(InvalidCredentialsError) as rejection:
                auth.authenticate(UserCredentials(username=username, password=credential_text))
            assert str(rejection.value) == "invalid username or password"


def test_auth_token_matches_independent_hmac_vector_and_reloads_current_user(
    tmp_path: Path,
) -> None:
    """A token signs only user identity while repository state remains authoritative."""
    with _auth_service(tmp_path / "auth.db") as auth:
        registered_user = auth.register(UserCreate(username="ada", password=_CREDENTIAL_TEXT))

        token = auth.issue_token(registered_user)

        assert token == _EXPECTED_SIGNED_IDENTITY
        assert auth.verify_token(token) == registered_user
        auth.users.delete(registered_user.id)
        with pytest.raises(InvalidCredentialsError, match="invalid token"):
            auth.verify_token(token)


def test_auth_token_rejects_tampering_malformed_payloads_and_wrong_key(tmp_path: Path) -> None:
    """Only a canonical positive identifier with the correct signature can identify a user."""
    with _auth_service(tmp_path / "auth.db") as auth:
        registered_user = auth.register(UserCreate(username="ada", password=_CREDENTIAL_TEXT))
        token = auth.issue_token(registered_user)
        wrong_key_auth = AuthService(auth.users, bytes(reversed(_SIGNING_KEY)))

        for invalid_token in (
            "",
            "1",
            ".signature",
            "0.signature",
            "-1.signature",
            "01.signature",
            "1.not-hex",
            "1.é",
            f"{registered_user.id + 1}.{token.split('.', 1)[1]}",
            f"{token[:-1]}0",
        ):
            with pytest.raises(InvalidCredentialsError, match="invalid token"):
                auth.verify_token(invalid_token)
        with pytest.raises(InvalidCredentialsError, match="invalid token"):
            wrong_key_auth.verify_token(token)


def test_security_reloads_current_role_and_requires_repository_admin(tmp_path: Path) -> None:
    """A signed caller role cannot override the repository-owned authorization role."""
    with _auth_service(tmp_path / "auth.db") as auth:
        registered_user = auth.register(UserCreate(username="ada", password=_CREDENTIAL_TEXT))
        forged_admin = replace(registered_user, role=Role.ADMIN)

        resolved_user = current_user(auth.issue_token(forged_admin), auth)

        assert resolved_user == registered_user
        with pytest.raises(AdminRequiredError, match="administrator"):
            require_admin(resolved_user)
        persisted_admin = auth.users.create("grace", "stored", Role.ADMIN)
        resolved_admin = current_user(auth.issue_token(persisted_admin), auth)
        assert require_admin(resolved_admin) == persisted_admin


@settings(max_examples=12, deadline=None)
@given(username=_USERNAMES, credential_text=_CREDENTIALS)
def test_authentication_properties_round_trip_unicode_and_reject_token_tampering(
    username: str,
    credential_text: str,
) -> None:
    """Valid Unicode credentials round-trip while any signature-nibble change is rejected."""
    with _auth_service(Path(":memory:")) as auth:
        registered_user = auth.register(UserCreate(username=username, password=credential_text))
        token = auth.issue_token(registered_user)
        signature = token.rsplit(".", 1)[1]
        replacement_nibble = "1" if signature[-1] == "0" else "0"
        tampered_token = f"{token[:-1]}{replacement_nibble}"

        assert (
            auth.authenticate(UserCredentials(username=username, password=credential_text))
            == registered_user
        )
        assert auth.verify_token(token) == registered_user
        with pytest.raises(InvalidCredentialsError, match="invalid token"):
            auth.verify_token(tampered_token)
        with pytest.raises(ValidationError, match="null character"):
            UserCredentials(
                username=username,
                password=f"{credential_text}\N{NULL}",
            )


@pytest.mark.parametrize(
    "stored_password",
    [
        "",
        "pbkdf2_sha256$10000$00",
        "other$10000$616461$" + "00" * 32,
        "pbkdf2_sha256$9999$616461$" + "00" * 32,
        "pbkdf2_sha256$10000$not-hex$" + "00" * 32,
        "pbkdf2_sha256$10000$616461$00",
        _ALTERNATE_SALT_PBKDF2_RECORD,
        _EMPTY_SALT_PBKDF2_RECORD,
        _EXPECTED_PBKDF2_RECORD.replace("$10000$", "$010000$"),
        ("pbkdf2_sha256$10000$616461$" + _EXPECTED_PBKDF2_RECORD.rsplit("$", 1)[1].upper()),
        f"{_EXPECTED_PBKDF2_RECORD} ",
    ],
)
def test_authentication_rejects_malformed_password_records(
    tmp_path: Path,
    stored_password: str,
) -> None:
    """Corrupt or unsupported stored password records never authenticate a caller."""
    with _auth_service(tmp_path / "auth.db") as auth:
        auth.users.create("ada", stored_password)

        with pytest.raises(InvalidCredentialsError, match="invalid username or password"):
            auth.authenticate(UserCredentials(username="ada", password=_CREDENTIAL_TEXT))


@pytest.mark.parametrize(
    "invalid_signing_key",
    [
        b"",
        b"key",
        b"key\x00",
        bytes(31),
        bytes(33),
    ],
)
def test_auth_service_requires_exactly_32_signing_key_bytes(
    tmp_path: Path,
    invalid_signing_key: bytes,
) -> None:
    """Token construction rejects weak lengths and HMAC-equivalent key aliases."""
    with closing(connect_database(tmp_path / "auth.db")) as connection:
        initialize_database(connection)
        with pytest.raises(ValueError, match="exactly 32 bytes"):
            AuthService(UserRepository(connection), invalid_signing_key)


def test_schedule_service_creates_for_actor_and_lists_by_current_role(tmp_path: Path) -> None:
    """Normal users see their own schedules while administrators see every schedule."""
    with _schedule_services(tmp_path / "schedules.db") as (auth, schedules):
        first_user = auth.register(UserCreate(username="ada", password=_CREDENTIAL_TEXT))
        second_user = auth.register(UserCreate(username="lin", password=_CREDENTIAL_TEXT))
        administrator = auth.users.create("grace", "stored", Role.ADMIN)

        first_schedule = schedules.create(
            first_user,
            ScheduleCreate(name="nightly", cron_expression="0 2 * * *"),
        )
        second_schedule = schedules.create(
            second_user,
            ScheduleCreate(name="hourly", cron_expression="0 * * * *", enabled=False),
        )

        assert first_schedule.owner_id == first_user.id
        assert second_schedule.owner_id == second_user.id
        assert schedules.list(first_user) == [first_schedule]
        assert schedules.list(second_user) == [second_schedule]
        assert schedules.list(administrator) == [first_schedule, second_schedule]


def test_schedule_service_rejects_invalid_create_and_missing_mutations_without_writes(
    tmp_path: Path,
) -> None:
    """Invalid creation and missing-ID mutations preserve every existing schedule."""
    with _schedule_services(tmp_path / "schedules.db") as (auth, schedules):
        owner = auth.register(UserCreate(username="ada", password=_CREDENTIAL_TEXT))
        existing_schedule = schedules.create(
            owner,
            ScheduleCreate(name="nightly", cron_expression="0 2 * * *"),
        )

        with pytest.raises(InvalidCronExpressionError):
            schedules.create(
                owner,
                ScheduleCreate(name="invalid", cron_expression="not cron"),
            )
        assert schedules.list(owner) == [existing_schedule]

        for missing_operation in (
            lambda: schedules.update(
                owner,
                404,
                ScheduleUpdate(cron_expression="not cron"),
            ),
            lambda: schedules.delete(owner, 404),
        ):
            with pytest.raises(ScheduleNotFoundError, match="404"):
                missing_operation()
            assert schedules.list(owner) == [existing_schedule]


def test_schedule_service_enforces_access_and_preserves_state_after_denials(
    tmp_path: Path,
) -> None:
    """Wrong-owner, missing, and invalid mutations leave the stored schedule unchanged."""
    with _schedule_services(tmp_path / "schedules.db") as (auth, schedules):
        owner = auth.register(UserCreate(username="ada", password=_CREDENTIAL_TEXT))
        outsider = auth.register(UserCreate(username="lin", password=_CREDENTIAL_TEXT))
        administrator = auth.users.create("grace", "stored", Role.ADMIN)
        original_schedule = schedules.create(
            owner,
            ScheduleCreate(name="nightly", cron_expression="0 2 * * *"),
        )

        assert schedules.get(owner, original_schedule.id) == original_schedule
        assert schedules.get(administrator, original_schedule.id) == original_schedule
        with pytest.raises(ScheduleAccessDeniedError, match="access denied"):
            schedules.get(outsider, original_schedule.id)
        with pytest.raises(ScheduleNotFoundError, match="404"):
            schedules.get(owner, 404)

        for denied_operation in (
            lambda: schedules.update(
                outsider,
                original_schedule.id,
                ScheduleUpdate(name="stolen"),
            ),
            lambda: schedules.delete(outsider, original_schedule.id),
        ):
            with pytest.raises(ScheduleAccessDeniedError, match="access denied"):
                denied_operation()
            assert schedules.get(owner, original_schedule.id) == original_schedule

        with pytest.raises(InvalidCronExpressionError):
            schedules.update(
                owner,
                original_schedule.id,
                ScheduleUpdate(cron_expression="not cron"),
            )
        assert schedules.get(owner, original_schedule.id) == original_schedule

        updated_schedule = schedules.update(
            administrator,
            original_schedule.id,
            ScheduleUpdate(name="admin-renamed", enabled=False),
        )
        assert updated_schedule.name == "admin-renamed"
        assert updated_schedule.cron_expression == original_schedule.cron_expression
        assert updated_schedule.enabled is False
        schedules.delete(administrator, original_schedule.id)
        with pytest.raises(ScheduleNotFoundError, match=str(original_schedule.id)):
            schedules.get(owner, original_schedule.id)


def test_schedule_service_derives_enabled_cron_behavior_and_disables_firing(
    tmp_path: Path,
) -> None:
    """Due and next-fire values derive from accessible state without persisted copies."""
    with _schedule_services(tmp_path / "schedules.db") as (auth, schedules):
        owner = auth.register(UserCreate(username="ada", password=_CREDENTIAL_TEXT))
        outsider = auth.register(UserCreate(username="lin", password=_CREDENTIAL_TEXT))
        enabled_schedule = schedules.create(
            owner,
            ScheduleCreate(name="nightly", cron_expression="35 14 * * *"),
        )
        disabled_schedule = schedules.create(
            owner,
            ScheduleCreate(
                name="paused",
                cron_expression="35 14 * * *",
                enabled=False,
            ),
        )
        selected_minute = datetime(2026, 8, 28, 14, 35, 45, tzinfo=UTC)

        assert schedules.is_due(owner, enabled_schedule.id, selected_minute) is True
        assert schedules.next_fire_at(owner, enabled_schedule.id, selected_minute) == datetime(
            2026,
            8,
            29,
            14,
            35,
            tzinfo=UTC,
        )
        assert schedules.is_due(owner, disabled_schedule.id, selected_minute) is False
        assert schedules.next_fire_at(owner, disabled_schedule.id, selected_minute) is None
        with pytest.raises(ScheduleAccessDeniedError, match="access denied"):
            schedules.is_due(outsider, enabled_schedule.id, selected_minute)
        with pytest.raises(ScheduleNotFoundError, match="404"):
            schedules.next_fire_at(owner, 404, selected_minute)


def test_cron_matches_wildcards_and_single_values_at_minute_granularity() -> None:
    """Cron matching combines all five UTC fields and ignores sub-minute precision."""
    selected_minute = datetime(2026, 8, 28, 14, 35, 59, 999_999, tzinfo=UTC)

    assert is_due("* * * * *", selected_minute) is True
    assert is_due("35 14 28 8 5", selected_minute) is True
    assert is_due("34 14 28 8 5", selected_minute) is False


def test_cron_supports_lists_ranges_steps_and_sunday_alias() -> None:
    """Each field expands its closed grammar and weekday seven aliases Sunday zero."""
    sunday = datetime(2026, 8, 30, 14, 35, tzinfo=UTC)

    assert is_due("5,35 9-17/5 */2 8,12 7", sunday) is True
    assert is_due("*/15 9-17/5 */2 8,12 0", sunday) is False
    assert is_due("35 9-17/5 1-31/2 8 0", sunday) is True


@pytest.mark.parametrize(
    "expression",
    [
        "* * * *",
        "* * * * * *",
        "60 * * * *",
        "* 24 * * *",
        "* * 0 * *",
        "* * * 13 *",
        "* * * * 8",
        "*/0 * * * *",
        "1-0 * * * *",
        "1,,2 * * * *",
        "1/2 * * * *",
        "* * * * sunday",
    ],
)
def test_cron_rejects_malformed_or_out_of_range_fields(expression: str) -> None:
    """Unsupported grammar fails loudly rather than silently broadening a schedule."""
    with pytest.raises(InvalidCronExpressionError):
        is_due(expression, datetime(2026, 8, 28, tzinfo=UTC))


def test_cron_rejects_naive_datetimes() -> None:
    """Evaluation never guesses a local timezone for an ambiguous reference instant."""
    with pytest.raises(ValueError, match="timezone-aware"):
        is_due("* * * * *", datetime(2026, 8, 28))
    with pytest.raises(ValueError, match="timezone-aware"):
        next_fire_at("* * * * *", datetime(2026, 8, 28))


def test_cron_uses_standard_day_or_semantics_and_strict_next_minute() -> None:
    """Restricted calendar days are ORed while next-fire excludes the reference minute."""
    friday = datetime(2026, 8, 28, 14, 35, 45, tzinfo=UTC)

    assert is_due("35 14 1 * 5", friday) is True
    assert is_due("35 14 28 * 1", friday) is True
    assert is_due("35 14 1 * 1", friday) is False
    assert next_fire_at("35 14 * * *", friday) == datetime(2026, 8, 29, 14, 35, tzinfo=UTC)


def test_cron_treats_stepped_star_day_field_as_starred_for_day_selection() -> None:
    """A star-based day field defers to the restricted peer even when its values match."""
    thursday = datetime(2026, 8, 27, 14, 35, tzinfo=UTC)

    assert is_due("35 14 */2 * 1", thursday) is False
    assert is_due("35 14 1 * */2", thursday) is False


def test_cron_next_fire_crosses_leap_year_and_timezone_boundaries() -> None:
    """UTC normalization and calendar traversal yield hand-derived leap-day occurrences."""
    utc_plus_two = timezone(timedelta(hours=2))
    reference = datetime(2027, 3, 1, 2, 0, tzinfo=utc_plus_two)

    assert next_fire_at("0 0 29 2 *", reference) == datetime(2028, 2, 29, tzinfo=UTC)


def test_cron_next_fire_rejects_calendar_impossibilities_after_bounded_search() -> None:
    """A valid but impossible calendar selection terminates instead of looping forever."""
    with pytest.raises(CronSearchExhaustedError, match="no occurrence"):
        next_fire_at("0 0 31 2 *", datetime(2026, 1, 1, tzinfo=UTC))
