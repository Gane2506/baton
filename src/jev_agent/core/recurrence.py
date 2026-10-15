"""Schedule text parsing for recurring tasks.

A schedule is either a shorthand (`hourly`, `daily`, `daily@09:30`, `weekly@mon-09:00`,
`monthly`) or a five-field cron expression. `parse_schedule` normalizes either form into a
`Schedule` (cron + kind + the original text); `next_due` answers "when does this next fire
after this moment, in this timezone".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from croniter import croniter

__all__ = [
    "Schedule",
    "ScheduleError",
    "describe",
    "local_timezone_name",
    "next_due",
    "parse_schedule",
]


class ScheduleError(ValueError):
    """An invalid schedule string; the message lists the valid formats."""


@dataclass(frozen=True)
class Schedule:
    """A parsed schedule: the normalized 5-field `cron` expression, its `kind`
    ("hourly" | "daily" | "weekly" | "monthly" | "cron"), and the original user input."""

    cron: str
    kind: str
    source: str


_SHORTHANDS: dict[str, tuple[str, str]] = {
    "hourly": ("0 * * * *", "hourly"),
    "daily": ("0 0 * * *", "daily"),
    "weekly": ("0 0 * * 0", "weekly"),
    "monthly": ("0 0 1 * *", "monthly"),
}

# cron day-of-week: Monday is 1, Sunday is 0 (7 is accepted as Sunday too).
_DOW_TO_CRON = {"mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 0}
_WEEKDAY_NAMES = (
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
)

_DAILY_AT = re.compile(r"daily@(\d{1,2}):(\d{1,2})")
_WEEKLY_AT = re.compile(r"weekly@(mon|tue|wed|thu|fri|sat|sun)-(\d{1,2}):(\d{1,2})")


def _invalid(text: str) -> ScheduleError:
    return ScheduleError(
        f"invalid schedule {text!r}; use a cron expression like '0 9 * * *' or a"
        " shorthand: hourly, daily, weekly, monthly, daily@HH:MM, weekly@day-HH:MM"
    )


def parse_schedule(text: str) -> Schedule:
    """Parse `text` into a `Schedule`; raises `ScheduleError` on anything else.

    Shorthands and day names match case-insensitively; a raw cron expression is
    validated with croniter and stored with normalized whitespace.
    """
    source = text
    normalized = text.strip().lower()
    shorthand = _SHORTHANDS.get(normalized)
    if shorthand is not None:
        return Schedule(cron=shorthand[0], kind=shorthand[1], source=source)
    match = _DAILY_AT.fullmatch(normalized)
    if match is not None:
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            raise _invalid(source)
        return Schedule(cron=f"{minute} {hour} * * *", kind="daily", source=source)
    match = _WEEKLY_AT.fullmatch(normalized)
    if match is not None:
        hour, minute = int(match.group(2)), int(match.group(3))
        if hour > 23 or minute > 59:
            raise _invalid(source)
        dow = _DOW_TO_CRON[match.group(1)]
        return Schedule(cron=f"{minute} {hour} * * {dow}", kind="weekly", source=source)
    fields = normalized.split()
    if len(fields) != 5:
        raise _invalid(source)
    try:
        croniter(normalized)
    except ValueError:
        raise _invalid(source) from None
    return Schedule(cron=" ".join(fields), kind="cron", source=source)


def _dow_name(field: str) -> str:
    return _WEEKDAY_NAMES[int(field) % 7]


def _ordinal(day: int) -> str:
    suffix = "th" if 11 <= day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def describe(schedule: Schedule) -> str:
    """A human sentence for the schedule, e.g. "Every day at 09:30"."""
    if schedule.kind == "hourly":
        return "Every hour"
    if schedule.kind == "cron":
        return f"Cron `{schedule.cron}`"
    fields = schedule.cron.split()
    minute, hour = int(fields[0]), int(fields[1])
    time = f"{hour:02d}:{minute:02d}"
    if schedule.kind == "daily":
        return f"Every day at {time}"
    if schedule.kind == "weekly":
        return f"Every week on {_dow_name(fields[4])} at {time}"
    if schedule.kind == "monthly":
        return f"Every month on the {_ordinal(int(fields[2]))} at {time}"
    raise ScheduleError(f"unknown schedule kind {schedule.kind!r}")


def next_due(schedule: Schedule, after: datetime, tz: ZoneInfo) -> datetime:
    """The first fire time strictly after `after`, tz-aware in `tz`.

    `after` is converted into `tz` first so the schedule is read in the wall-clock
    the user scheduled it in (handles DST on the `tz` side, via croniter).
    """
    due = croniter(schedule.cron, after.astimezone(tz)).get_next(datetime)
    assert due.tzinfo is not None  # a tz-aware start yields a tz-aware result
    return due


def local_timezone_name() -> str:
    """The local zone's IANA name (e.g. "America/Toronto"); "UTC" when unknown."""
    tzinfo = datetime.now().astimezone().tzinfo
    if isinstance(tzinfo, ZoneInfo):
        return tzinfo.key
    return "UTC"
