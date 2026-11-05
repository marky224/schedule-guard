"""demo: a new database from the fixture and demo-updates.jsonl, served as the report."""

import json
import os
import tempfile
from contextlib import closing
from pathlib import Path

import pytest
import uvicorn
from test_report import STOP_SIGNALS, Served, local, rows  # helpers only, so no test is collected twice

from schedule_guard import cli
from schedule_guard.demo import DEMO_CLOCK, FIXTURE, UPDATES, DemoRefused, build_demo
from schedule_guard.mspdi.parse import parse_mspdi
from schedule_guard.report.app import PAGES, create_app
from schedule_guard.store.db import open_db

REASONS = ["field_not_allowed", "inconsistent_actuals", "invalid_value", "malformed", "stale_version", "summary_task",
           "unknown_task"]


def test_the_demo_database_has_progress_overdue_tasks_reached_milestones_and_conflicts(tmp_path):
    db = tmp_path / "demo.db"
    now, daily = build_demo(db)
    assert (now.trigger, now.status, now.seen, now.applied, now.conflicts, now.duplicates) == ("now", "ok", 29, 22, 7, 0)
    assert (daily.trigger, daily.status, daily.applied, daily.conflicts, daily.duplicates) == ("daily", "ok", 0, 0, 29)
    with closing(open_db(db)) as conn:
        assert conn.execute("SELECT locked_by FROM baseline WHERE number = 0").fetchone() == ("baseline lock",)
        assert [r for (r,) in conn.execute("SELECT reason_code FROM conflict WHERE status = 'open' ORDER BY 1")] == REASONS
        assert conn.execute(
            "SELECT t.uid FROM task t JOIN task_progress p ON p.task_uid = t.uid"
            " WHERE t.is_milestone AND p.percent_complete = 100 ORDER BY t.uid"
        ).fetchall() == [(2,), (11,)]
    client = local(create_app(db, clock=DEMO_CLOCK))
    for route in PAGES:
        assert client.get(route).status_code == 200, route
    overdue = [cells[0] for cells in rows(client.get("/overdue").text) if cells]
    assert {"19", "20", "21", "24", "25", "26", "27"} <= set(overdue)
    assert not {"3", "11", "29"} & set(overdue)  # done, a reached milestone, not due yet
    with closing(open_db(db)) as conn:
        summaries = {str(uid) for (uid,) in conn.execute("SELECT uid FROM task WHERE is_summary")}
    assert summaries and not summaries & set(overdue)  # leaf tasks only


@pytest.mark.parametrize("content", [b"", b"someone else's file"], ids=["empty", "not-empty"])
def test_the_demo_never_writes_into_a_file_that_exists(tmp_path, content):
    db = tmp_path / "demo.db"
    db.write_bytes(content)
    with pytest.raises(DemoRefused, match="already exists"):
        build_demo(db)
    assert db.read_bytes() == content
    assert sorted(tmp_path.iterdir()) == [db]


def test_the_demo_updates_use_fake_names_and_the_fixtures_tasks():
    uids = {task.uid for task in parse_mspdi(FIXTURE.read_bytes()).tasks}
    lines = [json.loads(line) for line in UPDATES.read_text().splitlines()]
    assert all(line["submitted_by"].endswith(" (demo)") for line in lines)
    assert {line["task_uid"] for line in lines} - uids == {77}  # the one unknown_task conflict


def serving(monkeypatch) -> list[uvicorn.Server]:
    """Make uvicorn's Server.run record the server instead of listening."""
    served = []
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: served.append(self))
    return served


def test_cli_demo_builds_the_database_then_serves_the_report_on_127_0_0_1(tmp_path, monkeypatch, capsys):
    served = serving(monkeypatch)
    db = tmp_path / "demo.db"
    assert cli.main(["demo", "--db", str(db), "--port", "8123"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == f"built the demo database {db}; today is 2026-10-30 for the demo"
    assert out[1].endswith(" now ok: 29 seen, 22 applied, 7 conflicts, 0 duplicates")
    assert out[2].endswith(" daily ok: 29 seen, 0 applied, 0 conflicts, 29 duplicates")
    (server,) = served
    assert (server.config.host, server.config.port) == ("127.0.0.1", 8123)
    page = local(server.config.app).get("/")
    assert page.status_code == 200 and "Today is 2026-10-30" in page.text and "Riverbend Clinic" in page.text


def test_cli_demo_without_db_builds_in_a_temporary_directory_it_removes(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", None)  # so tempfile reads TMPDIR again
    built, there = [], []
    real = cli.build_demo
    monkeypatch.setattr(cli, "build_demo", lambda db: built.append(Path(db)) or real(db))
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: there.append(built[0].exists()))
    assert cli.main(["demo"]) == 0
    assert there == [True]  # while the report was served
    assert built[0].parent.parent == tmp_path
    assert not built[0].parent.exists()  # gone afterwards, directory and all


@pytest.mark.parametrize("signum", STOP_SIGNALS, ids=lambda signum: signum.name)
def test_cli_demo_without_db_stops_on_ctrl_c_sigterm_or_sighup_and_removes_its_directory(tmp_path, signum):
    with Served("demo", env={**os.environ, "TMPDIR": str(tmp_path)}) as served:
        (scratch,) = tmp_path.iterdir()  # while the report is served
        assert scratch.name.startswith("schedule-guard-demo-") and (scratch / "demo.db").is_file()
        code, err = served.stop(signum)
    assert code == 0, err
    assert "Traceback" not in err and "KeyboardInterrupt" not in err, err
    assert "Finished server process" in err
    assert list(tmp_path.iterdir()) == []  # the directory is gone, demo.db and all


def test_cli_demo_refuses_an_existing_database_with_one_error_line(tmp_path, monkeypatch, capsys):
    served = serving(monkeypatch)
    db = tmp_path / "demo.db"
    db.write_bytes(b"someone else's file")
    assert cli.main(["demo", "--db", str(db)]) == 1
    out, err = capsys.readouterr()
    assert out == "" and err.count("\n") == 1 and "already exists" in err
    assert db.read_bytes() == b"someone else's file" and served == []
