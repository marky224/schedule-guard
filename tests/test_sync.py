"""Sync: one transaction per item, exactly once, in (submitted_at, source_item_id) order."""

import fcntl
import json
import os
import shutil
import subprocess
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from schedule_guard import progress
from schedule_guard.clock import fixed_clock
from schedule_guard.importer import import_file
from schedule_guard.sources.file import FileSource
from schedule_guard.store.db import open_db
from schedule_guard.sync import SyncRefused, rerun, sync

CLOCK = fixed_clock(datetime(2026, 10, 30, 17, 0, tzinfo=UTC))
WEEK_44 = Path(__file__).resolve().parent / "fixtures" / "updates" / "week-44.jsonl"


def line(source_item_id, task_uid=3, made_against_version=0, fields=None, submitted_at="2026-10-26T16:00:00-05:00",
         item_version=1, submitted_by="Site lead (demo)") -> str:
    return json.dumps({
        "source_item_id": source_item_id, "item_version": item_version, "task_uid": task_uid,
        "made_against_version": made_against_version, "fields": fields or {"percent_complete": 25},
        "submitted_at": submitted_at, "submitted_by": submitted_by,
    })


def write(tmp_path, *lines, name="updates.jsonl"):
    path = tmp_path / name
    path.write_text("".join(f"{text}\n" for text in lines))
    return FileSource(path)


def test_a_run_decides_each_item_once_and_a_second_run_sees_duplicates(fixture_db, tmp_path):
    source = write(tmp_path, line("u-001"), line("u-002", fields={"planned_finish": "2027-01-01T17:00:00"}))
    first = sync(fixture_db, source, clock=CLOCK)
    assert (first.status, first.seen, first.applied, first.conflicts, first.duplicates) == ("ok", 2, 1, 1, 0)
    again = sync(fixture_db, source, clock=CLOCK)
    assert (again.status, again.seen, again.applied, again.conflicts, again.duplicates) == ("ok", 2, 0, 0, 2)
    repeat = rerun(fixture_db, first.run_id, clock=CLOCK)
    assert (repeat.trigger, repeat.rerun_of, repeat.seen, repeat.duplicates) == ("rerun", first.run_id, 2, 2)
    with closing(open_db(fixture_db)) as conn:
        assert conn.execute("SELECT status, reason_code FROM outcome ORDER BY update_id").fetchall() == [
            ("applied", None), ("conflict", "field_not_allowed"),
        ]


def state(db) -> dict:
    """What a run leaves behind, keyed by item rather than by row id."""
    with closing(open_db(db)) as conn:
        return {
            "progress": conn.execute("SELECT * FROM task_progress ORDER BY task_uid").fetchall(),
            "outcomes": conn.execute(
                "SELECT su.source_item_id, o.status, o.reason_code, o.detail, o.decided_at, o.before_json, o.after_json"
                " FROM outcome o JOIN source_update su ON su.id = o.update_id ORDER BY su.source_item_id"
            ).fetchall(),
            "conflicts": conn.execute(
                "SELECT su.source_item_id, c.reason_code, c.status FROM conflict c"
                " JOIN source_update su ON su.id = c.update_id ORDER BY su.source_item_id"
            ).fetchall(),
        }


def outcomes(db) -> dict:
    return {sid: (status, reason) for sid, status, reason, *_ in state(db)["outcomes"]}


def test_the_week_44_file(fixture_db):
    result = sync(fixture_db, FileSource(WEEK_44), clock=CLOCK)
    assert (result.status, result.seen, result.applied, result.conflicts, result.duplicates) == ("ok", 14, 5, 8, 1)
    decided = outcomes(fixture_db)
    unkeyed = {status_reason for sid, status_reason in decided.items() if sid.startswith("unkeyed/")}
    assert {sid: v for sid, v in decided.items() if not sid.startswith("unkeyed/")} == {
        "u-001": ("applied", None),
        "u-002": ("applied", None),
        "u-011": ("applied", None),  # 15:00 UTC is 10:00 in Chicago, after u-002 and before u-003
        "u-003": ("conflict", "stale_version"),  # made against version 0 too, but later
        "u-004": ("conflict", "field_not_allowed"),
        "u-005": ("conflict", "summary_task"),
        "u-006": ("conflict", "invalid_value"),
        "u-007": ("conflict", "unknown_task"),
        "u-008": ("conflict", "inconsistent_actuals"),
        "u-009": ("applied", None),
        "u-010": ("applied", None),
    }
    assert unkeyed == {("conflict", "malformed")} and len(decided) == 13  # the repeated u-001 line is a duplicate
    with closing(open_db(fixture_db)) as conn:
        fence = conn.execute(
            "SELECT version, percent_complete, actual_start, remaining_work, notes, last_update_id FROM task_progress"
            " WHERE task_uid = 3"
        ).fetchone()
        last = conn.execute("SELECT id FROM source_update WHERE source_item_id = 'u-009'").fetchone()[0]
        milestone = conn.execute("SELECT percent_complete, actual_finish FROM task_progress WHERE task_uid = 2").fetchone()
    assert fence == (2, 40, "2026-06-08T08:00:00", "PT58H0M0S",
                     "[2026-10-28, file, Site lead (demo)] Fence inspected, gate hung", last)
    assert milestone == (100, "2026-06-01T08:00:00")


