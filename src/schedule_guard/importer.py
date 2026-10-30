"""`schedule-guard import`: store an MSPDI file in an empty database.

The whole file is read and parsed before anything is written. Then the
project (raw bytes and their SHA-256), the master schedule and every
baseline go in one transaction that ends by setting
`project.import_finished_at`; after that the schema refuses new tasks. A
database holds one project, so a second import is refused (Q14): a changed
schedule goes into a fresh database. Imported baselines stay unlocked until
`baseline lock` (Q24).
"""

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from schedule_guard.mspdi.parse import parse_mspdi, read_mspdi_bytes
from schedule_guard.store.db import transaction

PROJECT_ID = 1


class ImportRefused(Exception):
    """The database already holds a project."""


@dataclass(frozen=True, slots=True)
class ImportResult:
    name: str | None
    tasks: int
    baselines: tuple[int, ...]
    source_sha256: str


def import_file(conn: sqlite3.Connection, path: str | Path, *, now: datetime) -> ImportResult:
    """Store the MSPDI file at `path`; `now` stamps imported_at and the baselines' created_at."""
    data = read_mspdi_bytes(path)
    project = parse_mspdi(data)
    name = project.title or project.name
    sha256 = hashlib.sha256(data).hexdigest()
    stamp = now.isoformat(timespec="seconds")
    numbers = tuple(sorted({b.number for t in project.tasks for b in t.baselines}))
    with transaction(conn):
        if conn.execute("SELECT 1 FROM project").fetchone():
            raise ImportRefused("this database already holds a project; import a changed schedule into a new database")
        conn.execute(
            "INSERT INTO project (id, name, source_sha256, source_xml, imported_at) VALUES (?, ?, ?, ?, ?)",
            (PROJECT_ID, name, sha256, data, stamp),
        )
        conn.executemany(
            "INSERT INTO task (project_id, uid, name, wbs, outline_level, is_summary, is_milestone, planned_start,"
            " planned_finish, planned_duration, planned_work, plan_c14n) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (PROJECT_ID, t.uid, t.name, t.wbs, t.outline_level, int(t.is_summary), int(t.is_milestone),
                 t.planned_start, t.planned_finish, t.planned_duration, t.planned_work, t.plan_c14n)
                for t in project.tasks
            ],
        )
        conn.executemany(
            "INSERT INTO baseline (project_id, number, created_at) VALUES (?, ?, ?)",
            [(PROJECT_ID, number, stamp) for number in numbers],
        )
        conn.executemany(
            "INSERT INTO baseline_task (project_id, number, task_uid, start, finish, duration, work, cost, xml_c14n)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (PROJECT_ID, b.number, t.uid, b.start, b.finish, b.duration, b.work, b.cost, b.xml_c14n)
                for t in project.tasks
                for b in t.baselines
            ],
        )
        conn.execute("UPDATE project SET import_finished_at = ? WHERE id = ?", (stamp, PROJECT_ID))
    return ImportResult(name=name, tasks=len(project.tasks), baselines=numbers, source_sha256=sha256)
