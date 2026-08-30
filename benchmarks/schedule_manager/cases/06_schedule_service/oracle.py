"""Immutable behavioral oracle for the role-aware schedule service.

Constructs the service over a real SQLite-backed repository with seeded users and
asserts its contract directly — the ``is_due`` and ``next_fire_at`` methods have no
HTTP endpoint, so this rung tests the unit itself rather than the API. Coverage:
role-aware listing and access, cron validation, partial update and delete with the
precise domain errors, and derived due / next-fire behavior. Expected values are
hand-derived from the spec (an administrator sees every schedule while a user sees
only their own; a cross-owner access raises access-denied and a missing id raises
not-found; an invalid cron is rejected; the daily ``0 9 * * *`` schedule fires at
09:00 UTC and its next occurrence after 08:00 UTC is that same day at 09:00, while a
disabled schedule is never due and has no next fire), never read back from the
implementation.
"""

from datetime import UTC, datetime

import pytest
from app.cron import InvalidCronExpressionError
from app.db import connect_database, initialize_database
from app.repositories.schedule_repository import ScheduleRepository
from app.repositories.user_repository import UserRecord, UserRepository
from app.schemas import Role, ScheduleCreate, ScheduleUpdate
from app.services.schedule_service import (
    ScheduleAccessDeniedError,
    ScheduleNotFoundError,
    ScheduleService,
)

_DAILY_9AM = "0 9 * * *"


def _service_with_users() -> tuple[ScheduleService, UserRecord, UserRecord, UserRecord]:
    """Build the service over a fresh DB seeded with two users and one administrator."""
    connection = connect_database(":memory:")
    initialize_database(connection)
    users = UserRepository(connection)
    alice = users.create("alice", "hash", Role.USER)  # id 1
    bob = users.create("bob", "hash", Role.USER)  # id 2
    admin = users.create("root", "hash", Role.ADMIN)  # id 3
    return ScheduleService(ScheduleRepository(connection)), alice, bob, admin


def _daily(name: str = "daily", *, enabled: bool = True) -> ScheduleCreate:
    return ScheduleCreate(name=name, cron_expression=_DAILY_9AM, enabled=enabled)


def test_created_schedule_is_owned_by_the_acting_user_and_persisted() -> None:
    """Creation stamps the acting user as owner and the record is retrievable."""
    service, alice, _bob, _admin = _service_with_users()
    created = service.create(alice, _daily("alice-daily"))
    assert created.owner_id == alice.id
    assert created.name == "alice-daily"
    assert service.get(alice, created.id) == created


def test_create_rejects_an_invalid_cron_expression() -> None:
    """A syntactically invalid cron expression is refused before persistence."""
    service, alice, _bob, _admin = _service_with_users()
    with pytest.raises(InvalidCronExpressionError):
        service.create(alice, ScheduleCreate(name="bad", cron_expression="not a cron"))


def test_list_scopes_to_owner_for_users_and_to_all_for_admin() -> None:
    """A user lists only their own schedules; an administrator lists everyone's."""
    service, alice, bob, admin = _service_with_users()
    service.create(alice, _daily("alice-daily"))
    service.create(bob, _daily("bob-daily"))
    assert [schedule.owner_id for schedule in service.list(alice)] == [alice.id]
    assert sorted(schedule.owner_id for schedule in service.list(admin)) == [alice.id, bob.id]


def test_get_denies_a_users_access_to_another_users_schedule() -> None:
    """A normal user cannot read a schedule they do not own."""
    service, alice, bob, _admin = _service_with_users()
    alice_schedule = service.create(alice, _daily())
    with pytest.raises(ScheduleAccessDeniedError):
        service.get(bob, alice_schedule.id)


def test_admin_may_access_any_users_schedule() -> None:
    """An administrator bypasses ownership and reads any schedule."""
    service, alice, _bob, admin = _service_with_users()
    alice_schedule = service.create(alice, _daily())
    assert service.get(admin, alice_schedule.id) == alice_schedule


def test_get_raises_not_found_for_a_missing_schedule() -> None:
    """A schedule id that was never created is not-found, not access-denied."""
    service, alice, _bob, _admin = _service_with_users()
    with pytest.raises(ScheduleNotFoundError):
        service.get(alice, 999)


def test_update_applies_a_partial_change_and_leaves_other_fields_intact() -> None:
    """A partial update changes only the supplied field and persists it."""
    service, alice, _bob, _admin = _service_with_users()
    created = service.create(alice, _daily("before"))
    updated = service.update(alice, created.id, ScheduleUpdate(name="after"))
    assert updated.name == "after"
    assert updated.cron_expression == created.cron_expression
    assert service.get(alice, created.id).name == "after"


def test_update_denies_cross_owner_and_writes_nothing() -> None:
    """A denied update leaves the target schedule unchanged."""
    service, alice, bob, _admin = _service_with_users()
    created = service.create(alice, _daily("original"))
    with pytest.raises(ScheduleAccessDeniedError):
        service.update(bob, created.id, ScheduleUpdate(name="hijacked"))
    assert service.get(alice, created.id).name == "original"


def test_delete_removes_an_owned_schedule() -> None:
    """Deleting an owned schedule makes it subsequently not-found."""
    service, alice, _bob, _admin = _service_with_users()
    created = service.create(alice, _daily())
    service.delete(alice, created.id)
    with pytest.raises(ScheduleNotFoundError):
        service.get(alice, created.id)


def test_delete_denies_cross_owner_and_writes_nothing() -> None:
    """A denied delete leaves the target schedule present."""
    service, alice, bob, _admin = _service_with_users()
    created = service.create(alice, _daily())
    with pytest.raises(ScheduleAccessDeniedError):
        service.delete(bob, created.id)
    assert service.get(alice, created.id) == created


def test_enabled_schedule_is_due_only_at_a_selected_minute() -> None:
    """A daily 09:00 schedule is due at 09:00 UTC and not one minute later."""
    service, alice, _bob, _admin = _service_with_users()
    created = service.create(alice, _daily())
    assert service.is_due(alice, created.id, datetime(2026, 1, 1, 9, 0, tzinfo=UTC)) is True
    assert service.is_due(alice, created.id, datetime(2026, 1, 1, 9, 1, tzinfo=UTC)) is False


def test_next_fire_is_the_next_daily_occurrence() -> None:
    """The next fire after 08:00 UTC is that same day at 09:00 UTC."""
    service, alice, _bob, _admin = _service_with_users()
    created = service.create(alice, _daily())
    next_fire = service.next_fire_at(alice, created.id, datetime(2026, 1, 1, 8, 0, tzinfo=UTC))
    assert next_fire == datetime(2026, 1, 1, 9, 0, tzinfo=UTC)


def test_disabled_schedule_is_never_due_and_has_no_next_fire() -> None:
    """A disabled schedule reports not-due and a next fire of None."""
    service, alice, _bob, _admin = _service_with_users()
    created = service.create(alice, _daily(enabled=False))
    assert service.is_due(alice, created.id, datetime(2026, 1, 1, 9, 0, tzinfo=UTC)) is False
    assert service.next_fire_at(alice, created.id, datetime(2026, 1, 1, 8, 0, tzinfo=UTC)) is None
