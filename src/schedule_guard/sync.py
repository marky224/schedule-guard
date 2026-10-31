"""Sync: run every item a source gives through the guard, exactly once (PLAN.md section 5).

A run takes the lock file next to the database, so a second run at the
same time (the timer and a manual `sync now`) is refused; the OS drops the
lock if the process dies. It records a sync_run, then captures what the
source gave, in one transaction: each item's source_update, stored once per
idempotency key (source, source_item_id, item_version), and a run_item. A
key that comes twice in one run is captured once and counted a duplicate.

Then each captured item, in (submitted_at, source_item_id) order, gets its
own BEGIN IMMEDIATE transaction that looks up the key's outcome (G0),
decides with the guard, applies an applied update through
progress.apply_progress and stores the outcome, and the conflict for a
conflict. A key already decided is a duplicate and writes nothing. An item
its source couldn't decode or key is a `malformed` conflict.

If a run stops halfway, `rerun` captures the same items again and
processes them the same way: decided keys come back as duplicates, the rest
are decided as the first run would have. A run killed partway is left
`running`; the next run to start holds the lock, so it knows no other run
is alive and marks that one `interrupted`. Sync never imports the baseline
commands.
"""

import fcntl
import json
import os
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from schedule_guard import progress
from schedule_guard.clock import Clock, system_clock
from schedule_guard.guard import Outcome, Progress, Update, decide, target_uid
from schedule_guard.sources.base import SourceAdapter, SourceItem, decode_json
from schedule_guard.store.db import open_db, transaction

_MAX_SQLITE_INTEGER = 2**63 - 1
_EARLIEST = datetime.min.replace(tzinfo=UTC)  # where an item without a usable submitted_at sorts


class SyncRefused(Exception):
    """The run didn't start: the lock is taken, there is no project, the source isn't text, or nothing to rerun."""


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: str
    trigger: str
    rerun_of: str | None
    source: str
    started_at: str
    finished_at: str | None
    status: str
    seen: int
    applied: int
    conflicts: int
    duplicates: int
    error: str | None


def sync(db_path: str | Path, source: SourceAdapter, *, trigger: str = "now", clock: Clock = system_clock) -> RunResult:
    """Read the source and decide every item it gives that has no outcome yet."""
    if trigger not in ("now", "daily"):
        raise ValueError(f"a sync run is triggered now or daily, not {trigger!r}")
    try:
        source.spec.encode("utf-8")  # a run records it, and SQLite stores text as UTF-8
    except UnicodeEncodeError:  # a file name that isn't UTF-8 decodes to lone surrogates
        raise SyncRefused(f"the source must be UTF-8 text, not {source.spec!r}") from None

    def capture(conn: sqlite3.Connection, run_id: str) -> None:
        items = source.read()
        with transaction(conn):
            _capture(conn, run_id, [_store(conn, source.name, item, run_id) for item in items])

    return _run(db_path, trigger=trigger, rerun_of=None, source_spec=source.spec, capture=capture, clock=clock)


def rerun(db_path: str | Path, run_id: str, *, clock: Clock = system_clock) -> RunResult:
    """Process again the items run `run_id` captured, as a new run; refused if it captured none."""
    with closing(open_db(db_path)) as conn:
        row = conn.execute("SELECT source FROM sync_run WHERE id = ?", (run_id,)).fetchone()
        captured = conn.execute("SELECT 1 FROM run_item WHERE run_id = ? LIMIT 1", (run_id,)).fetchone()
    if row is None:
        raise SyncRefused(f"there is no run {run_id!r}")
    if captured is None:  # it failed before capturing, or its source gave nothing
        raise SyncRefused(f"run {run_id} captured nothing, so there is nothing to rerun")

    def capture(conn: sqlite3.Connection, new_id: str) -> None:
        with transaction(conn):
            update_ids = [u for (u,) in conn.execute("SELECT update_id FROM run_item WHERE run_id = ?", (run_id,))]
            _capture(conn, new_id, update_ids)

    return _run(db_path, trigger="rerun", rerun_of=run_id, source_spec=row[0], capture=capture, clock=clock)


def list_runs(conn: sqlite3.Connection) -> list[RunResult]:
    return [RunResult(*row) for row in conn.execute(f"SELECT {_RUN_COLUMNS} FROM sync_run ORDER BY started_at, rowid")]


_RUN_COLUMNS = (
    "id, trigger, rerun_of, source, started_at, finished_at, status, seen, applied, conflicts, duplicates, error"
)


@contextmanager
def run_lock(db_path: str | Path) -> Iterator[None]:
    """Hold the lock file next to the database for one run, or refuse if another run holds it."""
    path = Path(db_path)
    lock = path.with_name(f"{path.name}.lock")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncRefused(f"another sync run is in progress (it holds {lock.name})") from None
        yield
    finally:
        os.close(fd)  # closing the file drops the lock