def test_the_same_file_again_changes_nothing(fixture_db):
    sync(fixture_db, FileSource(WEEK_44), clock=CLOCK)
    before = state(fixture_db)
    again = sync(fixture_db, FileSource(WEEK_44), clock=CLOCK)
    assert (again.seen, again.applied, again.conflicts, again.duplicates) == (14, 0, 0, 14)
    assert state(fixture_db) == before


def test_of_two_updates_made_against_one_version_the_first_applies(fixture_db, tmp_path):
    # Written latest first: the order is (submitted_at, source_item_id), not the file's.
    source = write(
        tmp_path,
        line("b-late", fields={"percent_complete": 30}, submitted_at="2026-10-27T11:00:00-05:00"),
        line("b-early", fields={"percent_complete": 20}, submitted_at="2026-10-27T15:59:00Z"),  # 10:59 in Chicago
        line("a-tie", fields={"percent_complete": 10}, submitted_at="2026-10-27T11:00:00-05:00"),
    )
    sync(fixture_db, source, clock=CLOCK)
    assert outcomes(fixture_db) == {
        "b-early": ("applied", None), "a-tie": ("conflict", "stale_version"), "b-late": ("conflict", "stale_version"),
    }


def test_an_item_without_a_usable_submitted_at_is_malformed(fixture_db, tmp_path):
    source = write(tmp_path, line("u-001"), line("u-000", submitted_at="last tuesday"))
    sync(fixture_db, source, clock=CLOCK)
    assert outcomes(fixture_db) == {"u-000": ("conflict", "malformed"), "u-001": ("applied", None)}


def test_an_applied_outcome_keeps_the_note_entry_it_appended_not_the_tasks_notes(fixture_db, tmp_path):
    source = write(
        tmp_path,
        line("u-001", fields={"notes": "Fence up"}),
        line("u-002", made_against_version=1, fields={"notes": "Gate hung", "percent_complete": 30}),
        line("u-003", made_against_version=2, fields={"percent_complete": 40}),
    )
    assert sync(fixture_db, source, clock=CLOCK).applied == 3
    with closing(open_db(fixture_db)) as conn:
        stored = [(json.loads(b), json.loads(a)) for b, a in conn.execute(
            "SELECT before_json, after_json FROM outcome ORDER BY update_id"
        )]
        (notes,) = conn.execute("SELECT notes FROM task_progress WHERE task_uid = 3").fetchone()
    progress = {"percent_complete": 0, "actual_start": None, "actual_finish": None, "remaining_work": "PT96H0M0S"}
    assert stored[1] == (  # no copy of "Fence up": only the entry this update appended
        {"version": 1, **progress},
        {"version": 2, **progress, "percent_complete": 30, "notes_entry": "[2026-10-26, file, Site lead (demo)] Gate hung"},
    )
    assert stored[2][1] == {"version": 3, **progress, "percent_complete": 40, "notes_entry": None}
    assert notes == "[2026-10-26, file, Site lead (demo)] Fence up\n[2026-10-26, file, Site lead (demo)] Gate hung"


def stored_before_and_after(db, tmp_path, k: int) -> int:
    """Sync k notes updates to one task, each made against the version the one before left; the outcome text."""
    tmp_path.mkdir()
    lines = [line(f"n-{i:04}", made_against_version=i, fields={"notes": ""}) for i in range(k)]
    assert sync(db, write(tmp_path, *lines), clock=CLOCK).applied == k
    with closing(open_db(db)) as conn:
        (notes,) = conn.execute("SELECT notes FROM task_progress WHERE task_uid = 3").fetchone()
        assert notes == "\n".join(["[2026-10-26, file, Site lead (demo)] "] * k)  # the task keeps every entry
        return conn.execute("SELECT sum(length(before_json) + length(after_json)) FROM outcome").fetchone()[0]


