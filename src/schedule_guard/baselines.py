"""`baseline lock` and `rebaseline`: the only commands that change baselines.

Baselines are append-only (Q7). `lock_baseline` locks one once; after that
its rows can't change and it gets no new rows. `rebaseline` appends the next
baseline number, built from the current planned values, and locks it. Older
baselines never change. Sync, rerun and report code never import this module.
"""

import sqlite3
from datetime import datetime
from xml.etree import ElementTree as ET

from schedule_guard.mspdi.parse import MAX_BASELINE_NUMBER, NS, c14n
from schedule_guard.store.db import transaction


class BaselineRefused(Exception):
    """The baseline command was refused; nothing changed."""


def lock_baseline(conn: sqlite3.Connection, number: int = 0, *, now: datetime) -> None:
    with transaction(conn):
        project_id = _imported_project(conn)
        row = conn.execute(
            "SELECT locked_at FROM baseline WHERE project_id = ? AND number = ?", (project_id, number)
        ).fetchone()
        if row is None:
            raise BaselineRefused(f"there is no baseline {number}")
        if row[0] is not None:
            raise BaselineRefused(f"baseline {number} is already locked (at {row[0]})")
        conn.execute(
            "UPDATE baseline SET locked_at = ?, locked_by = 'baseline lock' WHERE project_id = ? AND number = ?",
            (now.isoformat(timespec="seconds"), project_id, number),
        )


def rebaseline(conn: sqlite3.Connection, *, confirm: bool, reason: str | None, now: datetime) -> int:
    """Append and lock the next baseline from the current planned values; return its number."""
    if not confirm:
        raise BaselineRefused("rebaseline adds a baseline that can never be removed; pass --confirm")
    if not reason or not reason.strip():
        raise BaselineRefused('rebaseline needs --reason "..."')
    stamp = now.isoformat(timespec="seconds")
    with transaction(conn):
        project_id = _imported_project(conn)
        (last,) = conn.execute("SELECT max(number) FROM baseline WHERE project_id = ?", (project_id,)).fetchone()
        number = 0 if last is None else last + 1
        if number > MAX_BASELINE_NUMBER:
            raise BaselineRefused(f"baseline {last} exists; numbers stop at {MAX_BASELINE_NUMBER}")
        conn.execute(
            "INSERT INTO baseline (project_id, number, reason, created_at) VALUES (?, ?, ?, ?)",
            (project_id, number, reason, stamp),
        )
        tasks = conn.execute(
            "SELECT uid, planned_start, planned_finish, planned_duration, planned_work FROM task"
            " WHERE project_id = ? ORDER BY uid",
            (project_id,),
        ).fetchall()
        # The rows go in before the lock: the schema refuses rows for a locked baseline.
        conn.executemany(
            "INSERT INTO baseline_task (project_id, number, task_uid, start, finish, duration, work, cost, xml_c14n)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)",
            [(project_id, number, *values, _baseline_bytes(number, *values[1:])) for values in tasks],
        )
        conn.execute(
            "UPDATE baseline SET locked_at = ?, locked_by = 'rebaseline' WHERE project_id = ? AND number = ?",
            (stamp, project_id, number),
        )
    return number


def _imported_project(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT id FROM project WHERE import_finished_at IS NOT NULL").fetchone()
    if row is None:
        raise BaselineRefused("no project has been imported into this database")
    return row[0]


def _baseline_bytes(number: int, start: str | None, finish: str | None, duration: str | None,
                    work: str | None) -> bytes:
    """C14N bytes of the <Baseline> MSPDI would hold for these values, like an imported one."""
    baseline = ET.Element(f"{{{NS}}}Baseline")
    for tag, value in (("Number", str(number)), ("Start", start), ("Finish", finish), ("Duration", duration),
                       ("Work", work)):
        if value is not None:
            ET.SubElement(baseline, f"{{{NS}}}{tag}").text = value
    return c14n(baseline, f"baseline {number}")
