"""MSPDI export: the imported file with its progress written back (PLAN.md section 9.7, spec 002)."""

import ast
import hashlib
import re
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from schedule_guard import cli
from schedule_guard.baselines import rebaseline
from schedule_guard.clock import fixed_clock
from schedule_guard.importer import import_file
from schedule_guard.mspdi import export
from schedule_guard.mspdi.export import ExportRefused, export_mspdi, write_new_file
from schedule_guard.mspdi.parse import NS, c14n, parse_mspdi
from schedule_guard.sources.file import FileSource
from schedule_guard.store.db import StoreError, open_db
from schedule_guard.sync import sync

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "riverbend-clinic.xml"
WEEK_44 = Path(__file__).resolve().parent / "fixtures" / "updates" / "week-44.jsonl"
CLOCK = fixed_clock(datetime(2026, 10, 30, 17, 0, tzinfo=UTC))
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
EXPORT = Path(__file__).resolve().parent.parent / "src" / "schedule_guard" / "mspdi" / "export.py"
PROGRESS = "percent_complete, actual_start, actual_finish, remaining_work, notes"


def q(tag: str) -> str:
    return f"{{{NS}}}{tag}"


def small_db(tmp_path: Path, body: str) -> Path:
    """A database imported from a small MSPDI file whose Project holds `body`."""
    xml = tmp_path / "small.xml"
    xml.write_text(f'<Project xmlns="{NS}">{body}</Project>')
    db = tmp_path / "small.db"
    with closing(open_db(db, create=True)) as conn:
        import_file(conn, xml, now=NOW)
    return db


def set_progress(db: Path, uid: int, **values) -> None:
    """Write task_progress as an applied update leaves it (the table isn't protected)."""
    with closing(open_db(db)) as conn:
        names = ", ".join(f"{name} = ?" for name in values)
        conn.execute(f"UPDATE task_progress SET {names}, version = version + 1 WHERE task_uid = ?", (*values.values(), uid))


def tasks(data: bytes) -> dict[int, ET.Element]:
    return {int(t.findtext(q("UID"))): t for t in ET.fromstring(data).iter(q("Task"))}


def children(task: ET.Element) -> list[str]:
    return [child.tag.rpartition("}")[2] for child in task]


def test_a_project_with_no_applied_update_exports_c14n_identical_to_its_file(fixture_db):
    exported = export_mspdi(fixture_db)
    assert (exported.name, exported.tasks, exported.progress_tasks, exported.added_baselines) == (
        "Riverbend Clinic", 40, 0, ()
    )
    assert ET.canonicalize(exported.data.decode()) == ET.canonicalize(FIXTURE.read_text())
    assert exported.data.startswith(b"<?xml version='1.0' encoding='utf-8'?>\n<Project xmlns=\"" + NS.encode() + b'">')


def test_export_writes_each_tasks_progress_from_task_progress(fixture_db):
    assert sync(fixture_db, FileSource(WEEK_44), clock=CLOCK).applied == 5
    with closing(open_db(fixture_db)) as conn:
        stored = {uid: rest for uid, *rest in conn.execute(f"SELECT task_uid, {PROGRESS} FROM task_progress")}
    exported = export_mspdi(fixture_db)
    read = {t.uid: [t.percent_complete, t.actual_start, t.actual_finish, t.remaining_work, t.notes]
            for t in parse_mspdi(exported.data).tasks}
    assert read == stored
    imported = {t.uid: t for t in parse_mspdi(FIXTURE.read_bytes()).tasks}
    changed = {uid for uid, t in imported.items()
               if stored[uid] != [t.percent_complete, t.actual_start, t.actual_finish, t.remaining_work, t.notes]}
    assert exported.progress_tasks == len(changed) > 0


def test_a_new_progress_element_goes_to_its_place_in_the_task_sequence(tmp_path):
    db = small_db(tmp_path, (
        "<Tasks><Task><UID>1</UID><Name>Pour slab</Name><Start>2026-07-01T08:00:00</Start><Milestone>0</Milestone>"
        "<Summary>0</Summary><PercentComplete>0</PercentComplete><Hold>local</Hold><Cost>10</Cost>"
        "<PredecessorLink><PredecessorUID>2</PredecessorUID></PredecessorLink></Task>"
        "<Task><UID>2</UID></Task></Tasks>"
    ))
    set_progress(db, 1, percent_complete=100, actual_start="2026-07-01T08:00:00",
                 actual_finish="2026-07-02T17:00:00", remaining_work="PT0H0M0S", notes="Done")
    set_progress(db, 2, notes="Only a note")
    exported = tasks(export_mspdi(db).data)
    # ActualStart goes after Cost, not after Hold, a name the sequence doesn't list.
    assert children(exported[1]) == [
        "UID", "Name", "Start", "Milestone", "Summary", "PercentComplete", "Hold", "Cost", "ActualStart",
        "ActualFinish", "RemainingWork", "Notes", "PredecessorLink",
    ]
    assert [exported[1].findtext(q(tag)) for tag in ("PercentComplete", "ActualFinish", "Notes")] == [
        "100", "2026-07-02T17:00:00", "Done"
    ]
    assert children(exported[2]) == ["UID", "Notes"]


