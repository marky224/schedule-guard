import dataclasses
import hashlib
import os
import subprocess
import sys
import unicodedata
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from schedule_guard import cli, importer
from schedule_guard.importer import ImportRefused, ImportResult, import_file
from schedule_guard.mspdi.parse import NS, MspdiError, parse_mspdi
from schedule_guard.store.db import open_db

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "riverbend-clinic.xml"
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
STAMP = "2026-10-30T12:00:00+00:00"
PLANNED = {f"{{{NS}}}{tag}" for tag in (
    "Name", "WBS", "OutlineLevel", "Summary", "Milestone", "Start", "Finish", "Duration", "Work", "PredecessorLink"
)}
TABLES = ("project", "task", "task_progress", "baseline", "baseline_task")


@pytest.fixture
def conn(tmp_path):
    with closing(open_db(tmp_path / "project.db", create=True)) as conn:
        yield conn


def contents(conn) -> dict:
    return {table: conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall() for table in TABLES}


def shape(el: ET.Element) -> list:
    """Tags, attributes, text and tails of a subtree, without the whitespace after it."""
    return [(e.tag, e.attrib, e.text, None if e is el else e.tail) for e in el.iter()]


def test_import_stores_the_file_its_hash_and_the_master_schedule(conn):
    raw = FIXTURE.read_bytes()
    sha256 = hashlib.sha256(raw).hexdigest()
    assert import_file(conn, FIXTURE, now=NOW) == ImportResult(
        name="Riverbend Clinic", tasks=40, baselines=(0,), source_sha256=sha256
    )
    assert conn.execute("SELECT * FROM project").fetchall() == [(1, "Riverbend Clinic", sha256, raw, STAMP, STAMP)]
    tasks = conn.execute("SELECT * FROM task ORDER BY uid").fetchall()
    assert tasks == [
        (1, t.uid, t.name, t.wbs, t.outline_level, int(t.is_summary), int(t.is_milestone), t.planned_start,
         t.planned_finish, t.planned_duration, t.planned_work, t.plan_c14n)
        for t in sorted(parse_mspdi(raw).tasks, key=lambda t: t.uid)
    ]
    assert conn.execute("SELECT count(*) FROM baseline_task WHERE number = 0").fetchone() == (40,)


def test_imported_c14n_bytes_round_trip(conn):
    import_file(conn, FIXTURE, now=NOW)
    (source,) = conn.execute("SELECT source_xml FROM project").fetchone()
    reparsed = {t.uid: t for t in parse_mspdi(source).tasks}
    # Read with plain ElementTree, not the parser, as an independent oracle.
    originals = {int(t.findtext(f"{{{NS}}}UID")): t for t in ET.fromstring(FIXTURE.read_bytes()).iter(f"{{{NS}}}Task")}

    for uid, plan in conn.execute("SELECT uid, plan_c14n FROM task"):
        assert plan == reparsed[uid].plan_c14n
        assert ET.canonicalize(xml_data=plan.decode()).encode() == plan
        stored = ET.fromstring(plan)
        planned = [child for child in originals[uid] if child.tag in PLANNED]
        assert [shape(child) for child in stored] == [shape(child) for child in planned]

    rows = conn.execute("SELECT number, task_uid, start, finish, duration, work, cost, xml_c14n FROM baseline_task")
    for number, uid, *values, xml in rows:
        (parsed,) = [b for b in reparsed[uid].baselines if b.number == number]
        assert xml == parsed.xml_c14n
        assert values == [parsed.start, parsed.finish, parsed.duration, parsed.work, parsed.cost]
        assert ET.canonicalize(xml_data=xml.decode()).encode() == xml
        (original,) = originals[uid].iterfind(f"{{{NS}}}Baseline")
        assert shape(ET.fromstring(xml)) == shape(original)


def test_import_seeds_each_tasks_progress_at_version_0(conn, tmp_path):
    import_file(conn, FIXTURE, now=NOW)
    rows = conn.execute("SELECT * FROM task_progress ORDER BY task_uid").fetchall()
    assert rows == [
        (1, t.uid, 0, t.percent_complete, t.actual_start, t.actual_finish, t.remaining_work, t.notes, None)
        for t in sorted(parse_mspdi(FIXTURE.read_bytes()).tasks, key=lambda t: t.uid)
    ]
    # The fixture has no actuals or notes yet, so seed a file that has them.
    started = tmp_path / "started.xml"
    started.write_text(
        f'<Project xmlns="{NS}"><Tasks><Task><UID>7</UID><PercentComplete>40</PercentComplete>'
        "<ActualStart>2026-07-16T08:00:00</ActualStart><RemainingWork>PT96H0M0S</RemainingWork>"
        "<Notes>Pump truck booked</Notes></Task><Task><UID>8</UID></Task></Tasks></Project>"
    )
    with closing(open_db(tmp_path / "started.db", create=True)) as other:
        import_file(other, started, now=NOW)
        assert other.execute("SELECT * FROM task_progress ORDER BY task_uid").fetchall() == [
            (1, 7, 0, 40, "2026-07-16T08:00:00", None, "PT96H0M0S", "Pump truck booked", None),
            (1, 8, 0, None, None, None, None, None, None),
        ]


def test_imported_baselines_stay_unlocked_until_baseline_lock(conn):
    import_file(conn, FIXTURE, now=NOW)
    assert conn.execute("SELECT * FROM baseline").fetchall() == [(1, 0, None, None, None, STAMP)]


def test_a_second_import_is_refused_and_changes_nothing(conn):
    import_file(conn, FIXTURE, now=NOW)
    before = contents(conn)
    with pytest.raises(ImportRefused, match="already holds a project"):
        import_file(conn, FIXTURE, now=datetime(2026, 11, 2, tzinfo=UTC))
    assert contents(conn) == before


