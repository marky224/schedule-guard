"""The injectable clock the guard reads "today" from.

A clock is any callable that returns the current instant as an aware
datetime. "Today" is that instant's date in America/Chicago, whatever the
host's time zone (Q6), so an update dated later than today is judged the
same way on a UTC CI runner. Tests pass a `fixed_clock`; no test reads the
host clock.
"""

from collections.abc import Callable
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

Clock = Callable[[], datetime]

SITE_TIMEZONE = ZoneInfo("America/Chicago")


def system_clock() -> datetime:
    return datetime.now(UTC)


def fixed_clock(instant: datetime) -> Clock:
    """A clock that always reads `instant`, which must be aware."""
    _check_aware(instant)
    return lambda: instant


def today(clock: Clock) -> date:
    """The clock's current date in America/Chicago."""
    instant = clock()
    _check_aware(instant)
    return instant.astimezone(SITE_TIMEZONE).date()


def _check_aware(instant: datetime) -> None:
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError(f"the clock must give an aware datetime, not {instant!r}")