def test_the_stored_outcomes_grow_in_proportion_to_the_notes_updates(fresh_db, tmp_path):
    # Holding the task's whole notes before and after, k updates would store about k squared entries.
    k = 20
    once = stored_before_and_after(fresh_db(), tmp_path / "k", k)
    twice = stored_before_and_after(fresh_db(), tmp_path / "2k", 2 * k)
    assert twice <= 2.1 * once, (once, twice)


def test_a_key_given_twice_in_one_run_is_decided_once_on_its_first_content(fixture_db, tmp_path):
    source = write(tmp_path, line("u-001", fields={"percent_complete": 25}), line("u-001", fields={"percent_complete": 90}))
    result = sync(fixture_db, source, clock=CLOCK)
    assert (result.seen, result.applied, result.duplicates) == (2, 1, 1)
    with closing(open_db(fixture_db)) as conn:
        assert conn.execute("SELECT percent_complete FROM task_progress WHERE task_uid = 3").fetchone() == (25,)
        assert conn.execute("SELECT count(*) FROM source_update").fetchone() == (1,)


def test_an_unusable_line_is_one_malformed_conflict_on_every_run(fixture_db, tmp_path):
    source = write(tmp_path, "this is not json", '{"no": "key"}')
    first = sync(fixture_db, source, clock=CLOCK)
    assert (first.conflicts, first.duplicates) == (2, 0)
    second = sync(fixture_db, source, clock=CLOCK)
    assert (second.conflicts, second.duplicates) == (0, 2)
    assert sorted(reason for _, reason, _ in state(fixture_db)["conflicts"]) == ["malformed", "malformed"]


LONE_SURROGATE = {
    "submitted-by": {"submitted_by": "Crew \ud800"},
    "submitted-at": {"submitted_at": "2026-10-26T16:00:00-05:00\udfff"},
    "field-value": {"fields": {"notes": "Gate \udc00 hung"}},
    "field-name": {"fields": {"notes\ud83d": "Gate hung"}},
    "key": {"\udbff": 1},
}


@pytest.mark.parametrize("change", LONE_SURROGATE.values(), ids=LONE_SURROGATE.keys())
def test_a_line_with_a_lone_surrogate_is_malformed_and_every_other_line_is_decided(
    fixture_db, tmp_path, change
):
    # json.dumps writes each lone surrogate as an escape, such as \ud800.
    bad = {**json.loads(line("u-bad", task_uid=4)), **change}
    source = write(tmp_path, line("u-001"), json.dumps(bad), line("u-002", task_uid=5))
    first = sync(fixture_db, source, clock=CLOCK)
    assert (first.status, first.seen, first.applied, first.conflicts, first.error) == ("ok", 3, 2, 1, None)
    again = sync(fixture_db, source, clock=CLOCK)
    assert (again.status, again.duplicates) == ("ok", 3)
    decided = {sid: (status, reason, detail) for sid, status, reason, detail, *_ in state(fixture_db)["outcomes"]}
    assert decided.pop("u-001")[0] == decided.pop("u-002")[0] == "applied"
    ((status, reason, detail),) = decided.values()  # the bad line, keyed by its bytes
    assert (status, reason) == ("conflict", "malformed")
    assert detail.startswith("the line is not JSON: a string holds a lone surrogate, U+D")


def test_a_second_run_at_the_same_time_is_refused(fixture_db, tmp_path):
    source = write(tmp_path, line("u-001"))
    with open(f"{fixture_db}.lock", "a") as held:  # another run's open lock file
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SyncRefused, match="another sync run is in progress"):
            sync(fixture_db, source, clock=CLOCK)
        with closing(open_db(fixture_db)) as conn:
            assert conn.execute("SELECT count(*) FROM sync_run").fetchone() == (0,)
    assert sync(fixture_db, source, clock=CLOCK).applied == 1  # the lock is free again


def test_a_run_through_a_symlink_shares_the_lock_of_the_real_path(fixture_db, tmp_path):
    link = tmp_path / "link.db"
    link.symlink_to(fixture_db)
    source = write(tmp_path, line("u-001"))
    with open(f"{fixture_db}.lock", "a") as held:  # a run on the real path holds its lock
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SyncRefused, match=f"another sync run is in progress \\(it holds {fixture_db.name}.lock\\)"):
            sync(link, source, clock=CLOCK)
    assert not (tmp_path / "link.db.lock").exists()
    assert sync(link, source, clock=CLOCK).applied == 1  # free again, through either name


