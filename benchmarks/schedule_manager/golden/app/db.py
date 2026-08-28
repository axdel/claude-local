"""SQLite connection and schema lifecycle for the schedule-manager golden app."""

from __future__ import annotations

import sqlite3
from pathlib import Path

type DatabasePath = str | Path

DEFAULT_DATABASE_PATH: DatabasePath = ":memory:"
"""Default to one in-memory database owned by the application connection."""

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    cron_expression TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1))
);
"""


def connect_database(database_path: DatabasePath) -> sqlite3.Connection:
    """Open a cross-thread app connection with relational checks enabled."""
    connection = sqlite3.connect(database_path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database(connection: sqlite3.Connection) -> None:
    """Create the schedule-manager schema without replacing existing rows."""
    connection.executescript(_SCHEMA_SQL)
