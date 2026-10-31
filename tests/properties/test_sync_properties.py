"""P3 and P6 (PLAN.md section 8) on the real sync engine."""

from contextlib import closing

from harness import BATCHES, CLOCK, ENGINE, items, normalized, run_batches
from hypothesis import given
from hypothesis import strategies as st

from schedule_guard import progress
from schedule_guard.sources.file import FileSource
from schedule_guard.store.db import open_db
from schedule_guard.sync import rerun, sync


@ENGINE
@given(BATCHES)
def test_p3_syncing_the_same_file_again_changes_nothing(fresh_db, batches):
    db = fresh_db()
    run_batches(db, batches)
    before = normalized(db)
    again = sync(db, FileSource(db.with_name("updates.jsonl")), clock=CLOCK)
    assert (again.status, again.applied, again.conflicts, again.duplicates) == ("ok", 0, 0, again.seen)
    assert normalized(db) == before


@ENGINE
@given(BATCHES)
def test_p3_every_line_twice_leaves_the_same_state_as_once(fresh_db, batches):
    once, twice = fresh_db(), fresh_db()
    run_batches(once, batches)
    run_batches(twice, batches, copies=2)
    assert normalized(twice) == normalized(once)


@ENGINE
@given(BATCHES)
def test_p6_a_rerun_after_a_run_gives_only_duplicates(fresh_db, batches):
    db = fresh_db()
    results = run_batches(db, batches)
    before = normalized(db)
    for result in results:
        again = rerun(db, result.run_id, clock=CLOCK)
        assert (again.status, again.applied, again.conflicts, again.duplicates) == ("ok", 0, 0, again.seen)
        assert again.rerun_of == result.run_id
    assert normalized(db) == before


class Stop(BaseException):
    """Stands in for the process dying halfway through a run."""


def stopping_after(nth: int):
    """An applier that stops the run right after its nth progress write."""
    real, writes = progress.apply_progress, []

    def apply_then_stop(conn, outcome, **kwargs):
        real(conn, outcome, **kwargs)
        writes.append(outcome)
        if len(writes) == nth:
            raise Stop

    return apply_then_stop


@ENGINE
@given(batch=st.lists(items(), min_size=1, max_size=8), nth=st.integers(1, 3))
def test_p6_a_run_stopped_halfway_and_rerun_ends_as_an_uninterrupted_run(fresh_db, batch, nth):
    uninterrupted, stopped = fresh_db(), fresh_db()
    run_batches(uninterrupted, [batch])
    real = progress.apply_progress
    progress.apply_progress = stopping_after(nth)
    try:
        run_batches(stopped, [batch])
    except Stop:
        pass
    finally:
        progress.apply_progress = real
    with closing(open_db(stopped)) as conn:
        (run_id,) = conn.execute("SELECT id FROM sync_run").fetchone()
    assert rerun(stopped, run_id, clock=CLOCK).status == "ok"
    assert normalized(stopped) == normalized(uninterrupted)
