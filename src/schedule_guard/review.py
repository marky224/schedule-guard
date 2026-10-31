"""The reviewer's two actions on conflicts: list them, and dismiss one with a note.

Dismissing is the only review action (Q5). It never applies the update, and
this module doesn't import the applier: the field resubmits against the
task's current version instead.
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from schedule_guard.store.db import transaction

_MAX_SQLITE_INTEGER = 2**63 - 1


class ReviewRefused(Exception):
    """Nothing was dismissed."""


@dataclass(frozen=True, slots=True)
class Conflict:
    id: int
    status: str
    reason: str
    detail: str | None
    task_uid: int | None
    source: str
    source_item_id: str
    item_version: str
    submitted_by: str | None
    reviewed_at: str | None
    note: str | None


def list_conflicts(conn: sqlite3.Connection, *, include_dismissed: bool = False) -> list[Conflict]:
    where = "" if include_dismissed else " WHERE c.status = 'open'"
    rows = conn.execute(
        "SELECT c.id, c.status, c.reason_code, c.detail, su.task_uid, su.source, su.source_item_id, su.item_version,"
        f" su.submitted_by, c.reviewed_at, c.note FROM conflict c JOIN source_update su ON su.id = c.update_id{where}"
        " ORDER BY c.id"
    )
    return [Conflict(*row) for row in rows]


def dismiss(conn: sqlite3.Connection, conflict_id: int, note: str | None, *, now: datetime) -> None:
    """Mark an open conflict dismissed, with the reviewer's note. The update stays unapplied."""
    if not note or not note.strip():
        raise ReviewRefused('dismissing a conflict needs --note "..."')
    if not 1 <= conflict_id <= _MAX_SQLITE_INTEGER:
        raise ReviewRefused(f"there is no conflict {conflict_id}")
    with transaction(conn):
        row = conn.execute("SELECT status FROM conflict WHERE id = ?", (conflict_id,)).fetchone()
        if row is None:
            raise ReviewRefused(f"there is no conflict {conflict_id}")
        if row[0] != "open":
            raise ReviewRefused(f"conflict {conflict_id} is already dismissed")
        conn.execute(
            "UPDATE conflict SET status = 'dismissed', reviewed_at = ?, note = ? WHERE id = ?",
            (now.astimezone(UTC).isoformat(timespec="seconds"), note, conflict_id),
        )
