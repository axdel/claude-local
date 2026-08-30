"""Deterministic five-field UTC cron parsing and evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

_MAX_SEARCH_DAYS = 8 * 366


class InvalidCronExpressionError(ValueError):
    """Raised when a cron expression falls outside the supported grammar."""


class CronSearchExhaustedError(ValueError):
    """Raised when a valid expression has no occurrence in a Gregorian cycle."""


@dataclass(frozen=True, slots=True)
class _FieldDefinition:
    name: str
    minimum: int
    maximum: int
    aliases_sunday: bool = False

    def normalize(self, value: int) -> int:
        return 0 if self.aliases_sunday and value == 7 else value


@dataclass(frozen=True, slots=True)
class _CronField:
    values: frozenset[int]
    is_starred: bool

    def selects(self, value: int) -> bool:
        return value in self.values


_FIELD_DEFINITIONS = (
    _FieldDefinition("minute", 0, 59),
    _FieldDefinition("hour", 0, 23),
    _FieldDefinition("day of month", 1, 31),
    _FieldDefinition("month", 1, 12),
    _FieldDefinition("day of week", 0, 7, aliases_sunday=True),
)


@dataclass(frozen=True, slots=True)
class CronSchedule:
    """One parsed UTC cron schedule with wildcard provenance for calendar matching."""

    minute: _CronField
    hour: _CronField
    day_of_month: _CronField
    month: _CronField
    day_of_week: _CronField

    def selects(self, at: datetime) -> bool:
        """Return whether this schedule selects the UTC minute containing ``at``."""
        selected = _as_utc(at)
        return (
            self.minute.selects(selected.minute)
            and self.hour.selects(selected.hour)
            and self.month.selects(selected.month)
            and self._selects_day(selected)
        )

    def next_after(self, after: datetime) -> datetime:
        """Return the first selected UTC minute strictly after the reference minute."""
        reference = _as_utc(after)
        threshold = reference.replace(second=0, microsecond=0) + timedelta(minutes=1)
        day_start = threshold.replace(hour=0, minute=0)
        for day_offset in range(_MAX_SEARCH_DAYS + 1):
            candidate_day = day_start + timedelta(days=day_offset)
            if not self.month.selects(candidate_day.month) or not self._selects_day(candidate_day):
                continue
            for hour in sorted(self.hour.values):
                for minute in sorted(self.minute.values):
                    candidate = candidate_day.replace(hour=hour, minute=minute)
                    if candidate >= threshold:
                        return candidate
        raise CronSearchExhaustedError("cron expression has no occurrence in a Gregorian cycle")

    def _selects_day(self, at: datetime) -> bool:
        selected_weekday = (at.weekday() + 1) % 7
        if self.day_of_month.is_starred:
            return self.day_of_week.selects(selected_weekday)
        if self.day_of_week.is_starred:
            return self.day_of_month.selects(at.day)
        return self.day_of_month.selects(at.day) or self.day_of_week.selects(selected_weekday)


def parse_cron(expression: str) -> CronSchedule:
    """Parse one five-field cron expression into its immutable UTC schedule."""
    fields = expression.split()
    if len(fields) != len(_FIELD_DEFINITIONS):
        raise InvalidCronExpressionError("cron expression must contain five fields")
    minute, hour, day_of_month, month, day_of_week = (
        _parse_field(field, definition)
        for field, definition in zip(fields, _FIELD_DEFINITIONS, strict=True)
    )
    return CronSchedule(minute, hour, day_of_month, month, day_of_week)


def is_due(expression: str, at: datetime) -> bool:
    """Return whether the cron expression selects the UTC minute containing ``at``."""
    return parse_cron(expression).selects(at)


def next_fire_at(expression: str, after: datetime) -> datetime:
    """Return the first UTC occurrence strictly after the reference minute."""
    return parse_cron(expression).next_after(after)


def _parse_field(field: str, definition: _FieldDefinition) -> _CronField:
    if not field or any(not component for component in field.split(",")):
        raise InvalidCronExpressionError(f"invalid {definition.name} field: {field}")
    values: set[int] = set()
    for component in field.split(","):
        values.update(_expand_component(component, definition))
    return _CronField(
        values=frozenset(definition.normalize(value) for value in values),
        is_starred="*" in field,
    )


def _expand_component(component: str, definition: _FieldDefinition) -> range:
    if component.count("/") > 1:
        raise InvalidCronExpressionError(f"invalid {definition.name} field: {component}")
    base, separator, step_text = component.partition("/")
    step = _parse_number(step_text, definition) if separator else 1
    if step < 1:
        raise InvalidCronExpressionError(f"{definition.name} step must be positive")
    if base == "*":
        start, stop = definition.minimum, definition.maximum
    elif "-" in base:
        start, stop = _parse_range(base, definition)
    elif separator:
        raise InvalidCronExpressionError(f"invalid stepped {definition.name} field: {component}")
    else:
        value = _parse_number(base, definition)
        start = stop = value
    return range(start, stop + 1, step)


def _parse_range(value_range: str, definition: _FieldDefinition) -> tuple[int, int]:
    if value_range.count("-") != 1:
        raise InvalidCronExpressionError(f"invalid {definition.name} range: {value_range}")
    start_text, stop_text = value_range.split("-")
    start = _parse_number(start_text, definition)
    stop = _parse_number(stop_text, definition)
    if start > stop:
        raise InvalidCronExpressionError(f"descending {definition.name} range: {value_range}")
    return start, stop


def _parse_number(text: str, definition: _FieldDefinition) -> int:
    if not text.isascii() or not text.isdecimal():
        raise InvalidCronExpressionError(f"invalid {definition.name} value: {text}")
    value = int(text)
    if not definition.minimum <= value <= definition.maximum:
        raise InvalidCronExpressionError(
            f"{definition.name} value outside {definition.minimum}-{definition.maximum}: {value}"
        )
    return value


def _as_utc(at: datetime) -> datetime:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("cron evaluation requires a timezone-aware datetime")
    return at.astimezone(UTC)
