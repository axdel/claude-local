Implement `app/schemas.py`: the strict Pydantic v2 request and response models shared across the schedule-manager app's routers, services, and repositories.

Every model must use Pydantic v2 in **strict mode** and must **forbid unknown fields**, in both directions (validating incoming request payloads and serializing outgoing responses). Strict mode means no lax type coercion — for example, a JSON string is NOT accepted for a `bool` or `int` field.

Field constraints (apply the same rule everywhere the concept appears):
- identifiers (`id`, `owner_id`): `int` strictly greater than 0.
- `username`: `str`, length 1–64.
- `password`: `str`, length 1–128, UTF-8 encodable.
- schedule `name`: `str`, length 1–100.
- `cron_expression`: `str`, length 1–100.

Define exactly these public names — the app's other modules import them by name:

- `Role` — a string enum with members `USER = "user"` and `ADMIN = "admin"`.
- `UserCredentials` — `username` and `password`; the body accepted at login.
- `UserCreate` — the registration body; the same fields as `UserCredentials`.
- `UserRead` — the public user response: `id`, `username`, and `role` (a `Role`). It must NOT expose any password field.
- `ScheduleCreate` — `name`, `cron_expression`, and `enabled: bool` defaulting to `True`.
- `ScheduleRead` — the persisted schedule response: `id`, `owner_id`, `name`, `cron_expression`, `enabled`.
- `ScheduleUpdate` — a partial update: `name`, `cron_expression`, and `enabled` are each optional (absent by default), but a validated instance must carry **at least one non-null field** — an empty update, or one whose supplied fields are all null, is rejected.

These are pure boundary data contracts: no I/O, no database access, no business logic. Depend only on Pydantic and the standard library; do not import from other `app.*` modules.
