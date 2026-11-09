import os
import subprocess
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from schedule_guard import cli
from schedule_guard.baselines import MAX_BASELINE_NUMBER, BaselineRefused, lock_baseline, rebaseline
from schedule_guard.importer import import_file
from schedule_guard.mspdi.parse import NS, parse_mspdi
from schedule_guard.store.db import open_db

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "riverbend-clinic.xml"
IMPORTED = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 31, 9, 30, tzinfo=UTC)
STAMP = "2026-10-31T09:30:00+00:00"
TABLES = ("project", "task", "task_progress", "baseline", "baseline_task")


@pytest.fixture
def conn(tmp_path):
    with closing(open_db(tmp_path / "project.db", create=True)) as conn:
        import_file(conn, FIXTURE, now=IMPORTED)
        yield conn


def contents(conn) -> dict:
    return {table: conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall() for table in TABLES}


def baseline_rows(conn, number: int) -> list:
    return conn.execute("SELECT * FROM baseline_task WHERE number = ? ORDER BY task_uid", (number,)).fetchall()


def test_lock_locks_an_imported_baseline_once(conn):
    rows = baseline_rows(conn, 0)
    lock_baseline(conn, 0, now=NOW)
    assert conn.execute("SELECT locked_at, locked_by FROM baseline WHERE number = 0").fetchone() == (
        STAMP, "baseline lock"
    )
    before = contents(conn)
    with pytest.raises(BaselineRefused, match="baseline 0 is already locked"):
        lock_baseline(conn, 0, now=datetime(2026, 11, 2, tzinfo=UTC))
    assert contents(conn) == before
    assert baseline_rows(conn, 0) == rows


def test_lock_refuses_a_missing_baseline(conn):
    before = contents(conn)
    with pytest.raises(BaselineRefused, match="there is no baseline 3"):
        lock_baseline(conn, 3, now=NOW)
    assert contents(conn) == before


@pytest.mark.parametrize("number", [-1, 11, 99999999999999999999])
def test_lock_refuses_a_number_outside_0_to_10(conn, number):
    before = contents(conn)
    with pytest.raises(BaselineRefused, match=f"there is no baseline {number}: numbers run from 0 to 10"):
        lock_baseline(conn, number, now=NOW)
    assert contents(conn) == before


@pytest.mark.parametrize("command", [
    pytest.param(lambda conn: lock_baseline(conn, 0, now=NOW), id="lock"),
    pytest.param(lambda conn: rebaseline(conn, confirm=True, reason="r", now=NOW), id="rebaseline"),
])
def test_baseline_commands_need_an_imported_project(tmp_path, command):
    with closing(open_db(tmp_path / "empty.db", create=True)) as conn:
        with pytest.raises(BaselineRefused, match="no project has been imported"):
            command(conn)


@pytest.mark.parametrize(
    ("confirm", "reason", "message"),
    [
        pytest.param(False, "Owner moved the opening date", "pass --confirm", id="no-confirm"),
        pytest.param(True, None, "needs --reason", id="no-reason"),
        pytest.param(True, "", "needs --reason", id="empty-reason"),
        pytest.param(True, " \t", "needs --reason", id="blank-reason"),
        pytest.param(True, "new \udcff", "the reason must be UTF-8 text", id="reason-not-utf-8"),  # b"new \xff"
    ],
)
def test_rebaseline_refuses_without_confirm_and_reason(conn, confirm, reason, message):
    before = contents(conn)
    with pytest.raises(BaselineRefused, match=message):
        rebaseline(conn, confirm=confirm, reason=reason, now=NOW)
    assert contents(conn) == before


def test_rebaseline_appends_a_locked_baseline_from_the_planned_values(conn):
    baseline_0 = baseline_rows(conn, 0)
    assert rebaseline(conn, confirm=True, reason="Owner moved the opening date", now=NOW) == 1
    assert conn.execute("SELECT * FROM baseline WHERE number = 1").fetchone() == (
        1, 1, STAMP, "rebaseline", "Owner moved the opening date", STAMP
    )
    planned = conn.execute(
        "SELECT uid, planned_start, planned_finish, planned_duration, planned_work FROM task ORDER BY uid"
    ).fetchall()
    assert [row[2:7] for row in baseline_rows(conn, 1)] == planned
    assert all(row[7] is None for row in baseline_rows(conn, 1))  # cost isn't a planned value
    assert baseline_rows(conn, 0) == baseline_0  # the older baseline never changes
    # Some fixture tasks slipped after Baseline 0, so Baseline 1 differs from it.
    assert [row[3:7] for row in baseline_rows(conn, 1)] != [row[3:7] for row in baseline_0]