def _run(
    db_path: str | Path, *, trigger: str, rerun_of: str | None, source_spec: str,
    capture: Callable[[sqlite3.Connection, str], None], clock: Clock,
) -> RunResult:
    with closing(open_db(db_path)) as conn, run_lock(db_path):
        if conn.execute("SELECT 1 FROM project WHERE import_finished_at IS NOT NULL").fetchone() is None:
            raise SyncRefused("no project has been imported into this database")
        run_id = str(uuid.uuid4())
        with transaction(conn):
            # This run holds the lock, so no other run is alive: one still running was killed.
            conn.execute("UPDATE sync_run SET status = 'interrupted' WHERE status = 'running'")
            conn.execute(
                "INSERT INTO sync_run (id, trigger, rerun_of, source, started_at, status) VALUES (?, ?, ?, ?, ?, 'running')",
                (run_id, trigger, rerun_of, source_spec, _stamp(clock)),
            )
        try:
            capture(conn, run_id)
            for update_id in _in_order(conn, run_id):
                _process(conn, run_id, update_id, clock)
        except Exception as exc:  # the run fails; a BaseException such as a kill leaves it running
            status, error = "failed", f"{type(exc).__name__}: {exc}"
        else:
            status, error = "ok", None
        with transaction(conn):
            conn.execute(
                "UPDATE sync_run SET status = ?, error = ?, finished_at = ? WHERE id = ?",
                (status, error, _stamp(clock), run_id),
            )
        return RunResult(*conn.execute(f"SELECT {_RUN_COLUMNS} FROM sync_run WHERE id = ?", (run_id,)).fetchone())


def _store(conn: sqlite3.Connection, source: str, item: SourceItem, run_id: str) -> int:
    """The source_update row for the item's key, stored now if the key is new."""
    key = (source, item.source_item_id, item.item_version)
    row = conn.execute(
        "SELECT id FROM source_update WHERE source = ? AND source_item_id = ? AND item_version = ?", key
    ).fetchone()
    if row is not None:
        return row[0]
    payload = item.payload if isinstance(item.payload, dict) else {}
    fields = payload.get("fields")
    cursor = conn.execute(
        "INSERT INTO source_update (source, source_item_id, item_version, task_uid, made_against_version, fields_json,"
        " submitted_at, submitted_by, raw_json, problem, first_seen_run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (*key, _integer(payload.get("task_uid")), _integer(payload.get("made_against_version")),
         json.dumps(fields) if isinstance(fields, dict) else None, _text(payload.get("submitted_at")),
         _text(payload.get("submitted_by")), item.raw, item.problem, run_id),
    )
    return cursor.lastrowid


def _capture(conn: sqlite3.Connection, run_id: str, update_ids: list[int]) -> None:
    """Record what the run captured; a key that came more than once counts as a duplicate."""
    captured: set[int] = set()
    for update_id in update_ids:
        if update_id not in captured:
            captured.add(update_id)
            conn.execute("INSERT INTO run_item (run_id, update_id) VALUES (?, ?)", (run_id, update_id))
    conn.execute(
        "UPDATE sync_run SET seen = ?, duplicates = ? WHERE id = ?",
        (len(update_ids), len(update_ids) - len(captured), run_id),
    )


def _in_order(conn: sqlite3.Connection, run_id: str) -> list[int]:
    rows = conn.execute(
        "SELECT su.id, su.submitted_at, su.source_item_id FROM run_item ri"
        " JOIN source_update su ON su.id = ri.update_id WHERE ri.run_id = ?",
        (run_id,),
    ).fetchall()
    return [update_id for update_id, *_ in sorted(rows, key=lambda r: (_instant(r[1]), r[2], r[0]))]


def _process(conn: sqlite3.Connection, run_id: str, update_id: int, clock: Clock) -> None:
    """Decide one captured item and store what was decided, all in one transaction."""
    with transaction(conn):
        decided = conn.execute("SELECT 1 FROM outcome WHERE update_id = ?", (update_id,)).fetchone() is not None
        source, raw, problem = conn.execute(
            "SELECT source, raw_json, problem FROM source_update WHERE id = ?", (update_id,)
        ).fetchone()
        if problem is not None:
            outcome = Outcome("duplicate") if decided else Outcome("conflict", "malformed", problem)
        else:
            update = Update(source, decode_json(raw))
            uid = target_uid(update)
            task = progress.load_task(conn, uid) if uid is not None else None
            outcome = decide(update, task, already_decided=decided, clock=clock)
        if outcome.status == "duplicate":
            conn.execute("UPDATE sync_run SET duplicates = duplicates + 1 WHERE id = ?", (run_id,))
            return
        if outcome.status == "applied":
            progress.apply_progress(conn, outcome, update_id=update_id)
        conn.execute(
            "INSERT INTO outcome (update_id, status, reason_code, detail, run_id, decided_at, before_json, after_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (update_id, outcome.status, outcome.reason, outcome.detail, run_id, _stamp(clock),
             _progress_json(outcome.before), _progress_json(outcome.after)),
        )
        if outcome.status == "conflict":
            conn.execute(
                "INSERT INTO conflict (update_id, reason_code, detail, status) VALUES (?, ?, ?, 'open')",
                (update_id, outcome.reason, outcome.detail),
            )
            conn.execute("UPDATE sync_run SET conflicts = conflicts + 1 WHERE id = ?", (run_id,))
        else:
            conn.execute("UPDATE sync_run SET applied = applied + 1 WHERE id = ?", (run_id,))


def _instant(text: str | None) -> datetime:
    try:
        instant = datetime.fromisoformat(text)
        return instant.astimezone(UTC) if instant.utcoffset() is not None else _EARLIEST
    except (TypeError, ValueError, OverflowError):
        return _EARLIEST


def _integer(value: object) -> int | None:
    ok = isinstance(value, int) and not isinstance(value, bool) and -_MAX_SQLITE_INTEGER - 1 <= value <= _MAX_SQLITE_INTEGER
    return value if ok else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _progress_json(value: Progress | None) -> str | None:
    return None if value is None else json.dumps(asdict(value))


def _stamp(clock: Clock) -> str:
    return clock().astimezone(UTC).isoformat(timespec="seconds")
