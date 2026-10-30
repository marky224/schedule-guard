"""The oracle has teeth (PLAN.md section 8).

A deliberately broken applier also writes a planned field or a baseline
field. On a normal connection the DB trigger refuses it. With triggers
switched off on the connection, as someone who can write the file could,
the write goes through, and P2 or P1 fails: the properties catch the very
bug they guard against.
"""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest
from harness import assert_unchanged, baselines, master_schedule, progress_rows, run

from schedule_guard import progress
from schedule_guard.clock import fixed_clock
from schedule_guard.guard import Update
from schedule_guard.store.db import open_db

CLOCK = fixed_clock(datetime(2026, 10, 30, 17, 0, tzinfo=UTC))
VALID = (
    ("file", "row-1", 1),
    Update("file", {
        "task_uid": 3, "made_against_version": 0, "fields": {"percent_complete": 50},
        "submitted_at": "2026-10-30T09:15:00-05:00", "submitted_by": "Crew lead A",
    }),
)
real_apply = progress.apply_progress


def also_moves_the_planned_finish(conn, outcome):
    real_apply(conn, outcome)
    conn.execute("UPDATE task SET planned_finish = '2030-01-01T17:00:00' WHERE uid = ?", (outcome.task_uid,))


def also_moves_the_baseline_finish(conn, outcome):
    real_apply(conn, outcome)
    conn.execute("UPDATE baseline_task SET finish = '2030-01-01T17:00:00' WHERE task_uid = ?", (outcome.task_uid,))


BROKEN = {
    "planned-finish": (also_moves_the_planned_finish, master_schedule, "master schedule", "task: rows are read-only"),
    "baseline-finish": (also_moves_the_baseline_finish, baselines, "baselines", "baseline_task: rows are read-only"),
}


@pytest.mark.parametrize(("broken", "read", "what", "message"), BROKEN.values(), ids=BROKEN.keys())
def test_the_trigger_refuses_a_broken_applier(fixture_db, monkeypatch, broken, read, what, message):
    monkeypatch.setattr(progress, "apply_progress", broken)
    with closing(open_db(fixture_db)) as conn:
        before, rows = read(conn), progress_rows(conn)
        with pytest.raises(sqlite3.IntegrityError, match=message):
            run(conn, [VALID], CLOCK)
        assert_unchanged(before, read(conn), what)
        assert progress_rows(conn) == rows  # the whole update rolled back, progress too


@pytest.mark.parametrize(("broken", "read", "what", "message"), BROKEN.values(), ids=BROKEN.keys())
def test_the_property_fails_on_a_broken_applier_with_triggers_off(fixture_db, monkeypatch, broken, read, what, message):
    monkeypatch.setattr(progress, "apply_progress", broken)
    with closing(open_db(fixture_db)) as conn:
        conn.setconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_TRIGGER, False)
        before = read(conn)
        (step,) = run(conn, [VALID], CLOCK)
        assert step.outcome.status == "applied"
        with pytest.raises(AssertionError, match=f"{what}: a row changed"):
            assert_unchanged(before, read(conn), what)
