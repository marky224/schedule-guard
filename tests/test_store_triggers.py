"""The protected tables refuse every write except import, lock and rebaseline.

Each attack runs on a fresh sqlite3 connection with SQLite's defaults
(triggers on, recursive_triggers OFF), as any other writer of the file
would, and must leave every protected row as it was.
"""

import re
import sqlite3
from contextlib import closing

import pytest

from schedule_guard.store.db import SCHEMA_VERSION, StoreError, open_db, transaction

PROTECTED = {
    "project": "id",
    "task": "project_id, uid",
    "baseline": "project_id, number",
    "baseline_task": "project_id, number, task_uid",
}


def build(path, *, finish_import: bool) -> None:
    """Project 1 with tasks 1-3; Baseline 0 (unlocked) and Baseline 1 (locked) have rows for tasks 1 and 2."""
    conn = open_db(path, create=True)
    with transaction(conn):
        conn.execute("INSERT INTO project VALUES (1, 'P', 'hash', x'3c502f3e', 't0', NULL)")
        for uid in (1, 2, 3):
            conn.execute("INSERT INTO task VALUES (1, ?, 'T', '1', 1, 0, 0, 's', 'f', 'd', 'w', x'01')", (uid,))
        for number in (0, 1):
            conn.execute("INSERT INTO baseline VALUES (1, ?, NULL, NULL, NULL, 't0')", (number,))
            for uid in (1, 2):
                conn.execute("INSERT INTO baseline_task VALUES (1, ?, ?, 's', 'f', 'd', 'w', '5', x'02')", (number, uid))
        conn.execute("UPDATE baseline SET locked_at = 't1', locked_by = 'test' WHERE number = 1")
        if finish_import:
            conn.execute("UPDATE project SET import_finished_at = 't2' WHERE id = 1")
    conn.close()


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "store.db"
    build(path, finish_import=True)
    return path


@pytest.fixture
def importing_db(tmp_path):
    """The same rows while the import is still running, so only the key triggers and WITHOUT ROWID stand in the way."""
    path = tmp_path / "store.db"
    build(path, finish_import=False)
    return path


def snapshot(path) -> dict:
    with closing(sqlite3.connect(path)) as conn:
        return {table: conn.execute(f"SELECT * FROM {table} ORDER BY {key}").fetchall() for table, key in PROTECTED.items()}


def refused(path, sql: str) -> str:
    """Run `sql` on a fresh default connection; return SQLite's error, checking nothing changed."""
    before = snapshot(path)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA recursive_triggers").fetchone() == (0,)
        with pytest.raises(sqlite3.DatabaseError) as excinfo:
            conn.execute(sql)
            conn.commit()
    assert snapshot(path) == before
    return str(excinfo.value)


def allowed(path, sql: str) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(sql)
        conn.commit()


ROW = {
    "project": "(1, 'P', 'other-hash', x'00', 't0', NULL)",
    "task": "(1, 1, 'T', '1', 1, 0, 0, 's', 'later', 'd', 'w', x'09')",
    "baseline": "(1, 1, NULL, NULL, NULL, 't0')",  # Baseline 1 again, unlocked
    "baseline_task": "(1, 0, 1, 's', 'later', 'd', 'w', '5', x'09')",  # a row of unlocked Baseline 0
}

