"""Run updates through the guard and the applier the way sync will, and read what P1-P5 compare.

Sync (M4) adds the outcome table and the source items; until then each
item comes with its idempotency key, and the keys already decided are kept
in memory. An item may say it was made against CURRENT, the version its
task is at when the item comes up, as a crew that just read it would, or
NEXT, one past it, as a stale copy would be.
"""

import hashlib
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, replace

from schedule_guard import progress
from schedule_guard.clock import Clock
from schedule_guard.guard import Outcome, Update, decide, target_uid
from schedule_guard.store.db import transaction

Key = tuple[str, object, object]  # (source, source_item_id, item_version)

PROGRESS = "task_uid, version, percent_complete, actual_start, actual_finish, remaining_work, notes"
CURRENT, NEXT = object(), object()


@dataclass(frozen=True)
class Step:
    key: Key
    update: Update
    outcome: Outcome
    version_before: int | None  # the task's version when the update was decided
    progress_before: dict
    progress_after: dict


def run(conn: sqlite3.Connection, items: Iterable[tuple[Key, Update]], clock: Clock) -> list[Step]:
    """Decide and apply each item in its own transaction, as sync will."""
    decided: set[Key] = set()
    steps = []
    for key, update in items:
        with transaction(conn):
            before = progress_rows(conn)
            uid = target_uid(update)
            task = progress.load_task(conn, uid) if uid is not None else None
            update = _resolve(update, task)
            outcome = decide(update, task, already_decided=key in decided, clock=clock)
            if outcome.status == "applied":
                progress.apply_progress(conn, outcome)
            decided.add(key)
        version = task.progress.version if task else None
        steps.append(Step(key, update, outcome, version, before, progress_rows(conn)))
    return steps


def _resolve(update: Update, task) -> Update:
    payload = update.payload
    made_against = payload.get("made_against_version") if isinstance(payload, dict) else None
    if made_against is CURRENT or made_against is NEXT:
        version = (task.progress.version if task else 0) + (made_against is NEXT)
        return replace(update, payload={**payload, "made_against_version": version})
    return update


def progress_rows(conn: sqlite3.Connection) -> dict:
    return {row[0]: row[1:] for row in conn.execute(f"SELECT {PROGRESS} FROM task_progress ORDER BY task_uid")}


def baselines(conn: sqlite3.Connection) -> tuple[bytes, list]:
    """P1: the baseline bytes plus the stored file, and every column of every baseline row."""
    data = b"".join(xml for (xml,) in conn.execute(
        "SELECT xml_c14n FROM baseline_task ORDER BY project_id, number, task_uid"
    ))
    data += conn.execute("SELECT source_xml FROM project").fetchone()[0]
    rows = conn.execute("SELECT * FROM baseline ORDER BY project_id, number").fetchall()
    rows += conn.execute("SELECT * FROM baseline_task ORDER BY project_id, number, task_uid").fetchall()
    return data, rows


def master_schedule(conn: sqlite3.Connection) -> tuple[bytes, list]:
    """P2: the plan bytes, and every column of every task row."""
    data = b"".join(plan for (plan,) in conn.execute("SELECT plan_c14n FROM task ORDER BY project_id, uid"))
    return data, conn.execute("SELECT * FROM task ORDER BY project_id, uid").fetchall()


def assert_unchanged(before: tuple[bytes, list], after: tuple[bytes, list], what: str) -> None:
    assert hashlib.sha256(after[0]).hexdigest() == hashlib.sha256(before[0]).hexdigest(), f"{what}: bytes changed"
    assert after[0] == before[0], f"{what}: bytes changed"
    assert after[1] == before[1], f"{what}: a row changed"