class Crash(BaseException):
    """Stands in for the process dying: not an Exception, so the run is left running."""


def crash_on(monkeypatch, nth: int, error: type[BaseException]):
    """Make the nth applied update stop the run right after its progress write."""
    real, calls = progress.apply_progress, []

    def apply_then_stop(conn, outcome, **kwargs):
        real(conn, outcome, **kwargs)
        calls.append(outcome)
        if len(calls) == nth:
            raise error("stopped after a progress write")

    monkeypatch.setattr(progress, "apply_progress", apply_then_stop)


@pytest.mark.parametrize(("error", "status"), [(RuntimeError, "failed"), (Crash, "running")], ids=["error", "killed"])
def test_a_run_stopped_halfway_and_rerun_ends_as_an_uninterrupted_run(fresh_db, monkeypatch, error, status):
    uninterrupted, stopped = fresh_db(), fresh_db()
    sync(uninterrupted, FileSource(WEEK_44), clock=CLOCK)
    crash_on(monkeypatch, 3, error)
    if error is Crash:
        with pytest.raises(Crash):
            sync(stopped, FileSource(WEEK_44), clock=CLOCK)
        with closing(open_db(stopped)) as conn:
            (run_id, run_status) = conn.execute("SELECT id, status FROM sync_run").fetchone()
    else:
        result = sync(stopped, FileSource(WEEK_44), clock=CLOCK)
        run_id, run_status = result.run_id, result.status
        assert result.error == "RuntimeError: stopped after a progress write"
    assert run_status == status
    assert state(stopped) != state(uninterrupted)  # it really stopped partway
    monkeypatch.undo()
    again = rerun(stopped, run_id, clock=CLOCK)
    assert again.status == "ok" and again.duplicates > 0
    assert state(stopped) == state(uninterrupted)


@pytest.mark.parametrize("next_run", ["sync", "rerun"])
def test_the_next_run_marks_a_killed_run_interrupted_and_it_can_still_be_rerun(fresh_db, monkeypatch, next_run):
    uninterrupted, stopped = fresh_db(), fresh_db()
    sync(uninterrupted, FileSource(WEEK_44), clock=CLOCK)
    crash_on(monkeypatch, 3, Crash)
    with pytest.raises(Crash):
        sync(stopped, FileSource(WEEK_44), clock=CLOCK)
    monkeypatch.undo()
    with closing(open_db(stopped)) as conn:
        (killed,) = conn.execute("SELECT id FROM sync_run WHERE status = 'running'").fetchone()
    if next_run == "sync":
        later = sync(stopped, FileSource(WEEK_44), clock=CLOCK)
    else:
        later = rerun(stopped, killed, clock=CLOCK)
    assert later.status == "ok"
    with closing(open_db(stopped)) as conn:
        assert conn.execute("SELECT id, status, finished_at FROM sync_run ORDER BY rowid").fetchall() == [
            (killed, "interrupted", None), (later.run_id, "ok", later.finished_at),
        ]
    again = rerun(stopped, killed, clock=CLOCK)
    assert (again.status, again.applied, again.conflicts) == ("ok", 0, 0)
    assert state(stopped) == state(uninterrupted)


def test_a_rerun_after_a_complete_run_gives_only_duplicates(fixture_db):
    first = sync(fixture_db, FileSource(WEEK_44), clock=CLOCK)
    before = state(fixture_db)
    again = rerun(fixture_db, first.run_id, clock=CLOCK)
    assert (again.seen, again.applied, again.conflicts, again.duplicates) == (13, 0, 0, 13)  # one per key captured
    assert state(fixture_db) == before


def test_a_rerun_of_a_run_that_captured_nothing_is_refused(fixture_db, tmp_path):
    failed = sync(fixture_db, FileSource(tmp_path / "missing.jsonl"), clock=CLOCK)  # failed before capturing
    empty = sync(fixture_db, write(tmp_path), clock=CLOCK)  # read an empty file
    assert (failed.status, failed.seen, empty.status, empty.seen) == ("failed", 0, "ok", 0)
    for run in (failed, empty):
        with pytest.raises(SyncRefused, match=f"run {run.run_id} captured nothing, so there is nothing to rerun"):
            rerun(fixture_db, run.run_id, clock=CLOCK)
    with closing(open_db(fixture_db)) as conn:
        assert conn.execute("SELECT count(*) FROM sync_run").fetchone() == (2,)  # no rerun was recorded