def test_rebaseline_bytes_match_an_imported_baseline_with_the_same_values(conn):
    rebaseline(conn, confirm=True, reason="r", now=NOW)
    for uid, start, finish, duration, work, xml in conn.execute(
        "SELECT task_uid, start, finish, duration, work, xml_c14n FROM baseline_task WHERE number = 1"
    ):
        children = "".join(
            f"<{tag}>{value}</{tag}>"
            for tag, value in (("Start", start), ("Finish", finish), ("Duration", duration), ("Work", work))
            if value is not None
        )
        data = (
            f'<Project xmlns="{NS}"><Tasks><Task><UID>{uid}</UID>'
            f"<Baseline><Number>1</Number>{children}</Baseline></Task></Tasks></Project>"
        ).encode()
        (task,) = parse_mspdi(data).tasks
        assert xml == task.baselines[0].xml_c14n


def test_rebaseline_stops_after_baseline_number_10(conn):
    numbers = [rebaseline(conn, confirm=True, reason=f"change {n}", now=NOW) for n in range(MAX_BASELINE_NUMBER)]
    assert numbers == list(range(1, MAX_BASELINE_NUMBER + 1))
    before = contents(conn)
    with pytest.raises(BaselineRefused, match="baseline 10 exists; numbers stop at 10"):
        rebaseline(conn, confirm=True, reason="one more", now=NOW)
    assert contents(conn) == before


def test_rebaseline_counts_from_the_highest_imported_number(tmp_path):
    path = tmp_path / "late.xml"
    path.write_text(
        f'<Project xmlns="{NS}"><Tasks><Task><UID>1</UID><Start>2026-06-01T08:00:00</Start>'
        "<Baseline><Number>0</Number></Baseline><Baseline><Number>10</Number></Baseline></Task></Tasks></Project>"
    )
    with closing(open_db(tmp_path / "late.db", create=True)) as conn:
        import_file(conn, path, now=IMPORTED)
        with pytest.raises(BaselineRefused, match="baseline 10 exists"):
            rebaseline(conn, confirm=True, reason="r", now=NOW)


def test_cli_baseline_lock_and_rebaseline(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_now", lambda: NOW)
    db = str(tmp_path / "project.db")
    assert cli.main(["import", "--db", db, str(FIXTURE)]) == 0
    assert cli.main(["baseline", "lock", "--db", db]) == 0
    assert cli.main(["baseline", "lock", "--db", db, "--number", "5"]) == 1
    assert cli.main(["rebaseline", "--db", db, "--reason", "Owner moved the opening date"]) == 1
    assert cli.main(["rebaseline", "--db", db, "--confirm"]) == 1
    assert cli.main(["rebaseline", "--db", db, "--confirm", "--reason", "Owner moved the opening date"]) == 0
    out, err = capsys.readouterr()
    assert out.splitlines()[1:] == ["locked baseline 0", "added baseline 1 from the current planned values and locked it"]
    assert err.splitlines() == [
        "schedule-guard: error: there is no baseline 5",
        "schedule-guard: error: rebaseline adds a baseline that can never be removed; pass --confirm",
        'schedule-guard: error: rebaseline needs --reason "..."',
    ]
    with closing(open_db(db)) as conn:
        assert conn.execute("SELECT number, locked_by FROM baseline ORDER BY number").fetchall() == [
            (0, "baseline lock"), (1, "rebaseline")
        ]


def test_cli_rebaseline_refuses_a_reason_that_is_not_utf_8_with_one_error_line(tmp_path):
    db = tmp_path / "project.db"
    with closing(open_db(db, create=True)) as conn:
        import_file(conn, FIXTURE, now=IMPORTED)
    # A new process, given the bytes as a shell would pass them.
    args = [b"rebaseline", b"--db", os.fsencode(db), b"--confirm", b"--reason", b"new \xff"]
    result = subprocess.run([sys.executable, "-m", "schedule_guard.cli", *args], capture_output=True, check=False)
    assert (result.returncode, result.stdout, result.stderr) == (
        1, b"", b"schedule-guard: error: the reason must be UTF-8 text\n"
    )
    with closing(open_db(db)) as conn:
        assert conn.execute("SELECT count(*) FROM baseline").fetchone() == (1,)  # Baseline 0 only


def test_cli_baseline_lock_refuses_a_number_too_big_for_sqlite(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_now", lambda: NOW)
    db = str(tmp_path / "project.db")
    assert cli.main(["import", "--db", db, str(FIXTURE)]) == 0
    capsys.readouterr()
    assert cli.main(["baseline", "lock", "--db", db, "--number", "99999999999999999999"]) == 1
    assert capsys.readouterr().err == (
        "schedule-guard: error: there is no baseline 99999999999999999999: numbers run from 0 to 10\n"
    )


def test_cli_baseline_commands_refuse_a_missing_database(tmp_path, capsys):
    missing = tmp_path / "typo.db"
    assert cli.main(["baseline", "lock", "--db", str(missing)]) == 1
    assert "can't open database" in capsys.readouterr().err
    assert not missing.exists()