def test_a_refused_file_stores_nothing(conn, tmp_path):
    bad = tmp_path / "bad.xml"
    bad.write_bytes(FIXTURE.read_bytes().replace(b"<Name>Site work</Name>", b"<Name>Site<x/>work</Name>"))
    with pytest.raises(MspdiError, match="Name has child elements"):
        import_file(conn, bad, now=NOW)
    assert contents(conn) == dict.fromkeys(TABLES, [])
    import_file(conn, FIXTURE, now=NOW)  # the database is still empty, so a good file imports


def test_import_is_one_transaction(conn, monkeypatch):
    # A task repeated after the other 40: the schema refuses it after the
    # project and 40 tasks are written, and the whole import rolls back.
    project = parse_mspdi(FIXTURE.read_bytes())
    repeated = dataclasses.replace(project, tasks=project.tasks + project.tasks[:1])
    monkeypatch.setattr(importer, "parse_mspdi", lambda data: repeated)
    with pytest.raises(Exception, match="task: already exists"):
        import_file(conn, FIXTURE, now=NOW)
    assert contents(conn) == dict.fromkeys(TABLES, [])
    assert not conn.in_transaction


def test_cli_import(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_now", lambda: NOW)
    db = tmp_path / "project.db"
    assert cli.main(["import", "--db", str(db), str(FIXTURE)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("imported Riverbend Clinic: 40 tasks, baselines 0 (unlocked), sha256 ")
    assert cli.main(["import", "--db", str(db), str(FIXTURE)]) == 1
    assert "schedule-guard: error: this database already holds a project" in capsys.readouterr().err


def test_cli_import_reports_a_missing_file(tmp_path, capsys):
    assert cli.main(["import", "--db", str(tmp_path / "project.db"), str(tmp_path / "missing.xml")]) == 1
    assert "schedule-guard: error: [Errno 2] No such file or directory" in capsys.readouterr().err


def test_cli_import_escapes_control_characters_in_the_name(tmp_path, monkeypatch, capsys):
    # XML 1.0 allows a CR (as &#13;) and C1 controls such as U+009B in text.
    monkeypatch.setattr(cli, "_now", lambda: NOW)
    crafted = tmp_path / "crafted.xml"
    title = "<Title>Fake&#13;\u009b2Kok</Title>".encode()
    crafted.write_bytes(FIXTURE.read_bytes().replace(b"<Title>Riverbend Clinic</Title>", title))
    db = tmp_path / "project.db"
    assert cli.main(["import", "--db", str(db), str(crafted)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("imported Fake\\r\\x9b2Kok: 40 tasks")
    assert not [ch for ch in out.removesuffix("\n") if unicodedata.category(ch) == "Cc"]
    with closing(open_db(db)) as conn:  # only the printed line is escaped
        assert conn.execute("SELECT name FROM project").fetchone() == ("Fake\r\u009b2Kok",)


def test_cli_import_escapes_format_characters_in_the_name(tmp_path, monkeypatch, capsys):
    # U+202E (right-to-left override) would reorder the rest of the line on a bidi terminal.
    monkeypatch.setattr(cli, "_now", lambda: NOW)
    crafted = tmp_path / "crafted.xml"
    title = "<Title>Fake\u202e\u2028Clinic</Title>".encode()
    crafted.write_bytes(FIXTURE.read_bytes().replace(b"<Title>Riverbend Clinic</Title>", title))
    assert cli.main(["import", "--db", str(tmp_path / "project.db"), str(crafted)]) == 0
    assert capsys.readouterr().out.startswith("imported Fake\\u202e\\u2028Clinic: 40 tasks")


def test_cli_import_escapes_a_file_name_that_is_not_utf_8(tmp_path):
    # With no Name or Title, import prints the file name. A non-UTF-8 byte in
    # it reaches Python as a lone surrogate, which a strict UTF-8 stdout can't
    # write: the import would commit and then end in a traceback.
    data = FIXTURE.read_bytes()
    for element in (b"<Name>riverbend-clinic.xml</Name>", b"<Title>Riverbend Clinic</Title>"):
        assert data.count(element) == 1
        data = data.replace(element, b"")
    path = os.path.join(os.fsencode(tmp_path), b"crafted\x9b2K.xml")
    with open(path, "wb") as f:
        f.write(data)
    result = subprocess.run(
        [sys.executable, "-m", "schedule_guard.cli", "import", "--db", str(tmp_path / "project.db"), path],
        capture_output=True,
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert b"Traceback" not in result.stderr
    assert result.stdout.startswith(b"imported crafted\\udc9b2K.xml: 40 tasks, baselines 0 (unlocked)")
    assert b"\x9b" not in result.stdout


@pytest.mark.parametrize(
    ("name", "content", "message"),
    [
        pytest.param("missing.xml", None, "No such file or directory", id="missing-file"),
        pytest.param("wrong-root.xml", b"<Project/>", "root element must be Project", id="refused-file"),
    ],
)
def test_cli_import_of_a_refused_file_leaves_no_database_file(tmp_path, capsys, name, content, message):
    source = tmp_path / name
    if content is not None:
        source.write_bytes(content)
    db = tmp_path / "new.db"
    assert cli.main(["import", "--db", str(db), str(source)]) == 1
    assert message in capsys.readouterr().err
    assert not db.exists()


def test_cli_import_needs_a_database_path(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["import", str(FIXTURE)])
    assert excinfo.value.code == 2
    assert "--db" in capsys.readouterr().err