def test_a_source_that_cant_be_read_fails_the_run_and_captures_nothing(fixture_db, tmp_path):
    result = sync(fixture_db, FileSource(tmp_path / "missing.jsonl"), clock=CLOCK)
    assert (result.status, result.seen) == ("failed", 0)
    assert result.error.startswith("SourceError: can't read the update file")
    with closing(open_db(fixture_db)) as conn:
        assert conn.execute("SELECT count(*) FROM source_update").fetchone() == (0,)


def test_a_source_whose_name_is_not_utf_8_is_refused_before_the_run_starts(fixture_db, tmp_path):
    path = tmp_path / os.fsdecode(b"upd-\xff.jsonl")  # as a shell passes the name: a lone surrogate
    path.write_text(line("u-001") + "\n")
    with pytest.raises(SyncRefused, match=r"the source must be UTF-8 text, not 'file:.*/upd-\\udcff\.jsonl'"):
        sync(fixture_db, FileSource(path), clock=CLOCK)
    with closing(open_db(fixture_db)) as conn:
        assert conn.execute("SELECT count(*) FROM sync_run").fetchone() == (0,)


def test_sync_needs_an_imported_project(tmp_path):
    empty = tmp_path / "empty.db"
    open_db(empty, create=True).close()
    with pytest.raises(SyncRefused, match="no project has been imported"):
        sync(empty, write(tmp_path, line("u-001")), clock=CLOCK)


def test_a_missing_database_is_refused_without_leaving_a_lock_file(tmp_path):
    with pytest.raises(Exception, match="can't open database"):
        sync(tmp_path / "typo.db", write(tmp_path, line("u-001")), clock=CLOCK)
    assert not (tmp_path / "typo.db.lock").exists()


def test_a_task_imported_complete_without_a_finish_refuses_a_notes_update(tmp_path):
    xml = tmp_path / "done.xml"
    xml.write_text(
        '<Project xmlns="http://schemas.microsoft.com/project"><Tasks><Task><UID>3</UID>'
        "<PercentComplete>100</PercentComplete></Task></Tasks></Project>"
    )
    db = tmp_path / "done.db"
    with closing(open_db(db, create=True)) as conn:
        import_file(conn, xml, now=datetime(2026, 10, 30, 12, tzinfo=UTC))
    sync(db, write(tmp_path, line("u-001", fields={"notes": "Punch list done"})), clock=CLOCK)
    assert outcomes(db) == {"u-001": ("conflict", "inconsistent_actuals")}


# The CLI

from schedule_guard import cli  # noqa: E402


