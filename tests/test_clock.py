from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from schedule_guard.clock import fixed_clock, today


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        # Late evening in Chicago (CDT, UTC-5) is already the next day in UTC.
        pytest.param(datetime(2026, 10, 31, 3, 30, tzinfo=UTC), date(2026, 10, 30), id="utc-next-day-cdt"),
        pytest.param(datetime(2026, 10, 31, 4, 59, 59, tzinfo=UTC), date(2026, 10, 30), id="last-second-cdt"),
        pytest.param(datetime(2026, 10, 31, 5, 0, tzinfo=UTC), date(2026, 10, 31), id="midnight-cdt"),
        # After daylight saving time ends on 2026-11-01, Chicago is UTC-6 (CST).
        pytest.param(datetime(2026, 12, 1, 5, 30, tzinfo=UTC), date(2026, 11, 30), id="utc-next-day-cst"),
        pytest.param(datetime(2026, 12, 1, 6, 0, tzinfo=UTC), date(2026, 12, 1), id="midnight-cst"),
        # The instant's own offset doesn't matter, only where it falls in Chicago.
        pytest.param(
            datetime(2026, 10, 31, 9, 0, tzinfo=timezone(timedelta(hours=10))), date(2026, 10, 30), id="other-offset"
        ),
    ],
)
def test_today_is_the_america_chicago_date(instant, expected):
    assert today(fixed_clock(instant)) == expected


def test_fixed_clock_reads_its_instant():
    instant = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
    assert fixed_clock(instant)() == instant


def test_a_naive_datetime_is_refused():
    naive = datetime(2026, 10, 30, 12, 0)
    with pytest.raises(ValueError, match="aware datetime"):
        fixed_clock(naive)
    with pytest.raises(ValueError, match="aware datetime"):
        today(lambda: naive)
