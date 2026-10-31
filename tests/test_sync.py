"""Sync: one transaction per item, exactly once, in (submitted_at, source_item_id) order."""

import json
from contextlib import closing
from datetime import UTC, datetime

import pytest

from schedule_guard.clock import fixed_clock
from schedule_guard.sources.file import FileSource
from schedule_guard.store.db import open_db
from schedule_guard.sync import rerun, sync

CLOCK = fixed_clock(datetime(2026, 10, 30, 17, 0, tzinfo=UTC))


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


def test_cli_runs_list_with_no_runs(fixture_db, capsys):
    assert cli.main(["runs", "list", "--db", str(fixture_db)]) == 0
    assert capsys.readouterr().out == "no runs yet\n"