def test_cli_sync_runs_rerun_and_conflicts(fixture_db, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_now", CLOCK)
    path = tmp_path / "updates.jsonl"
    path.write_text(f"{line('u-001')}\n{line('u-002', task_uid=1)}\n")
    db = str(fixture_db)
    assert cli.main(["sync", "now", "--db", db, "--source", f"file:{path}"]) == 0
    first = capsys.readouterr().out.strip()
    assert first.startswith("run ") and first.endswith(" now ok: 2 seen, 1 applied, 1 conflicts, 0 duplicates")
    run_id = first.split()[1]
    assert cli.main(["sync", "daily", "--db", db, "--source", f"file:{path}"]) == 0
    assert capsys.readouterr().out.strip().endswith(" daily ok: 2 seen, 0 applied, 0 conflicts, 2 duplicates")
    assert cli.main(["rerun", run_id, "--db", db]) == 0
    assert capsys.readouterr().out.strip().endswith(f" rerun ok: 2 seen, 0 applied, 0 conflicts, 2 duplicates; rerun of {run_id}")
    assert cli.main(["runs", "list", "--db", db]) == 0
    runs = capsys.readouterr().out.strip().splitlines()
    assert [r.split()[3:5] for r in runs] == [["now", "ok:"], ["daily", "ok:"], ["rerun", "ok:"]]
    assert cli.main(["conflicts", "list", "--db", db]) == 0
    assert capsys.readouterr().out == (
        "1 open summary_task: task 1, item file:u-002 version 1: task UID 1 is a summary task;"
        " Project rolls its progress up\n"
    )
    assert cli.main(["conflicts", "dismiss", "1", "--db", db, "--note", "Report progress on its subtasks"]) == 0
    assert capsys.readouterr().out == "dismissed conflict 1; the update stays unapplied\n"
    assert cli.main(["conflicts", "list", "--db", db]) == 0
    assert capsys.readouterr().out == "no open conflicts\n"
    assert cli.main(["conflicts", "list", "--db", db, "--all"]) == 0
    assert capsys.readouterr().out.endswith("; note: Report progress on its subtasks\n")


def test_cli_conflicts_list_escapes_what_it_prints(fixture_db, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_now", CLOCK)
    path = tmp_path / "updates.jsonl"
    path.write_text(line("u-001", task_uid=1) + "\n")
    db = str(fixture_db)
    assert cli.main(["sync", "now", "--db", db, "--source", f"file:{path}"]) == 0
    note = "ok" + chr(0x202E) + "\x1b[2J"  # a bidi override and a clear-screen sequence
    assert cli.main(["conflicts", "dismiss", "1", "--db", db, "--note", note]) == 0
    capsys.readouterr()
    assert cli.main(["conflicts", "list", "--db", db, "--all"]) == 0
    out = capsys.readouterr().out
    assert out.endswith("; note: ok\\u202e\\x1b[2J\n")
    assert "\x1b" not in out and chr(0x202E) not in out


def test_cli_reports_a_failed_run_and_exits_1(fixture_db, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_now", CLOCK)
    missing = tmp_path / "missing.jsonl"
    assert cli.main(["sync", "now", "--db", str(fixture_db), "--source", f"file:{missing}"]) == 1
    out, err = capsys.readouterr()
    assert out.strip().endswith(" now failed: 0 seen, 0 applied, 0 conflicts, 0 duplicates")
    assert "failed: SourceError: can't read the update file" in err


@pytest.mark.parametrize("spec", ["updates.jsonl", "graph:list", "file:"])
def test_cli_sync_needs_a_file_source(fixture_db, capsys, spec):
    assert cli.main(["sync", "now", "--db", str(fixture_db), "--source", spec]) == 1
    assert "the source must be file:<path>" in capsys.readouterr().err


def test_cli_rerun_of_an_unknown_run_is_refused(fixture_db, capsys):
    assert cli.main(["rerun", "no-such-run", "--db", str(fixture_db)]) == 1
    assert "there is no run 'no-such-run'" in capsys.readouterr().err


def test_cli_rerun_of_a_run_that_captured_nothing_is_refused(fixture_db, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_now", CLOCK)
    db = str(fixture_db)
    assert cli.main(["sync", "now", "--db", db, "--source", f"file:{tmp_path / 'missing.jsonl'}"]) == 1
    run_id = capsys.readouterr().out.split()[1]
    assert cli.main(["rerun", run_id, "--db", db]) == 1
    error = f"schedule-guard: error: run {run_id} captured nothing, so there is nothing to rerun\n"
    assert capsys.readouterr() == ("", error)


@pytest.mark.parametrize(
    ("command", "message"),
    [("sync", b"the source must be UTF-8 text"), ("dismiss", b"the note must be UTF-8 text")],
)
def test_cli_refuses_a_source_or_a_note_that_is_not_utf_8_with_one_error_line(
    fixture_db, tmp_path, command, message
):
    sync(fixture_db, write(tmp_path, line("u-001", task_uid=1)), clock=CLOCK)  # conflict 1: a summary task
    db = os.fsencode(fixture_db)
    if command == "sync":
        name = os.fsencode(tmp_path) + b"/upd-\xff.jsonl"
        shutil.copyfile(tmp_path / "updates.jsonl", name)
        args = [b"sync", b"now", b"--db", db, b"--source", b"file:" + name]
    else:
        args = [b"conflicts", b"dismiss", b"1", b"--db", db, b"--note", b"ok \xff"]
    # A new process, given the bytes as a shell would pass them.
    result = subprocess.run([sys.executable, "-m", "schedule_guard.cli", *args], capture_output=True, check=False)
    assert (result.returncode, result.stdout) == (1, b"")
    assert result.stderr.startswith(b"schedule-guard: error: " + message) and result.stderr.count(b"\n") == 1, (
        result.stderr
    )
    with closing(open_db(fixture_db)) as conn:
        assert conn.execute("SELECT count(*) FROM sync_run").fetchone() == (1,)  # no run recorded
        assert conn.execute("SELECT status FROM conflict").fetchall() == [("open",)]


def test_cli_runs_list_with_no_runs(fixture_db, capsys):
    assert cli.main(["runs", "list", "--db", str(fixture_db)]) == 0
    assert capsys.readouterr().out == "no runs yet\n"
