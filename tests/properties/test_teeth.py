"""The oracle has teeth (PLAN.md section 8).

A deliberately broken applier also writes a planned field or a baseline
field. On a normal connection the DB trigger refuses it and the run fails.
With triggers switched off on sync's connection, as someone who can write
the file could, the write goes through, and P2 or P1 fails: the property
tests themselves catch the very bug they guard against.

A guard that replaces a task's notes with the new entry instead of
appending it makes the P4 test itself fail.

An exporter that also writes a planned element, or drops a Baseline, on
each task it writes progress on makes the P7 test itself fail.
"""

import json
import sqlite3
from contextlib import closing
from dataclasses import replace

import pytest
import test_export_properties as export_properties  # modules, so their tests aren't collected twice
import test_invariants as invariants
from harness import BATCHES, CLOCK, ENGINE, assert_unchanged, baselines, master_schedule, progress_rows
from hypothesis import Phase, given, settings

from schedule_guard import guard, progress
from schedule_guard import sync as sync_module
from schedule_guard.mspdi import export
from schedule_guard.mspdi.parse import NS
from schedule_guard.sources.file import FileSource
from schedule_guard.store.db import open_db
from schedule_guard.sync import sync

VALID = json.dumps({
    "source_item_id": "u-001", "item_version": 1, "task_uid": 3, "made_against_version": 0,
    "fields": {"percent_complete": 50}, "submitted_at": "2026-10-30T09:15:00-05:00", "submitted_by": "Crew lead A",
})
real_apply = progress.apply_progress
real_merge = guard._merge


def also_moves_the_planned_finish(conn, outcome, **kwargs):
    real_apply(conn, outcome, **kwargs)
    conn.execute("UPDATE task SET planned_finish = '2030-01-01T17:00:00' WHERE uid = ?", (outcome.task_uid,))


def also_moves_the_baseline_finish(conn, outcome, **kwargs):
    real_apply(conn, outcome, **kwargs)
    conn.execute("UPDATE baseline_task SET finish = '2030-01-01T17:00:00' WHERE task_uid = ?", (outcome.task_uid,))


def open_db_with_triggers_off(path, **kwargs):
    conn = open_db(path, **kwargs)
    conn.setconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_TRIGGER, False)
    return conn


def one_update(tmp_path) -> FileSource:
    (tmp_path / "updates.jsonl").write_text(f"{VALID}\n")
    return FileSource(tmp_path / "updates.jsonl")


BROKEN = {
    "planned-finish": (also_moves_the_planned_finish, master_schedule, "master schedule", "task: rows are read-only"),
    "baseline-finish": (also_moves_the_baseline_finish, baselines, "baselines", "baseline_task: rows are read-only"),
}


@pytest.mark.parametrize(("broken", "read", "what", "message"), BROKEN.values(), ids=BROKEN.keys())
def test_the_trigger_refuses_a_broken_applier(fixture_db, tmp_path, monkeypatch, broken, read, what, message):
    monkeypatch.setattr(progress, "apply_progress", broken)
    with closing(open_db(fixture_db)) as conn:
        before, rows = read(conn), progress_rows(conn)
    result = sync(fixture_db, one_update(tmp_path), clock=CLOCK)
    assert result.status == "failed" and message in result.error
    with closing(open_db(fixture_db)) as conn:
        assert_unchanged(before, read(conn), what)
        assert progress_rows(conn) == rows  # the whole item rolled back, progress too
        assert conn.execute("SELECT count(*) FROM outcome").fetchone() == (0,)  # and its outcome


@pytest.mark.parametrize(("broken", "read", "what", "message"), BROKEN.values(), ids=BROKEN.keys())
def test_the_property_fails_on_a_broken_applier_with_triggers_off(
    fixture_db, tmp_path, monkeypatch, broken, read, what, message
):
    monkeypatch.setattr(progress, "apply_progress", broken)
    monkeypatch.setattr(sync_module, "open_db", open_db_with_triggers_off)
    with closing(open_db(fixture_db)) as conn:
        before = read(conn)
    result = sync(fixture_db, one_update(tmp_path), clock=CLOCK)
    assert (result.status, result.applied) == ("ok", 1)
    with closing(open_db(fixture_db)) as conn, pytest.raises(AssertionError, match=f"{what}: a row changed"):
        assert_unchanged(before, read(conn), what)


