"""The device's own spray schedules (message types 103, 108 and 109).

Worked out from the official Android app (v2.6), whose transcoder library
builds and reads these messages:

* The device holds a list of up to 10 schedules, all active at once.
* Each is 5 bytes: a day bitmask (Mon = 64, Tue = 32, ... Sun = 1), the
  start hour, the start minute, the number of sprays in the window, and
  the interval in minutes. There is no end time: it is start + sprays x
  interval, so a window always ends on the same day it starts.
* The app replaces the whole list: "remove all" (108), a pause, then every
  schedule in one "set schedules" message (103), written to 6e400004.
  "Retrieve" (109) returns the same 5-byte entries.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time

from .const import SPRAY_INTERVALS

# The app's day order and bit for each day.
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_BITS = {day: 1 << (6 - i) for i, day in enumerate(DAYS)}
_DAY_LABELS = {
    "mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu",
    "fri": "Fri", "sat": "Sat", "sun": "Sun",
}

MAX_SCHEDULES = 10
_MINUTES_PER_DAY = 24 * 60


class ScheduleError(ValueError):
    """A schedule the device can't hold, with a message for the user."""


@dataclass(frozen=True)
class Schedule:
    days: tuple[str, ...]  # in DAYS order
    start: time
    interval: int  # minutes
    sprays: int  # sprays in the window

    @classmethod
    def create(cls, days, start: time, end: time, interval: int) -> "Schedule":
        """Build a schedule from a start and end time, checking it fits.

        The end is rounded down to the last whole interval, as the device
        only stores the number of sprays.
        """
        day_list = _normalise_days(days)
        if interval not in SPRAY_INTERVALS:
            raise ScheduleError(
                f"Interval must be one of {', '.join(map(str, SPRAY_INTERVALS))} minutes"
            )
        start_min = start.hour * 60 + start.minute
        end_min = end.hour * 60 + end.minute
        if end_min <= start_min:
            raise ScheduleError(
                "The end time must be later than the start time on the same day. "
                "For a window across midnight, add two schedules: one until "
                "23:59 and one from 00:00 on the following days"
            )
        sprays = (end_min - start_min) // interval
        if sprays < 1:
            raise ScheduleError(
                f"The window {start:%H:%M}-{end:%H:%M} is shorter than one "
                f"{interval}-minute interval"
            )
        return cls(day_list, time(start.hour, start.minute), interval, min(sprays, 255))

    @classmethod
    def from_bytes(cls, data: bytes) -> "Schedule":
        if len(data) != 5:
            raise ScheduleError(f"Schedule entry has {len(data)} bytes, expected 5")
        mask, hour, minute, sprays, interval = data
        days = tuple(day for day in DAYS if mask & _DAY_BITS[day])
        return cls(days, time(hour % 24, minute % 60), interval, sprays)

    def to_bytes(self) -> bytes:
        mask = 0
        for day in self.days:
            mask |= _DAY_BITS[day]
        return bytes([mask, self.start.hour, self.start.minute, self.sprays, self.interval])

    @property
    def start_minutes(self) -> int:
        return self.start.hour * 60 + self.start.minute

    @property
    def end_minutes(self) -> int:
        return self.start_minutes + self.sprays * self.interval

    @property
    def end(self) -> time:
        minutes = min(self.end_minutes, _MINUTES_PER_DAY - 1)
        return time(minutes // 60, minutes % 60)

    def as_dict(self) -> dict:
        """For entity attributes and logs."""
        return {
            "days": [_DAY_LABELS[d] for d in self.days],
            "start": f"{self.start:%H:%M}",
            "end": f"{self.end:%H:%M}",
            "interval": self.interval,
        }

    def __str__(self) -> str:
        return (
            f"{_days_text(self.days)} {self.start:%H:%M}-{self.end:%H:%M} "
            f"every {self.interval} min"
        )


def validate(schedules: list[Schedule]) -> None:
    """Check a whole list against the device's limits.

    Overlapping windows on the same day are refused, as the app does: what
    the device would do with them isn't known.
    """
    if len(schedules) > MAX_SCHEDULES:
        raise ScheduleError(f"The device holds at most {MAX_SCHEDULES} schedules")
    for i, a in enumerate(schedules):
        for b in schedules[i + 1:]:
            shared = set(a.days) & set(b.days)
            if (
                shared
                and a.start_minutes < b.end_minutes
                and b.start_minutes < a.end_minutes
            ):
                raise ScheduleError(f"Schedules overlap: {a} and {b}")


def _normalise_days(days) -> tuple[str, ...]:
    if isinstance(days, str):
        days = [days]
    chosen: set[str] = set()
    for day in days:
        key = str(day).strip().lower()
        if key in ("daily", "all", "every day"):
            chosen.update(DAYS)
        elif key[:3] in _DAY_BITS:
            chosen.add(key[:3])
        else:
            raise ScheduleError(f"Unknown day: {day}")
    if not chosen:
        raise ScheduleError("Pick at least one day")
    return tuple(day for day in DAYS if day in chosen)


def _days_text(days: tuple[str, ...]) -> str:
    if len(days) == 7:
        return "Daily"
    return ", ".join(_DAY_LABELS[d] for d in days)
