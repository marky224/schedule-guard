"""P1, P2, P4 and P5 (PLAN.md section 8) on the real sync engine.

Each example writes batches of updates to an update file on a fresh copy of
the imported fixture and runs `sync` once per batch: valid, stale,
duplicate (a key seen before), unknown-task, summary-task, forbidden-field,
bad-value, malformed and unparseable lines, mixed.
"""

from collections import Counter
from contextlib import closing

from harness import (
    BATCHES, ENGINE, assert_all_or_nothing, assert_stale_never_applied, assert_unchanged, baselines, master_schedule,
    progress_rows, run_batches,
)
from hypothesis import event, given

from schedule_guard.store.db import open_db


def run_on_a_fresh_copy(fresh_db, batches, read=lambda conn: None):
    db = fresh_db()
    with closing(open_db(db)) as conn:
        before, imported = read(conn), progress_rows(conn)
    results = run_batches(db, batches)
    assert [r.status for r in results] == ["ok"] * len(results), [r.error for r in results]
    with closing(open_db(db)) as conn:
        return before, read(conn), imported, db


@ENGINE
@given(BATCHES)
def test_p1_the_baselines_stay_byte_identical(fresh_db, batches):
    before, after, _, _ = run_on_a_fresh_copy(fresh_db, batches, baselines)
    assert_unchanged(before, after, "baselines")


@ENGINE
@given(BATCHES)
def test_p2_the_master_schedule_stays_byte_identical(fresh_db, batches):
    before, after, _, _ = run_on_a_fresh_copy(fresh_db, batches, master_schedule)
    assert_unchanged(before, after, "master schedule")


@ENGINE
@given(BATCHES)
def test_p4_each_update_has_one_outcome_and_changes_all_or_nothing(fresh_db, batches):
    _, _, imported, db = run_on_a_fresh_copy(fresh_db, batches)
    with closing(open_db(db)) as conn:
        for status, reason in conn.execute("SELECT status, reason_code FROM outcome"):
            event(f"outcome: {status} {reason or ''}".rstrip())
        assert_all_or_nothing(conn, imported)


@ENGINE
@given(BATCHES)
def test_p5_a_stale_update_never_applies(fresh_db, batches):
    _, _, _, db = run_on_a_fresh_copy(fresh_db, batches)
    with closing(open_db(db)) as conn:
        assert_stale_never_applied(conn)


def test_the_strategies_apply_a_fair_share_of_updates(fresh_db):
    # P1, P2, P4 and P5 can only catch a bad write if updates get applied.
    statuses = Counter()

    @ENGINE
    @given(BATCHES)
    def collect(batches):
        _, _, _, db = run_on_a_fresh_copy(fresh_db, batches)
        with closing(open_db(db)) as conn:
            statuses.update(status for (status,) in conn.execute("SELECT status FROM outcome"))

    collect()
    assert statuses["applied"] >= 0.15 * sum(statuses.values()), statuses