def test_a_value_equal_to_the_files_leaves_its_element_as_it_is(tmp_path):
    db = small_db(tmp_path, (
        "<Tasks><Task><UID>1</UID><PercentComplete> 40 </PercentComplete>"
        "<RemainingWork>\nPT8H0M0S\n</RemainingWork></Task></Tasks>"
    ))
    set_progress(db, 1, remaining_work="PT8H0M0S")  # the same value, as an update that repeats it leaves it
    exported = export_mspdi(db)
    assert exported.progress_tasks == 0
    assert b"<PercentComplete> 40 </PercentComplete><RemainingWork>\nPT8H0M0S\n</RemainingWork>" in exported.data


def test_a_new_element_takes_the_whitespace_after_the_element_before_it(fixture_db):
    set_progress(fixture_db, 3, actual_start="2026-06-01T08:00:00")
    text = export_mspdi(fixture_db).data.decode()
    assert ("      <PercentComplete>0</PercentComplete>\n      <ActualStart>2026-06-01T08:00:00</ActualStart>\n"
            "      <RemainingWork>") in text


def test_each_baseline_rebaseline_added_is_written_after_the_last_one(fixture_db):
    with closing(open_db(fixture_db)) as conn:
        for day in (1, 2):
            rebaseline(conn, confirm=True, reason="Owner approved", now=datetime(2026, 11, day, tzinfo=UTC))
        stored = {(uid, n): xml for uid, n, xml in conn.execute("SELECT task_uid, number, xml_c14n FROM baseline_task")}
    exported = export_mspdi(fixture_db)
    assert exported.added_baselines == (1, 2)
    imported = tasks(FIXTURE.read_bytes())
    for uid, task in tasks(exported.data).items():
        found = task.findall(q("Baseline"))
        assert [int(b.findtext(q("Number"))) for b in found] == [0, 1, 2]
        assert children(task)[-3:] == ["Baseline"] * 3  # after the imported one, the task's last child
        assert [c14n(b, "") for b in found] == [stored[uid, n] for n in (0, 1, 2)]
        assert c14n(found[0], "") == c14n(imported[uid].find(q("Baseline")), "")


def test_a_task_with_no_baseline_gets_the_added_one_at_its_place(tmp_path):
    db = small_db(tmp_path, (
        "<Tasks><Task><UID>1</UID><Start>2026-07-01T08:00:00</Start><Finish>2026-07-02T17:00:00</Finish>"
        "<PredecessorLink><PredecessorUID>2</PredecessorUID></PredecessorLink><OutlineCode>A</OutlineCode></Task>"
        "<Task><UID>2</UID></Task></Tasks>"
    ))
    with closing(open_db(db)) as conn:
        assert rebaseline(conn, confirm=True, reason="First baseline", now=NOW) == 0
    exported = tasks(export_mspdi(db).data)
    assert children(exported[1]) == ["UID", "Start", "Finish", "PredecessorLink", "Baseline", "OutlineCode"]
    assert children(exported[2]) == ["UID", "Baseline"]


def test_a_carriage_return_in_a_note_is_written_as_a_character_reference(tmp_path):
    db = small_db(tmp_path, "<Tasks><Task><UID>1</UID><Name>Line one&#13;\nline two</Name></Task></Tasks>")
    set_progress(db, 1, notes="[2026-10-30, file, Crew lead A] Gate hung\r\nFence inspected")
    data = export_mspdi(db).data
    assert b"\r" not in data and data.count(b"&#13;") == 2
    (task,) = parse_mspdi(data).tasks
    assert (task.name, task.notes) == ("Line one\r\nline two", "[2026-10-30, file, Crew lead A] Gate hung\r\nFence inspected")


