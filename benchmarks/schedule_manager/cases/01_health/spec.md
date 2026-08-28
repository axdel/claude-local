Implement `app/main.py` as a minimal FastAPI application module.

Requirements:
- Define a Pydantic v2 `HealthResponse` model with a strict, extra-forbidding `ConfigDict` and a `status: str` field.
- Define `create_app(database_url: str = DEFAULT_DATABASE_URL) -> FastAPI`, importing `DEFAULT_DATABASE_URL` from the provided `app.db` neighbor.
- The factory must return a FastAPI app whose `GET /health` operation declares `HealthResponse` as its response model and returns HTTP 200 with exactly `{"status": "ok"}`.
- Keep all state local to the returned app. Do not start a server or make network calls.
