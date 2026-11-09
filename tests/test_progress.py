from contextlib import closing

import pytest

from schedule_guard.guard import Outcome, Progress, TaskState
from schedule_guard.progress import ProgressError, apply_progress, load_task
from schedule_guard.store.db import open_db

LEAF = 3  # "Mobilize and site fencing"


def applied(made_against_version=0, **after) -> Outcome:
    before = Progress(version=made_against_version, percent_complete=0, remaining_work="PT96H0M0S")
    return Outcome(
        "applied", task_uid=LEAF, made_against_version=made_against_version, before=before,
        after=Progress(version=made_against_version + 1, **{"remaining_work": "PT96H0M0S", **after}),
    )


def test_load_task_reads_the_summary_flag_and_imported_progress(fixture_db):
    with closing(open_db(fixture_db)) as conn:
        assert load_task(conn, LEAF) == TaskState(
            LEAF, False, Progress(version=0, percent_complete=0, remaining_work="PT96H0M0S")
        )
        assert load_task(conn, 1).is_summary is True


@pytest.mark.parametrize("uid", [40, -1, 2**63, 10**30])
def test_load_task_finds_no_task_for_an_unknown_or_impossible_uid(fixture_db, uid):
    with closing(open_db(fixture_db)) as conn:
        assert load_task(conn, uid) is None


def test_apply_progress_writes_that_tasks_row_only(fixture_db):
    with closing(open_db(fixture_db)) as conn:
        others = conn.execute("SELECT * FROM task_progress WHERE task_uid != ? ORDER BY task_uid", (LEAF,)).fetchall()
        apply_progress(conn, applied(percent_complete=40, actual_start="2026-06-08T08:00:00", notes="x"))
        assert load_task(conn, LEAF).progress == Progress(
            version=1, percent_complete=40, actual_start="2026-06-08T08:00:00", remaining_work="PT96H0M0S", notes="x"
        )
        assert conn.execute("SELECT * FROM task_progress WHERE task_uid != ? ORDER BY task_uid", (LEAF,)).fetchall() == others


def test_apply_progress_refuses_when_the_task_has_moved_on(fixture_db):
    with closing(open_db(fixture_db)) as conn:
        apply_progress(conn, applied(percent_complete=40))
        with pytest.raises(ProgressError, match="task UID 3 is no longer at version 0"):
            apply_progress(conn, applied(percent_complete=90))  # made against 0 again
        assert load_task(conn, LEAF).progress.percent_complete == 40
