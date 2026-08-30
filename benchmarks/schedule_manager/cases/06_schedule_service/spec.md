Implement `app/services/schedule_service.py`: the role-aware service that owns schedule CRUD, ownership authorization, and derived cron behavior. It sits above the schedule repository (persistence) and below the HTTP layer (the routers and dependency layer call into it). It contains no HTTP concepts and no SQL — it delegates persistence to `ScheduleRepository` and cron evaluation to `app.cron.parse_cron` (see the neighbors for both).

Define exactly these public names — the app's other modules import them by name:

- `ScheduleNotFoundError` — raised when a requested schedule does not exist. The app maps it to HTTP 404; model it on the `LookupError` family.

- `ScheduleAccessDeniedError` — raised when a normal user requests a schedule they do not own. The app maps it to HTTP 403; model it on the `PermissionError` family.

- `ScheduleService` — a frozen dataclass holding exactly one field, `schedules: ScheduleRepository`, and no other state. Every method takes the acting `UserRecord` as its first argument, so authorization is explicit:

  - `create(user, schedule: ScheduleCreate) -> ScheduleRecord` — validate the cron expression by calling `parse_cron` (which raises the app's invalid-cron error, mapped to 400, on a bad expression), then persist a schedule owned by `user.id` with the supplied name, cron expression, and enabled flag.
  - `list(user) -> list[ScheduleRecord]` — an administrator (see `Role.ADMIN` in `app.schemas`) receives every schedule; a normal user receives only the schedules they own.
  - `get(user, schedule_id) -> ScheduleRecord` — return the schedule when the user may access it (see the access rule below), otherwise raise the precise domain error.
  - `update(user, schedule_id, changes: ScheduleUpdate) -> ScheduleRecord` — enforce access first, validate the cron expression only when one is supplied, apply the partial update, and return the updated record (raise not-found if the row no longer exists).
  - `delete(user, schedule_id) -> None` — enforce access first, then delete; raise not-found if no row was removed.
  - `is_due(user, schedule_id, at: datetime) -> bool` — for an accessible schedule, return whether it is enabled and its cron expression selects the given instant.
  - `next_fire_at(user, schedule_id, after: datetime) -> datetime | None` — for an accessible schedule, return its next fire time strictly after the reference instant, or `None` when the schedule is disabled.

Access rule (shared by get, update, delete, is_due, and next_fire_at): look the schedule up by id; if it does not exist, raise the not-found error; if the acting user is not an administrator and does not own it, raise the access-denied error; otherwise return it.

Depend only on `app.cron`, `app.repositories.schedule_repository`, `app.repositories.user_repository` (for the `UserRecord` type), and `app.schemas`. See those neighbors for the exact repository method names, the cron API (`parse_cron(expression).selects(at)` and `.next_after(after)`), and the schema fields.
