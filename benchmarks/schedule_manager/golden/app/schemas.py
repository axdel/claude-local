"""Strict request and response contracts for the schedule-manager API."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_Identifier = Annotated[int, Field(gt=0)]
_Username = Annotated[str, Field(min_length=1, max_length=64)]
_Password = Annotated[str, Field(min_length=1, max_length=128)]
_ScheduleName = Annotated[str, Field(min_length=1, max_length=100)]
_CronExpression = Annotated[str, Field(min_length=1, max_length=100)]


class Role(StrEnum):
    """The two authorization roles recognized across the golden app."""

    USER = "user"
    ADMIN = "admin"


class _StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


def _validate_utf8(text: str) -> str:
    try:
        text.encode()
    except UnicodeEncodeError as error:
        raise ValueError("credential text must be UTF-8 encodable") from error
    return text


def validate_password(password: str) -> str:
    """Return a UTF-8 password distinct under HMAC key padding."""
    _validate_utf8(password)
    if "\N{NULL}" in password:
        raise ValueError("password must not contain a null character")
    return password


class UserCredentials(_StrictModel):
    """Bounded credentials accepted by registration and authentication."""

    username: _Username
    password: _Password

    _validate_username = field_validator("username")(_validate_utf8)
    _validate_password = field_validator("password")(validate_password)


class UserCreate(UserCredentials):
    """Credentials accepted when registering a user."""


class UserRead(_StrictModel):
    """Public persisted user fields returned by the API."""

    id: _Identifier
    username: _Username
    role: Role


class ScheduleCreate(_StrictModel):
    """Fields accepted when creating a schedule."""

    name: _ScheduleName
    cron_expression: _CronExpression
    enabled: bool = True


class ScheduleRead(_StrictModel):
    """Persisted schedule fields returned by the API."""

    id: _Identifier
    owner_id: _Identifier
    name: _ScheduleName
    cron_expression: _CronExpression
    enabled: bool


class ScheduleUpdate(_StrictModel):
    """At least one concrete field accepted when updating a schedule."""

    name: _ScheduleName | None = None
    cron_expression: _CronExpression | None = None
    enabled: bool | None = None

    @model_validator(mode="after")
    def _require_concrete_change(self) -> Self:
        changes = self.model_dump(exclude_unset=True)
        if not changes or any(value is None for value in changes.values()):
            raise ValueError("schedule update requires at least one non-null field")
        return self