REFUSED = {
    # project: only the one-time set of import_finished_at
    "project-delete": ("DELETE FROM project", "project: rows are never deleted"),
    "project-id": ("UPDATE project SET id = 2", "project: only import_finished_at"),
    "project-bytes": ("UPDATE project SET source_xml = x'00'", "project: only import_finished_at"),
    "project-bytes-as-text": ("UPDATE project SET source_xml = CAST(source_xml AS TEXT)", "project: only import_finished_at"),
    "project-hash": ("UPDATE project SET source_sha256 = 'other'", "project: only import_finished_at"),
    "project-name": ("UPDATE project SET name = 'other'", "project: only import_finished_at"),
    "project-imported-at": ("UPDATE project SET imported_at = 't9'", "project: only import_finished_at"),
    "project-finish-again": ("UPDATE project SET import_finished_at = 't9'", "project: only import_finished_at"),
    "project-finish-unset": ("UPDATE project SET import_finished_at = NULL", "project: only import_finished_at"),
    # task: insert only
    "task-update": ("UPDATE task SET planned_finish = 'later'", "task: rows are read-only"),
    "task-update-plan-bytes": ("UPDATE task SET plan_c14n = x'09' WHERE uid = 1", "task: rows are read-only"),
    "task-delete": ("DELETE FROM task WHERE uid = 3", "task: rows are never deleted"),
    # baseline: only the one-time lock
    "baseline-delete-locked": ("DELETE FROM baseline WHERE number = 1", "baseline: rows are never deleted"),
    "baseline-delete-unlocked": ("DELETE FROM baseline WHERE number = 0", "baseline: rows are never deleted"),
    "baseline-unlock": (
        "UPDATE baseline SET locked_at = NULL, locked_by = NULL WHERE number = 1", "baseline: only the one-time lock"
    ),
    "baseline-relock": (
        "UPDATE baseline SET locked_at = 't9', locked_by = 'x' WHERE number = 1", "baseline: only the one-time lock"
    ),
    "baseline-renumber": ("UPDATE baseline SET number = 7 WHERE number = 0", "baseline: only the one-time lock"),
    "baseline-reason": ("UPDATE baseline SET reason = 'x' WHERE number = 0", "baseline: only the one-time lock"),
    # The one-time lock together with a change to any other column.
    "baseline-lock-and-reason": (
        "UPDATE baseline SET locked_at = 't9', locked_by = 'x', reason = 'x' WHERE number = 0",
        "baseline: only the one-time lock",
    ),
    "baseline-lock-and-project": (
        "UPDATE baseline SET locked_at = 't9', locked_by = 'x', project_id = 2 WHERE number = 0",
        "baseline: only the one-time lock",
    ),
    "baseline-lock-and-number": (
        "UPDATE baseline SET locked_at = 't9', locked_by = 'x', number = 7 WHERE number = 0",
        "baseline: only the one-time lock",
    ),
    # Renumbering onto locked Baseline 1 would delete it through REPLACE.
    "baseline-lock-and-renumber-replace": (
        "UPDATE OR REPLACE baseline SET locked_at = 't9', locked_by = 'x', number = 1 WHERE number = 0",
        "baseline: only the one-time lock",
    ),
    "baseline-lock-and-created-at": (
        "UPDATE baseline SET locked_at = 't9', locked_by = 'x', created_at = 't9' WHERE number = 0",
        "baseline: only the one-time lock",
    ),
    "baseline-lock-without-by": ("UPDATE baseline SET locked_at = 't9' WHERE number = 0", "baseline: only the one-time lock"),
    "baseline-by-without-lock": ("UPDATE baseline SET locked_by = 'x' WHERE number = 0", "baseline: only the one-time lock"),
    # baseline_task: insert only
    "baseline-task-update-locked": ("UPDATE baseline_task SET finish = 'later' WHERE number = 1", "baseline_task: rows are read-only"),
    "baseline-task-update-unlocked": ("UPDATE baseline_task SET xml_c14n = x'09' WHERE number = 0", "baseline_task: rows are read-only"),
    "baseline-task-delete": ("DELETE FROM baseline_task WHERE number = 0", "baseline_task: rows are never deleted"),
    # INSERT OR REPLACE (and upsert) with an existing key
    "project-replace": (
        f"INSERT OR REPLACE INTO project VALUES {ROW['project']}", "project: this database already holds a project"
    ),
    "project-replace-text-key": (
        "REPLACE INTO project VALUES ('1', 'P', 'other-hash', x'00', 't0', NULL)",
        "project: this database already holds a project",
    ),
    "project-second": (
        "INSERT INTO project VALUES (2, 'Q', 'hash', x'00', 't0', NULL)", "project: this database already holds a project"
    ),
    "task-replace": (f"INSERT OR REPLACE INTO task VALUES {ROW['task']}", "task: (already exists|added only while)"),
    "baseline-replace-locked": (f"INSERT OR REPLACE INTO baseline VALUES {ROW['baseline']}", "baseline: number already exists"),
    "baseline-replace-unlocked": (
        "REPLACE INTO baseline VALUES (1, 0, NULL, NULL, 'other', 't9')", "baseline: number already exists"
    ),
    "baseline-upsert-unlock": (
        f"INSERT INTO baseline VALUES {ROW['baseline']} ON CONFLICT DO UPDATE SET locked_at = NULL, locked_by = NULL",
        "baseline: number already exists",
    ),
    "baseline-task-replace-unlocked": (
        f"INSERT OR REPLACE INTO baseline_task VALUES {ROW['baseline_task']}", "baseline_task: already exists"
    ),
    "baseline-task-replace-locked": (
        "INSERT OR REPLACE INTO baseline_task VALUES (1, 1, 1, 's', 'later', 'd', 'w', '5', x'09')",
        "baseline_task: (already exists|its baseline is missing or locked)",
    ),
    # rows with a missing or closed parent
    "task-after-import": (
        "INSERT INTO task VALUES (1, 4, 'T', '1', 1, 0, 0, 's', 'f', 'd', 'w', x'01')",
        "task: added only while its project is being imported",
    ),
    "task-missing-project": (
        "INSERT INTO task VALUES (2, 1, 'T', '1', 1, 0, 0, 's', 'f', 'd', 'w', x'01')",
        "task: added only while its project is being imported",
    ),
    "baseline-missing-project": ("INSERT INTO baseline VALUES (2, 0, NULL, NULL, NULL, 't0')", "baseline: project does not exist"),
    # A new baseline goes in unlocked and numbered 0-10.
    "baseline-insert-locked": (
        "INSERT INTO baseline VALUES (1, 5, 't1', 'x', 'r', 't0')", "baseline: inserted unlocked"
    ),
    "baseline-insert-locked-at-only": (
        "INSERT INTO baseline VALUES (1, 5, 't1', NULL, 'r', 't0')", "baseline: inserted unlocked"
    ),
    "baseline-insert-locked-by-only": (
        "INSERT INTO baseline VALUES (1, 5, NULL, 'x', 'r', 't0')", "baseline: inserted unlocked"
    ),
    "baseline-insert-number-11": (
        "INSERT INTO baseline VALUES (1, 11, NULL, NULL, 'r', 't0')", "baseline: number is not an integer from 0 to 10"
    ),
    "baseline-insert-number-negative": (
        "INSERT INTO baseline VALUES (1, -1, NULL, NULL, 'r', 't0')", "baseline: number is not an integer from 0 to 10"
    ),
    "baseline-insert-number-fraction": (
        "INSERT INTO baseline VALUES (1, 2.5, NULL, NULL, 'r', 't0')", "baseline: number is not an integer from 0 to 10"
    ),
    "baseline-insert-number-text": (
        "INSERT INTO baseline VALUES (1, 'two', NULL, NULL, 'r', 't0')", "baseline: number is not an integer from 0 to 10"
    ),
    "baseline-insert-number-huge": (
        "INSERT INTO baseline VALUES (1, 99999999999999999999, NULL, NULL, 'r', 't0')",
        "baseline: number is not an integer from 0 to 10",
    ),
    "baseline-task-locked-baseline": (
        "INSERT INTO baseline_task VALUES (1, 1, 3, 's', 'f', 'd', 'w', '5', x'02')",
        "baseline_task: its baseline is missing or locked",
    ),
    "baseline-task-missing-baseline": (
        "INSERT INTO baseline_task VALUES (1, 5, 3, 's', 'f', 'd', 'w', '5', x'02')",
        "baseline_task: its baseline is missing or locked",
    ),
    "baseline-task-missing-task": (
        "INSERT INTO baseline_task VALUES (1, 0, 9, 's', 'f', 'd', 'w', '5', x'02')",
        "baseline_task: its task does not exist",
    ),
}