@pytest.mark.parametrize("body", [
    '<Calendars xmlns=""><Calendar>Standard</Calendar></Calendars><Tasks><Task><UID>1</UID></Task></Tasks>',
    '<Tasks><Task><UID>1</UID><ExtendedAttribute kind="text"><Value>A</Value></ExtendedAttribute></Task></Tasks>',
], ids=["element", "attribute"])
def test_export_refuses_a_name_outside_any_namespace(tmp_path, body):
    db = small_db(tmp_path, body)
    with pytest.raises(ExportRefused, match="outside any namespace"):
        export_mspdi(db)


@pytest.mark.parametrize("body", [
    f'<Tasks><Task xmlns:p="{NS}" p:flag="x"><UID>1</UID></Task></Tasks>',
    f'<Calendars xmlns:p="{NS}" p:kind="standard"/><Tasks><Task><UID>1</UID></Task></Tasks>',
], ids=["on-a-task", "outside-the-tasks"])
def test_export_refuses_an_attribute_in_the_mspdi_namespace_with_one_error_line(tmp_path, capsys, body):
    # ElementTree would write it unprefixed under the default namespace, which moves it out of the namespace.
    db = small_db(tmp_path, body)
    out = tmp_path / "out.xml"
    assert cli.main(["export", "--db", str(db), "--out", str(out)]) == 1
    stdout, err = capsys.readouterr()
    assert stdout == "" and err.count("\n") == 1
    assert err.startswith("schedule-guard: error: the file has an attribute in the MSPDI namespace"), err
    assert not out.exists()


def test_a_utf16_file_is_written_as_utf8_with_the_same_elements(tmp_path):
    xml = tmp_path / "utf16.xml"
    text = FIXTURE.read_text().replace("encoding='utf-8'", "encoding='utf-16'")
    xml.write_bytes(text.encode("utf-16"))
    db = tmp_path / "utf16.db"
    with closing(open_db(db, create=True)) as conn:
        import_file(conn, xml, now=NOW)
    data = export_mspdi(db).data
    assert data.startswith(b"<?xml version='1.0' encoding='utf-8'?>")
    assert ET.canonicalize(data.decode()) == ET.canonicalize(FIXTURE.read_text())


def test_export_refuses_a_database_with_no_project(tmp_path):
    open_db(tmp_path / "empty.db", create=True).close()
    with pytest.raises(ExportRefused, match="no project has been imported"):
        export_mspdi(tmp_path / "empty.db")


def test_export_refuses_a_missing_database_and_creates_none(tmp_path):
    with pytest.raises(StoreError, match="can't open database"):
        export_mspdi(tmp_path / "typo.db")
    assert list(tmp_path.iterdir()) == []


def test_export_leaves_the_database_file_byte_identical(fixture_db):
    sync(fixture_db, FileSource(WEEK_44), clock=CLOCK)
    folder = sorted(fixture_db.parent.iterdir())
    before = hashlib.sha256(fixture_db.read_bytes()).hexdigest()
    export_mspdi(fixture_db)
    assert hashlib.sha256(fixture_db.read_bytes()).hexdigest() == before
    assert sorted(fixture_db.parent.iterdir()) == folder  # no -wal, -shm or journal left either


