"""Sync: one transaction per item, exactly once, in (submitted_at, source_item_id) order."""

import json
from contextlib import closing
from datetime import UTC, datetime

from schedule_guard.clock import fixed_clock
from schedule_guard.sources.file import FileSource
from schedule_guard.store.db import open_db
from schedule_guard.sync import rerun, sync

CLOCK = fixed_clock(datetime(2026, 10, 30, 17, 0, tzinfo=UTC))


def line(source_item_id, task_uid=3, made_against_version=0, fields=None, submitted_at="2026-10-26T16:00:00-05:00",
         item_version=1, submitted_by="Site lead (demo)") -> str:
    return json.dumps({
        "source_item_id": source_item_id, "item_version": item_version, "task_uid": task_uid,
        "made_against_version": made_against_version, "fields": fields or {"percent_complete": 25},
        "submitted_at": submitted_at, "submitted_by": submitted_by,
    })


def write(tmp_path, *lines, name="updates.jsonl"):
    path = tmp_path / name
    path.write_text("".join(f"{text}\n" for text in lines))
    return FileSource(path)


def test_a_run_decides_each_item_once_and_a_second_run_sees_duplicates(fixture_db, tmp_path):
    source = write(tmp_path, line("u-001"), line("u-002", fields={"planned_finish": "2027-01-01T17:00:00"}))
    first = sync(fixture_db, source, clock=CLOCK)
    assert (first.status, first.seen, first.applied, first.conflicts, first.duplicates) == ("ok", 2, 1, 1, 0)
    again = sync(fixture_db, source, clock=CLOCK)
    assert (again.status, again.seen, again.applied, again.conflicts, again.duplicates) == ("ok", 2, 0, 0, 2)
    repeat = rerun(fixture_db, first.run_id, clock=CLOCK)
    assert (repeat.trigger, repeat.rerun_of, repeat.seen, repeat.duplicates) == ("rerun", first.run_id, 2, 2)
    with closing(open_db(fixture_db)) as conn:
        assert conn.execute("SELECT status, reason_code FROM outcome ORDER BY update_id").fetchall() == [
            ("applied", None), ("conflict", "field_not_allowed"),
        ]
