"""Typed persistence boundary for schedule-manager users."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import cast

from ..schemas import Role

_INSERT_USER = "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?) RETURNING *"
_SELECT_USER_BY_ID = "SELECT * FROM users WHERE id = ?"
_SELECT_USER_BY_USERNAME = "SELECT * FROM users WHERE username = ?"


@dataclass(frozen=True, slots=True)
class UserRecord:
    """One persisted user, including authentication-only password material."""

    id: int
    username: str
    password_hash: str
    role: Role


class UserRepository:
    """Own all user SQL over one caller-owned SQLite connection."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def create(
        self,
        username: str,
        password_hash: str,
        role: Role = Role.USER,
    ) -> UserRecord:
        """Persist and return a user; reject duplicate usernames or invalid roles atomically."""
        validated_role = Role(role)
        with self._connection:
            row = self._connection.execute(
                _INSERT_USER,
                (username, password_hash, validated_role),
            ).fetchone()
        if row is None:
            raise RuntimeError("user insert returned no row")
        return _user_from_row(row)

    def get_by_id(self, user_id: int) -> UserRecord | None:
        """Return the user with this identifier, or None when absent."""
        row = self._connection.execute(
            _SELECT_USER_BY_ID,
            (user_id,),
        ).fetchone()
        return None if row is None else _user_from_row(row)

    def get_by_username(self, username: str) -> UserRecord | None:
        """Return the user with this username, or None when absent."""
        row = self._connection.execute(
            _SELECT_USER_BY_USERNAME,
            (username,),
        ).fetchone()
        return None if row is None else _user_from_row(row)

    def delete(self, user_id: int) -> bool:
        """Delete one user and their schedules; report whether the user existed."""
        with self._connection:
            cursor = self._connection.execute(
                "DELETE FROM users WHERE id = ?",
                (user_id,),
            )
        return cursor.rowcount == 1


def _user_from_row(row: sqlite3.Row) -> UserRecord:
    """Map the repository's canonical SQL projection to one typed user record."""
    return UserRecord(
        id=cast(int, row["id"]),
        username=cast(str, row["username"]),
        password_hash=cast(str, row["password_hash"]),
        role=Role(cast(str, row["role"])),
    )
