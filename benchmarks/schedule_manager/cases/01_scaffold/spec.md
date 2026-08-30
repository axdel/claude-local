Implement `app/main.py` as a minimal FastAPI application module.

Requirements:
- Define a Pydantic v2 `HealthResponse` model with a strict, extra-forbidding `ConfigDict` and a `status: str` field.
- Define `create_app(database_path: DatabasePath = DEFAULT_DATABASE_PATH) -> FastAPI`, importing `DatabasePath` and `DEFAULT_DATABASE_PATH` from the provided `app.db` neighbor.
- The factory must return a FastAPI app whose lifespan opens one connection with `connect_database`, initializes it with `initialize_database`, exposes it as `app.state.database_connection` while running, and closes it during shutdown.
- The app's `GET /health` operation must declare `HealthResponse` as its response model and return HTTP 200 with exactly `{"status": "ok"}`.
- Keep all state local to the returned app. Do not start a server or make network calls.