@pytest.mark.parametrize(("sql", "message"), REFUSED.values(), ids=REFUSED.keys())
def test_refuses_writes_to_protected_rows(db, sql, message):
    assert re.search(message, refused(db, sql))


REFUSED_DURING_IMPORT = {
    # Finishing the import together with a change to any other column.
    "project-finish-and-id": ("UPDATE project SET import_finished_at = 't9', id = 2", "project: only import_finished_at"),
    "project-finish-and-name": (
        "UPDATE project SET import_finished_at = 't9', name = 'other'", "project: only import_finished_at"
    ),
    "project-finish-and-hash": (
        "UPDATE project SET import_finished_at = 't9', source_sha256 = 'other'", "project: only import_finished_at"
    ),
    "project-finish-and-bytes": (
        "UPDATE project SET import_finished_at = 't9', source_xml = x'00'", "project: only import_finished_at"
    ),
    "project-finish-and-imported-at": (
        "UPDATE project SET import_finished_at = 't9', imported_at = 't9'", "project: only import_finished_at"
    ),
    "project-name": ("UPDATE project SET name = 'other'", "project: only import_finished_at"),
    "project-finish-left-unset": ("UPDATE project SET import_finished_at = NULL", "project: only import_finished_at"),
    "project-replace": (
        f"INSERT OR REPLACE INTO project VALUES {ROW['project']}", "project: this database already holds a project"
    ),
    "project-second": (
        "INSERT INTO project VALUES (2, 'Q', 'hash', x'00', 't0', NULL)", "project: this database already holds a project"
    ),
    "task-replace": (f"INSERT OR REPLACE INTO task VALUES {ROW['task']}", "task: already exists"),
    "task-missing-project": (
        "INSERT INTO task VALUES (2, 1, 'T', '1', 1, 0, 0, 's', 'f', 'd', 'w', x'01')",
        "task: added only while its project is being imported",
    ),
    "baseline-replace-locked": (f"INSERT OR REPLACE INTO baseline VALUES {ROW['baseline']}", "baseline: number already exists"),
    "baseline-task-replace-unlocked": (
        f"INSERT OR REPLACE INTO baseline_task VALUES {ROW['baseline_task']}", "baseline_task: already exists"
    ),
}


