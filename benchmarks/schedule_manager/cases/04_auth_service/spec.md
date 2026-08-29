Implement `app/services/auth_service.py`: the service that owns user registration, password verification, and signed user-identity tokens for the schedule-manager app. It is constructed once per request over the shared user repository and the application's stable signing key.

Define exactly these public names — the app's other modules import them by name:

- `SIGNING_KEY_BYTES` — an `int` constant: the exact number of bytes every signing key must contain. It is **32**. The application generates its signing key with this length, so the constant must equal 32.

- `UsernameAlreadyExistsError` — an exception raised when registration requests a username that already exists (the app maps it to HTTP 409).

- `InvalidCredentialsError` — an exception raised when a username/password pair, or a token, cannot identify a user (the app maps it to HTTP 401).

- `AuthService` — constructed with two positional arguments, in this order:
  1. a user repository (see the `app.repositories.user_repository` neighbor) — its only persistence collaborator.
  2. a `signing_key: bytes` — the secret key used to sign and verify tokens.

  Construction must reject a signing key whose length is not exactly `SIGNING_KEY_BYTES` by raising `ValueError`.

  Methods:
  - `register(user) -> UserRecord` — register a new **normal** user (role `user`; see the `Role` enum in `app.schemas`) from a validated `UserCreate`, persisting the password in a non-recoverable (hashed) form — never as plaintext — and returning the persisted `UserRecord`. Raise `UsernameAlreadyExistsError` if the username is already taken.
  - `authenticate(credentials) -> UserRecord` — given a `UserCredentials`, return the matching `UserRecord` when the password is correct; raise `InvalidCredentialsError` when the user is unknown or the password is wrong.
  - `issue_token(user) -> str` — return a signed token string that identifies exactly this user. The token must be bound to the service's signing key: it must not be forgeable or tamperable without detection, and a bare user identifier with no signature must not be a valid token.
  - `verify_token(token) -> UserRecord` — verify a token's signature with the signing key, then load and return the current `UserRecord` it identifies. Raise `InvalidCredentialsError` for any token that is malformed, carries an invalid signature, or identifies no existing user.

Depend only on the standard library, the user repository, and `app.schemas`. Contain no HTTP concepts — the router and dependency layers above translate these domain errors to status codes.