def imports(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_the_export_imports_nothing_that_writes_and_runs_no_write_statement():
    schedule_guard = {name for name in imports(EXPORT) if name.startswith("schedule_guard.")}
    assert schedule_guard <= {
        "schedule_guard.mspdi.parse.NS", "schedule_guard.mspdi.parse.MspdiTask", "schedule_guard.mspdi.parse.parse_tree",
        "schedule_guard.mspdi.parse.read_project", "schedule_guard.mspdi.parse.task_elements",
        "schedule_guard.store.db.open_db_read_only",
    }
    strings = [node.value for node in ast.walk(ast.parse(EXPORT.read_text()))
               if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    for text in strings:
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER|ATTACH|VACUUM)\b", text), text


# write_new_file: --out must not exist (Q97).


def test_write_new_file_writes_the_bytes_with_the_mode_a_new_file_gets(tmp_path):
    with open(tmp_path / "reference.txt", "x"):
        pass
    write_new_file(tmp_path / "out.xml", b"<Project/>\n")
    assert (tmp_path / "out.xml").read_bytes() == b"<Project/>\n"
    assert (tmp_path / "out.xml").stat().st_mode == (tmp_path / "reference.txt").stat().st_mode
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out.xml", "reference.txt"]  # no temporary file left


@pytest.mark.parametrize("kind", ["file", "folder", "symlink", "dangling-symlink"])
def test_write_new_file_refuses_an_existing_path_and_leaves_it(tmp_path, kind):
    out = tmp_path / "out.xml"
    target = tmp_path / "target.xml"
    if kind == "file":
        out.write_bytes(b"keep me")
    elif kind == "folder":
        out.mkdir()
    else:
        if kind == "symlink":
            target.write_bytes(b"keep me")
        out.symlink_to(target)
    before = sorted((p.name, p.is_symlink(), p.read_bytes() if p.is_file() else None) for p in tmp_path.iterdir())
    with pytest.raises(ExportRefused, match="already exists"):
        write_new_file(out, b"<Project/>\n")
    assert sorted((p.name, p.is_symlink(), p.read_bytes() if p.is_file() else None) for p in tmp_path.iterdir()) == before


def test_write_new_file_in_a_missing_folder_creates_nothing(tmp_path):
    with pytest.raises(FileNotFoundError):
        write_new_file(tmp_path / "missing" / "out.xml", b"<Project/>\n")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(("step", "error"), [("fsync", OSError(28, "No space left on device")),
                                             ("replace", KeyboardInterrupt())], ids=["disk-full", "interrupted"])
def test_write_new_file_removes_both_files_when_it_fails(tmp_path, monkeypatch, step, error):
    def fail(*args):
        raise error

    monkeypatch.setattr(export.os, step, fail)
    with pytest.raises(type(error)):
        write_new_file(tmp_path / "out.xml", b"<Project/>\n")
    assert list(tmp_path.iterdir()) == []


# The CLI: export --db PATH --out FILE.


def test_cli_export_writes_the_file_and_prints_one_line(fixture_db, tmp_path, capsys):
    sync(fixture_db, FileSource(WEEK_44), clock=CLOCK)
    out = tmp_path / "riverbend-progress.xml"
    assert cli.main(["export", "--db", str(fixture_db), "--out", str(out)]) == 0
    assert capsys.readouterr() == (
        "exported Riverbend Clinic: 40 tasks, progress written on 3, baselines added by rebaseline: none;"
        f" wrote {out}\n", "",
    )
    assert out.read_bytes() == export_mspdi(fixture_db).data


def test_cli_export_refuses_an_existing_out_with_one_error_line(fixture_db, tmp_path, capsys):
    out = tmp_path / "out.xml"
    out.write_bytes(b"keep me")
    assert cli.main(["export", "--db", str(fixture_db), "--out", str(out)]) == 1
    assert capsys.readouterr() == (
        "", f"schedule-guard: error: {out} already exists; export writes a new file and never replaces one\n"
    )
    assert out.read_bytes() == b"keep me"
    assert list(tmp_path.iterdir()) == [out]


def test_cli_export_refuses_a_missing_database_and_writes_nothing(tmp_path, capsys):
    assert cli.main(["export", "--db", str(tmp_path / "typo.db"), "--out", str(tmp_path / "out.xml")]) == 1
    out, err = capsys.readouterr()
    assert out == "" and err.startswith("schedule-guard: error: can't open database") and err.count("\n") == 1
    assert list(tmp_path.iterdir()) == []


def test_cli_export_needs_out(fixture_db, capsys):
    with pytest.raises(SystemExit) as exit_:
        cli.main(["export", "--db", str(fixture_db)])
    assert exit_.value.code == 2
    assert "the following arguments are required: --out" in capsys.readouterr().err


def test_cli_the_exported_file_imports_into_an_empty_project(fixture_db, tmp_path, capsys):
    sync(fixture_db, FileSource(WEEK_44), clock=CLOCK)
    with closing(open_db(fixture_db)) as conn:
        rebaseline(conn, confirm=True, reason="Owner approved", now=NOW)
    out, again = tmp_path / "out.xml", tmp_path / "again.db"
    assert cli.main(["export", "--db", str(fixture_db), "--out", str(out)]) == 0
    assert cli.main(["import", "--db", str(again), str(out)]) == 0
    assert capsys.readouterr().out.splitlines()[1].startswith("imported Riverbend Clinic: 40 tasks, baselines 0, 1")
    with closing(open_db(fixture_db)) as first, closing(open_db(again)) as second:
        for sql in ("SELECT uid, plan_c14n FROM task ORDER BY uid",
                    "SELECT number, task_uid, xml_c14n FROM baseline_task ORDER BY number, task_uid",
                    f"SELECT task_uid, {PROGRESS} FROM task_progress ORDER BY task_uid"):
            assert first.execute(sql).fetchall() == second.execute(sql).fetchall(), sql
