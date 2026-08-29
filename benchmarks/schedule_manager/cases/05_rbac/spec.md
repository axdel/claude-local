Implement `app/security.py`: the thin security layer that resolves a signed token to the current user and enforces the administrator role. It sits between the authentication service (which verifies tokens) and the request dependency layer (which calls into it — see the `app.dependencies` neighbor for the exact call sites).

Define exactly these public names — the app's other modules import them by name:

- `AdminRequiredError` — an exception raised when a non-administrator attempts an administrator-only operation. The app maps it to HTTP 403, and its authorization failures are modeled on the `PermissionError` family.

- `current_user(token, auth_service) -> UserRecord` — resolve a bearer token string to the current `UserRecord` by calling the authentication service's `verify_token(token)` (see the `app.services.auth_service` neighbor), which validates the signature and loads the current user, raising the app's invalid-credentials error for a bad token. This function adds no logic of its own beyond that delegation.

- `require_admin(user) -> UserRecord` — return the given `UserRecord` unchanged when it holds the administrator role (see the `Role` enum in `app.schemas`); otherwise raise `AdminRequiredError`.

Depend only on `app.schemas`, `app.repositories.user_repository` (for the `UserRecord` type), and `app.services.auth_service`. Contain no HTTP concepts and no database access — identity comes from the auth service, and the dependency and exception-handler layers above translate these outcomes to status codes.
