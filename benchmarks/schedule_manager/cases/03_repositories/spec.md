Implement `app/repositories/schedule_repository.py`: the typed persistence boundary that owns ALL schedule SQL over one caller-supplied SQLite connection. No schedule SQL may live anywhere else in the app.

The `schedules` table (see the `app.db` neighbor) has columns: `id` (autoincrement primary key), `owner_id`, `name`, `cron_expression`, and `enabled` (stored as integer 0 or 1).

Define exactly these public names:

- `ScheduleRecord` — an immutable value object with fields `id: int`, `owner_id: int`, `name: str`, `cron_expression: str`, `enabled: bool`. Map the stored 0/1 `enabled` column to a Python `bool`.

- `ScheduleRepository` — constructed with one `sqlite3.Connection` whose lifecycle the caller owns. Methods:
  - `create(owner_id, name, cron_expression, *, enabled=True) -> ScheduleRecord` — insert one schedule for an existing owner and return the persisted record, including its assigned `id`.
  - `get_by_id(schedule_id) -> ScheduleRecord | None` — the schedule with that id, or `None` when absent.
  - `list_for_owner(owner_id) -> list[ScheduleRecord]` — that owner's schedules in insertion (id) order.
  - `list_all() -> list[ScheduleRecord]` — every schedule in insertion (id) order.
  - `update(schedule_id, changes: ScheduleUpdate) -> ScheduleRecord | None` — apply ONLY the fields the caller explicitly supplied on the `ScheduleUpdate` (see the `app.schemas` neighbor), leaving unsupplied fields unchanged, and return the updated record (or `None` when the schedule is absent). Use `changes.model_fields_set` to distinguish a supplied field from a defaulted one.
  - `delete(schedule_id) -> bool` — delete one schedule and report whether a row existed.

Commit every mutating write. Return typed `ScheduleRecord`s, never raw rows. Contain no business or authorization logic — that belongs to the service layer above this one.