@pytest.mark.parametrize(("sql", "message"), REFUSED_DURING_IMPORT.values(), ids=REFUSED_DURING_IMPORT.keys())
def test_refuses_writes_to_protected_rows_during_an_import(importing_db, sql, message):
    assert re.search(message, refused(importing_db, sql))


# Each would delete the row that holds rowid 1 or 2 (a locked baseline, a
# task, a project) through REPLACE without firing its DELETE trigger. They
# run during an import, so no parent trigger stands in the way: only
# WITHOUT ROWID refuses them.
ROWID_INSERTS = {
    "project": "INSERT OR REPLACE INTO project({rowid}, id, source_sha256, source_xml, imported_at) "
    "VALUES (1, 2, 'other-hash', x'00', 't0')",
    "task": "INSERT OR REPLACE INTO task({rowid}, project_id, uid, is_summary, is_milestone, plan_c14n) "
    "VALUES (1, 1, 9, 0, 0, x'09')",
    "baseline": "INSERT OR REPLACE INTO baseline({rowid}, project_id, number, created_at) VALUES (2, 1, 7, 't0')",
    "baseline_task": "INSERT OR REPLACE INTO baseline_task({rowid}, project_id, number, task_uid, xml_c14n) "
    "VALUES (1, 1, 0, 3, x'09')",
}
ROWID_UPDATES = {
    "project": "UPDATE OR REPLACE project SET {rowid} = 1, import_finished_at = 't9'",
    "task": "UPDATE OR REPLACE task SET {rowid} = 1 WHERE uid = 2",
    # The one allowed baseline update, plus a rowid that belongs to locked Baseline 1.
    "baseline": "UPDATE OR REPLACE baseline SET {rowid} = 2, locked_at = 't9', locked_by = 'x' WHERE number = 0",
    "baseline_task": "UPDATE OR REPLACE baseline_task SET {rowid} = 1 WHERE number = 0 AND task_uid = 2",
}
ROWID_NAMES = ("rowid", "_rowid_", "oid")


@pytest.mark.parametrize("rowid", ROWID_NAMES)
@pytest.mark.parametrize("table", PROTECTED)
def test_insert_or_replace_with_an_explicit_rowid_is_refused(importing_db, table, rowid):
    message = refused(importing_db, ROWID_INSERTS[table].format(rowid=rowid))
    assert message == f"table {table} has no column named {rowid}"


@pytest.mark.parametrize("rowid", ROWID_NAMES)
@pytest.mark.parametrize("table", PROTECTED)
def test_update_or_replace_setting_the_rowid_is_refused(importing_db, table, rowid):
    assert refused(importing_db, ROWID_UPDATES[table].format(rowid=rowid)) == f"no such column: {rowid}"


def test_protected_tables_are_without_rowid(db):
    with closing(sqlite3.connect(db)) as conn:
        wr = {name: without_rowid for _, name, _, _, without_rowid, _ in conn.execute("PRAGMA table_list")}
    assert {table: wr[table] for table in PROTECTED} == dict.fromkeys(PROTECTED, 1)


