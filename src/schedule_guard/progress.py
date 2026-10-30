"""Read a task's state for the guard, and write an applied outcome.

This is the only code that writes task_progress after import. It reads
task and task_progress and writes task_progress only; the schema refuses
any write to task, baseline or baseline_task anyway. Sync (M4) calls both
inside the transaction that also stores the update's outcome.
"""

import sqlite3

from schedule_guard.guard import Outcome, Progress, TaskState
from schedule_guard.importer import PROJECT_ID

_MAX_SQLITE_INTEGER = 2**63 - 1


class ProgressError(Exception):
    """The task is no longer at the version the update was made against; nothing was written."""


def load_task(conn: sqlite3.Connection, uid: int) -> TaskState | None:
    """The task's summary flag and progress, or None if the project has no such task."""
    if not 0 <= uid <= _MAX_SQLITE_INTEGER:  # MSPDI UIDs are never negative, and SQLite can't bind more
        return None
    row = conn.execute(
        "SELECT t.is_summary, p.version, p.percent_complete, p.actual_start, p.actual_finish, p.remaining_work,"
        " p.notes FROM task t JOIN task_progress p ON p.project_id = t.project_id AND p.task_uid = t.uid"
        " WHERE t.project_id = ? AND t.uid = ?",
        (PROJECT_ID, uid),
    ).fetchone()
    if row is None:
        return None
    is_summary, *progress = row
    return TaskState(uid=uid, is_summary=bool(is_summary), progress=Progress(*progress))


def apply_progress(conn: sqlite3.Connection, outcome: Outcome) -> None:
    """Write an applied outcome's progress, if the task is still at the version it was made against.

    The guard already checked the version (G4); checking it again in the
    write keeps a stale update from ever being applied.
    """
    after = outcome.after
    cursor = conn.execute(
        "UPDATE task_progress SET version = ?, percent_complete = ?, actual_start = ?, actual_finish = ?,"
        " remaining_work = ?, notes = ? WHERE project_id = ? AND task_uid = ? AND version = ?",
        (after.version, after.percent_complete, after.actual_start, after.actual_finish, after.remaining_work,
         after.notes, PROJECT_ID, outcome.task_uid, outcome.made_against_version),
    )
    if cursor.rowcount != 1:
        raise ProgressError(f"task UID {outcome.task_uid} is no longer at version {outcome.made_against_version}")