PROPERTIES = {
    "p2-planned-finish": (also_moves_the_planned_finish, invariants.test_p2_the_master_schedule_stays_byte_identical,
                          "master schedule: a row changed"),
    "p1-baseline-finish": (also_moves_the_baseline_finish, invariants.test_p1_the_baselines_stay_byte_identical,
                           "baselines: a row changed"),
}


@pytest.mark.parametrize(("broken", "prop", "message"), PROPERTIES.values(), ids=PROPERTIES.keys())
def test_the_property_test_itself_fails_on_a_broken_applier(fresh_db, monkeypatch, broken, prop, message):
    # The P1 and P2 tests as they run in the suite, strategies and all; the
    # triggers are off on sync's connection so the broken write reaches the database.
    monkeypatch.setattr(progress, "apply_progress", broken)
    monkeypatch.setattr(sync_module, "open_db", open_db_with_triggers_off)
    with pytest.raises(AssertionError, match=message):
        prop(fresh_db=fresh_db)


def merge_replacing_the_notes(current, fields, note_prefix):
    merged = real_merge(current, fields, note_prefix)
    return replace(merged, notes=f"[{note_prefix}] {fields['notes']}") if "notes" in fields else merged


def test_p4_itself_fails_on_a_guard_that_replaces_the_notes(fresh_db, monkeypatch):
    # The P4 test's own body and strategies, whose notes chains give a task
    # more than one applied entry, so a replaced note can't pass for an
    # appended one. Without shrinking, which would rerun the engine for
    # every smaller example, and stopping at the first failure.
    monkeypatch.setattr(guard, "_merge", merge_replacing_the_notes)
    p4 = invariants.test_p4_each_update_has_one_outcome_and_changes_all_or_nothing.hypothesis.inner_test
    first_failure = settings(ENGINE, phases=[Phase.generate], report_multiple_bugs=False)
    with pytest.raises(AssertionError, match="P4: "):
        first_failure(given(batches=BATCHES)(p4))(fresh_db=fresh_db)


real_write_progress = export._write_progress


def also_moves_the_finish(task, parsed, values):
    written = real_write_progress(task, parsed, values)
    if written:
        task.find(f"{{{NS}}}Finish").text = "2030-01-01T17:00:00"
    return written


def also_drops_a_baseline(task, parsed, values):
    written = real_write_progress(task, parsed, values)
    if written:
        task.remove(task.find(f"{{{NS}}}Baseline"))
    return written


EXPORTERS = {
    "writes-a-planned-element": (also_moves_the_finish, "P7: a non-progress element changed"),
    "drops-a-baseline": (also_drops_a_baseline, r"P7: Baselines \[\("),
}


@pytest.mark.parametrize(("broken", "message"), EXPORTERS.values(), ids=EXPORTERS.keys())
def test_p7_itself_fails_on_a_broken_exporter(fresh_db, monkeypatch, broken, message):
    # The P7 test's own body and strategies on fresh copies of the imported
    # fixture. The broken exporter acts only on a task it writes progress on,
    # so this also shows the strategies get progress written. Without
    # shrinking, stopping at the first failure, as for P4.
    monkeypatch.setattr(export, "_write_progress", broken)
    p7 = export_properties.test_p7_the_export_keeps_the_master_schedule_and_baselines_and_re_imports
    first_failure = settings(ENGINE, phases=[Phase.generate], report_multiple_bugs=False)
    with pytest.raises(AssertionError, match=message):
        first_failure(given(batches=BATCHES, rebaselines=export_properties.REBASELINES)(p7.hypothesis.inner_test))(
            fresh_db=fresh_db
        )