def test_protected_tables_have_no_unique_index_besides_the_primary_key(db):
    # A UNIQUE constraint shows origin "u" and a CREATE UNIQUE INDEX (partial
    # or on an expression too) origin "c"; REPLACE deletes through either.
    with closing(sqlite3.connect(db)) as conn:
        for table in PROTECTED:
            unique = [(name, origin) for _, name, is_unique, origin, _ in conn.execute(f"PRAGMA index_list({table})") if is_unique]
            assert [origin for _, origin in unique] == ["pk"], (table, unique)


def test_allows_the_one_time_baseline_lock_then_no_new_rows(db):
    allowed(db, "UPDATE baseline SET locked_at = 't9', locked_by = 'baseline lock' WHERE number = 0")
    assert snapshot(db)["baseline"][0] == (1, 0, "t9", "baseline lock", None, "t0")
    message = refused(db, "INSERT INTO baseline_task VALUES (1, 0, 3, 's', 'f', 'd', 'w', '5', x'02')")
    assert message == "baseline_task: its baseline is missing or locked"


def test_allows_finishing_an_import_once_then_no_new_tasks(importing_db):
    allowed(importing_db, "INSERT INTO task VALUES (1, 4, 'T', '1', 1, 0, 0, 's', 'f', 'd', 'w', x'01')")
    allowed(importing_db, "UPDATE project SET import_finished_at = 't9'")
    assert snapshot(importing_db)["project"][0][-1] == "t9"
    message = refused(importing_db, "INSERT INTO task VALUES (1, 5, 'T', '1', 1, 0, 0, 's', 'f', 'd', 'w', x'01')")
    assert message == "task: added only while its project is being imported"


def test_allows_a_new_baseline_with_rows_until_it_is_locked(db):
    # How rebaseline appends: the baseline, its rows, then the lock.
    allowed(db, "INSERT INTO baseline VALUES (1, 2, NULL, NULL, 'reason', 't9')")
    allowed(db, "INSERT INTO baseline_task VALUES (1, 2, 3, 's', 'f', 'd', 'w', NULL, x'02')")
    allowed(db, "UPDATE baseline SET locked_at = 't9', locked_by = 'rebaseline' WHERE number = 2")
    assert refused(db, "INSERT INTO baseline_task VALUES (1, 2, 1, 's', 'f', 'd', 'w', NULL, x'02')") == (
        "baseline_task: its baseline is missing or locked"
    )


# open_db and migrate


def test_open_db_sets_up_a_new_file_with_sqlite_defaults(tmp_path):
    path = tmp_path / "new.db"
    with closing(open_db(path, create=True)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
        assert conn.execute("PRAGMA recursive_triggers").fetchone() == (0,)
        tables = {name for (name,) in conn.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")}
    assert set(PROTECTED) <= tables
    with closing(open_db(path)) as conn:  # opening again changes nothing
        assert conn.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)


def test_open_db_refuses_a_missing_file_without_creating_it(tmp_path):
    path = tmp_path / "typo.db"
    with pytest.raises(StoreError, match="can't open database"):
        open_db(path)
    assert not path.exists()


def test_open_db_refuses_a_file_that_is_not_a_database(tmp_path):
    path = tmp_path / "notes.db"
    path.write_bytes(b"not a database, but long enough to have a header page" * 10)
    with pytest.raises(StoreError, match="file is not a database"):
        open_db(path)


def test_open_db_refuses_another_programs_database(tmp_path):
    path = tmp_path / "other.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE notes (text)")
    with pytest.raises(StoreError, match="not a schedule-guard database"):
        open_db(path)


def test_open_db_refuses_another_schema_version(tmp_path):
    path = tmp_path / "future.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    with pytest.raises(StoreError, match=f"schema version {SCHEMA_VERSION + 1}"):
        open_db(path)


def test_transaction_rolls_back_when_the_block_raises(tmp_path):
    path = tmp_path / "store.db"
    with closing(open_db(path, create=True)) as conn:
        with pytest.raises(RuntimeError), transaction(conn):
            conn.execute("INSERT INTO project VALUES (1, 'P', 'hash', x'00', 't0', NULL)")
            raise RuntimeError("stop")
        assert conn.execute("SELECT count(*) FROM project").fetchone() == (0,)
        assert not conn.in_transaction
