"""A state machine that interleaves submit, sync, rerun, dismiss and baseline lock (PLAN.md section 8).

P1, P2, P4 and P5 are checked after every step, and P3 after every sync: a
second sync of the same file right after it decides nothing new. A baseline
lock is the one step that may change a baseline row, and only its lock
columns, never the bytes.
"""

from contextlib import closing
from datetime import UTC, datetime

from harness import (
    CLOCK, assert_all_or_nothing, assert_stale_never_applied, assert_unchanged, baselines, items, master_schedule,
    progress_rows, write_batch,
)
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule, run_state_machine_as_test

from schedule_guard.baselines import lock_baseline
from schedule_guard.review import dismiss, list_conflicts
from schedule_guard.sources.file import FileSource
from schedule_guard.store.db import open_db
from schedule_guard.sync import rerun, sync

LATER = datetime(2026, 10, 30, 21, 0, tzinfo=UTC)


class SyncMachine(RuleBasedStateMachine):
    make_db = None  # the fresh_db fixture, set by the test below

    def __init__(self):
        super().__init__()
        self.db = type(self).make_db()
        self.updates = self.db.with_name("updates.jsonl")
        self.updates.touch()
        self.runs: list[str] = []
        with closing(open_db(self.db)) as conn:
            self.baselines, self.plan, self.imported = baselines(conn), master_schedule(conn), progress_rows(conn)

    def open_conflicts(self) -> list[int]:
        with closing(open_db(self.db)) as conn:
            return [c.id for c in list_conflicts(conn)]

    @rule(batch=st.lists(items(), min_size=1, max_size=3))
    def submit(self, batch):
        write_batch(self.db, self.updates, batch)

    @rule()
    def sync_now(self):
        result = sync(self.db, FileSource(self.updates), clock=CLOCK)
        assert result.status == "ok", result.error
        again = sync(self.db, FileSource(self.updates), clock=CLOCK)  # P3
        assert (again.status, again.applied, again.conflicts, again.duplicates) == ("ok", 0, 0, again.seen)
        self.runs += [result.run_id, again.run_id]

    @precondition(lambda self: self.runs)
    @rule(data=st.data())
    def rerun_a_run(self, data):
        result = rerun(self.db, data.draw(st.sampled_from(self.runs)), clock=CLOCK)
        assert (result.status, result.applied, result.conflicts) == ("ok", 0, 0)
        self.runs.append(result.run_id)

    @precondition(lambda self: self.open_conflicts())
    @rule(data=st.data())
    def dismiss_a_conflict(self, data):
        conflict_id = data.draw(st.sampled_from(self.open_conflicts()))
        with closing(open_db(self.db)) as conn:
            before = progress_rows(conn)
            dismiss(conn, conflict_id, "Resubmit against the current version", now=LATER)
            assert progress_rows(conn) == before  # dismissing never applies the update

    @rule()
    def lock_baseline_0(self):
        with closing(open_db(self.db)) as conn:
            if conn.execute("SELECT locked_at FROM baseline WHERE number = 0").fetchone() != (None,):
                return
            lock_baseline(conn, 0, now=LATER)
            data, rows = baselines(conn)
        assert data == self.baselines[0]  # the bytes never change
        changed = [(old, new) for old, new in zip(self.baselines[1], rows, strict=True) if old != new]
        assert [new[:2] + new[4:] for _, new in changed] == [old[:2] + old[4:] for old, _ in changed]
        self.baselines = (data, rows)

    @invariant()
    def p1_and_p2_the_baselines_and_the_master_schedule_stay_as_they_were(self):
        with closing(open_db(self.db)) as conn:
            assert_unchanged(self.baselines, baselines(conn), "baselines")
            assert_unchanged(self.plan, master_schedule(conn), "master schedule")

    @invariant()
    def p4_and_p5_all_or_nothing_and_stale_never_applies(self):
        with closing(open_db(self.db)) as conn:
            assert_all_or_nothing(conn, self.imported)
            assert_stale_never_applied(conn)


def test_the_state_machine_keeps_p1_to_p5_after_every_step(fresh_db):
    SyncMachine.make_db = fresh_db
    run_state_machine_as_test(SyncMachine, settings=settings(max_examples=12, stateful_step_count=10))
