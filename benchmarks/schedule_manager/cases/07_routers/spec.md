Implement `app/routers/schedules.py`: the FastAPI router exposing schedule CRUD, scoped by ownership and role. It is pure delegation — it parses and validates requests, calls the role-aware schedule service, and projects the returned records onto the public response schema. It contains no business logic, no authorization decisions, and no database access; the service enforces ownership, and the app's exception handlers translate the service's domain errors to status codes (404 not-found, 403 access-denied, 400 invalid cron), so the router never catches those itself.

Define exactly this public name — `app.main` imports this module and mounts `router`:

- `router` — an `APIRouter` with prefix `/schedules` (tag `schedules`).

Expose exactly these operations. Each receives the current user and the schedule service through the app's dependency-injection aliases `CurrentUser` and `ScheduleServiceDependency` (see the `app.dependencies` neighbor), so an unauthenticated request is rejected with 401 before the handler body runs:

- `POST /schedules` -> 201, body `ScheduleRead` — accept a `ScheduleCreate` body and return `service.create(user, body)`.
- `GET /schedules` -> `list[ScheduleRead]` — return `service.list(user)`.
- `GET /schedules/{schedule_id}` -> `ScheduleRead` — return `service.get(user, schedule_id)`.
- `PATCH /schedules/{schedule_id}` -> `ScheduleRead` — accept a `ScheduleUpdate` body and return `service.update(user, schedule_id, body)`.
- `DELETE /schedules/{schedule_id}` -> 204, empty body — call `service.delete(user, schedule_id)`.

The service methods return `ScheduleRecord` values (see the `app.repositories.schedule_repository` neighbor); project each onto `ScheduleRead` — identical fields: id, owner_id, name, cron_expression, enabled (see `app.schemas`) — before returning it. Depend only on `app.dependencies`, `app.repositories.schedule_repository`, and `app.schemas`.
