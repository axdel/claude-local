"""Typed persistence boundary for schedule-manager schedules."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import cast

from ..schemas import ScheduleUpdate

_INSERT_SCHEDULE = (
    "INSERT INTO schedules (owner_id, name, cron_expression, enabled) "
    "VALUES (?, ?, ?, ?) RETURNING *"
)
_SELECT_SCHEDULE_BY_ID = "SELECT * FROM schedules WHERE id = ?"
_SELECT_SCHEDULES_FOR_OWNER = "SELECT * FROM schedules WHERE owner_id = ? ORDER BY id"
_SELECT_ALL_SCHEDULES = "SELECT * FROM schedules ORDER BY id"
_UPDATE_SCHEDULE = (
    "UPDATE schedules SET "
    "name = CASE WHEN ? THEN ? ELSE name END, "
    "cron_expression = CASE WHEN ? THEN ? ELSE cron_expression END, "
    "enabled = CASE WHEN ? THEN ? ELSE enabled END "
    "WHERE id = ? RETURNING *"
)


@dataclass(frozen=True, slots=True)
class ScheduleRecord:
    """One persisted schedule and its owning user identifier."""

    id: int
    owner_id: int
    name: str
    cron_expression: str
    enabled: bool


class ScheduleRepository:
    """Own all schedule SQL over one caller-owned SQLite connection."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def create(
        self,
        owner_id: int,
        name: str,
        cron_expression: str,
        *,
        enabled: bool = True,
    ) -> ScheduleRecord:
        """Persist and return a schedule owned by an existing user."""
        with self._connection:
            row = self._connection.execute(
                _INSERT_SCHEDULE,
                (owner_id, name, cron_expression, enabled),
            ).fetchone()
        if row is None:
            raise RuntimeError("schedule insert returned no row")
        return _schedule_from_row(row)

    def get_by_id(self, schedule_id: int) -> ScheduleRecord | None:
        """Return the schedule with this identifier, or None when absent."""
        row = self._connection.execute(
            _SELECT_SCHEDULE_BY_ID,
            (schedule_id,),
        ).fetchone()
        return None if row is None else _schedule_from_row(row)

    def list_for_owner(self, owner_id: int) -> list[ScheduleRecord]:
        """Return this owner's schedules in insertion order."""
        rows = self._connection.execute(
            _SELECT_SCHEDULES_FOR_OWNER,
            (owner_id,),
        ).fetchall()
        return [_schedule_from_row(row) for row in rows]

    def list_all(self) -> list[ScheduleRecord]:
        """Return every schedule in insertion order."""
        rows = self._connection.execute(_SELECT_ALL_SCHEDULES).fetchall()
        return [_schedule_from_row(row) for row in rows]

    def update(
        self,
        schedule_id: int,
        changes: ScheduleUpdate,
    ) -> ScheduleRecord | None:
        """Atomically update only supplied fields, or return None when absent."""
        supplied_fields = changes.model_fields_set
        with self._connection:
            row = self._connection.execute(
                _UPDATE_SCHEDULE,
                (
                    "name" in supplied_fields,
                    changes.name,
                    "cron_expression" in supplied_fields,
                    changes.cron_expression,
                    "enabled" in supplied_fields,
                    changes.enabled,
                    schedule_id,
                ),
            ).fetchone()
        return None if row is None else _schedule_from_row(row)

    def delete(self, schedule_id: int) -> bool:
        """Delete one schedule and report whether a row existed."""
        with self._connection:
            cursor = self._connection.execute(
                "DELETE FROM schedules WHERE id = ?",
                (schedule_id,),
            )
        return cursor.rowcount == 1


def _schedule_from_row(row: sqlite3.Row) -> ScheduleRecord:
    """Map the repository's canonical SQL projection to one typed schedule record."""
    return ScheduleRecord(
        id=cast(int, row["id"]),
        owner_id=cast(int, row["owner_id"]),
        name=cast(str, row["name"]),
        cron_expression=cast(str, row["cron_expression"]),
        enabled=bool(cast(int, row["enabled"])),
    )
