"""Role-aware schedule orchestration for the golden app."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..cron import parse_cron
from ..repositories.schedule_repository import ScheduleRecord, ScheduleRepository
from ..repositories.user_repository import UserRecord
from ..schemas import Role, ScheduleCreate, ScheduleUpdate


class ScheduleNotFoundError(LookupError):
    """Raised when a requested schedule does not exist."""


class ScheduleAccessDeniedError(PermissionError):
    """Raised when a normal user requests another user's schedule."""


@dataclass(frozen=True, slots=True)
class ScheduleService:
    """Own schedule CRUD, authorization policy, and derived cron behavior."""

    schedules: ScheduleRepository

    def create(self, user: UserRecord, schedule: ScheduleCreate) -> ScheduleRecord:
        """Validate and persist a schedule owned by the current user."""
        parse_cron(schedule.cron_expression)
        return self.schedules.create(
            user.id,
            schedule.name,
            schedule.cron_expression,
            enabled=schedule.enabled,
        )

    def list(self, user: UserRecord) -> list[ScheduleRecord]:
        """List the current user's schedules, or every schedule for an administrator."""
        if user.role is Role.ADMIN:
            return self.schedules.list_all()
        return self.schedules.list_for_owner(user.id)

    def get(self, user: UserRecord, schedule_id: int) -> ScheduleRecord:
        """Return an accessible schedule or raise a precise domain error."""
        return self._accessible_schedule(user, schedule_id)

    def update(
        self,
        user: UserRecord,
        schedule_id: int,
        changes: ScheduleUpdate,
    ) -> ScheduleRecord:
        """Authorize, validate, and atomically update supplied schedule fields."""
        self._accessible_schedule(user, schedule_id)
        if changes.cron_expression is not None:
            parse_cron(changes.cron_expression)
        updated_schedule = self.schedules.update(schedule_id, changes)
        if updated_schedule is None:
            raise ScheduleNotFoundError(f"schedule not found: {schedule_id}")
        return updated_schedule

    def delete(self, user: UserRecord, schedule_id: int) -> None:
        """Authorize and delete one schedule."""
        self._accessible_schedule(user, schedule_id)
        if not self.schedules.delete(schedule_id):
            raise ScheduleNotFoundError(f"schedule not found: {schedule_id}")

    def _accessible_schedule(
        self,
        user: UserRecord,
        schedule_id: int,
    ) -> ScheduleRecord:
        schedule = self.schedules.get_by_id(schedule_id)
        if schedule is None:
            raise ScheduleNotFoundError(f"schedule not found: {schedule_id}")
        if user.role is not Role.ADMIN and schedule.owner_id != user.id:
            raise ScheduleAccessDeniedError(f"schedule access denied: {schedule_id}")
        return schedule

    def is_due(self, user: UserRecord, schedule_id: int, at: datetime) -> bool:
        """Return whether an accessible enabled schedule fires at the given instant."""
        schedule = self._accessible_schedule(user, schedule_id)
        return schedule.enabled and parse_cron(schedule.cron_expression).selects(at)

    def next_fire_at(
        self,
        user: UserRecord,
        schedule_id: int,
        after: datetime,
    ) -> datetime | None:
        """Return an enabled schedule's next fire time, or None when disabled."""
        schedule = self._accessible_schedule(user, schedule_id)
        if not schedule.enabled:
            return None
        return parse_cron(schedule.cron_expression).next_after(after)
