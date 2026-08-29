"""Schedule CRUD endpoints scoped by ownership and role."""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from ..dependencies import CurrentUser, ScheduleServiceDependency
from ..repositories.schedule_repository import ScheduleRecord
from ..schemas import ScheduleCreate, ScheduleRead, ScheduleUpdate

router = APIRouter(prefix="/schedules", tags=["schedules"])


def _to_read(record: ScheduleRecord) -> ScheduleRead:
    """Project a persisted schedule record onto its public response schema."""
    return ScheduleRead(
        id=record.id,
        owner_id=record.owner_id,
        name=record.name,
        cron_expression=record.cron_expression,
        enabled=record.enabled,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
def create_schedule(
    body: ScheduleCreate,
    user: CurrentUser,
    service: ScheduleServiceDependency,
) -> ScheduleRead:
    """Create a schedule owned by the current user."""
    return _to_read(service.create(user, body))


@router.get("")
def list_schedules(user: CurrentUser, service: ScheduleServiceDependency) -> list[ScheduleRead]:
    """List the current user's schedules, or every schedule for an administrator."""
    return [_to_read(record) for record in service.list(user)]


@router.get("/{schedule_id}")
def get_schedule(
    schedule_id: int,
    user: CurrentUser,
    service: ScheduleServiceDependency,
) -> ScheduleRead:
    """Return one accessible schedule."""
    return _to_read(service.get(user, schedule_id))


@router.patch("/{schedule_id}")
def update_schedule(
    schedule_id: int,
    body: ScheduleUpdate,
    user: CurrentUser,
    service: ScheduleServiceDependency,
) -> ScheduleRead:
    """Update the supplied fields of one accessible schedule."""
    return _to_read(service.update(user, schedule_id, body))


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_schedule(
    schedule_id: int,
    user: CurrentUser,
    service: ScheduleServiceDependency,
) -> Response:
    """Delete one accessible schedule."""
    service.delete(user, schedule_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
