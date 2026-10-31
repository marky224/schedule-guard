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
import test_invariants as invariants  # imported as a module, so its tests aren't collected twice
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


def open_db_with_triggers_off(path, **kwargs):
    conn = open_db(path, **kwargs)
    conn.setconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_TRIGGER, False)
    return conn


PROPERTIES = {
    "p2-planned-finish": (also_moves_the_planned_finish, invariants.test_p2_the_master_schedule_stays_byte_identical,
                          "master schedule: a row changed"),
    "p1-baseline-finish": (also_moves_the_baseline_finish, invariants.test_p1_the_baselines_stay_byte_identical,
                           "baselines: a row changed"),
}


@pytest.mark.parametrize(("broken", "prop", "message"), PROPERTIES.values(), ids=PROPERTIES.keys())
def test_the_property_test_itself_fails_on_a_broken_applier(fresh_db, monkeypatch, broken, prop, message):
    # The P1 and P2 tests as they run in the suite, strategies and all; the
    # triggers are off so the broken write reaches the database.
    monkeypatch.setattr(progress, "apply_progress", broken)
    monkeypatch.setattr(invariants, "open_db", open_db_with_triggers_off)
    with pytest.raises(AssertionError, match=message):
        prop(fresh_db=fresh_db)
