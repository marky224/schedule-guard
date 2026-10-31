"""Review: list conflicts, and dismiss one with a note; dismissing never applies the update."""

import ast
import json
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from schedule_guard.clock import fixed_clock
from schedule_guard.review import ReviewRefused, dismiss, list_conflicts
from schedule_guard.sources.file import FileSource
from schedule_guard.store.db import open_db
from schedule_guard.sync import sync

CLOCK = fixed_clock(datetime(2026, 10, 30, 17, 0, tzinfo=UTC))
LATER = datetime(2026, 10, 30, 21, 0, tzinfo=UTC)
SRC = Path(__file__).resolve().parent.parent / "src" / "schedule_guard"


@pytest.fixture
def conflicted(fixture_db, tmp_path):
    """A database whose one update was a field_not_allowed conflict."""
    item = {
        "source_item_id": "u-001", "item_version": 1, "task_uid": 3, "made_against_version": 0,
        "fields": {"percent_complete": 25, "Proposed Finish": "2027-01-04T17:00:00"},
        "submitted_at": "2026-10-26T16:00:00-05:00", "submitted_by": "Site lead (demo)",
    }
    (tmp_path / "updates.jsonl").write_text(json.dumps(item) + "\n")
    assert sync(fixture_db, FileSource(tmp_path / "updates.jsonl"), clock=CLOCK).conflicts == 1
    return fixture_db


def progress_and_outcomes(conn):
    return (conn.execute("SELECT * FROM task_progress ORDER BY task_uid").fetchall(),
            conn.execute("SELECT * FROM outcome").fetchall())


def test_list_shows_open_conflicts_with_their_update(conflicted):
    with closing(open_db(conflicted)) as conn:
        (c,) = list_conflicts(conn)
    assert (c.id, c.status, c.reason, c.task_uid, c.source, c.source_item_id, c.item_version, c.submitted_by) == (
        1, "open", "field_not_allowed", 3, "file", "u-001", "1", "Site lead (demo)"
    )
    assert "'Proposed Finish'" in c.detail


def test_dismiss_marks_it_dismissed_with_the_note_and_applies_nothing(conflicted):
    with closing(open_db(conflicted)) as conn:
        before = progress_and_outcomes(conn)
        dismiss(conn, 1, "Finish dates go through the scheduler", now=LATER)
        assert list_conflicts(conn) == []
        (c,) = list_conflicts(conn, include_dismissed=True)
        assert (c.status, c.reviewed_at, c.note) == ("dismissed", "2026-10-30T21:00:00+00:00",
                                                     "Finish dates go through the scheduler")
        assert progress_and_outcomes(conn) == before


@pytest.mark.parametrize(
    ("conflict_id", "note", "message"),
    [
        (1, None, "needs --note"), (1, "   ", "needs --note"), (2, "x", "there is no conflict 2"),
        (0, "x", "there is no conflict 0"), (2**63, "x", "there is no conflict"),
        (1, "ok \udcff", "the note must be UTF-8 text"),  # the argument a shell passes for b"ok \xff"
    ],
    ids=["no-note", "blank-note", "unknown", "zero", "too-big-for-sqlite", "note-not-utf-8"],
)
def test_dismiss_refuses_without_a_note_or_an_open_conflict(conflicted, conflict_id, note, message):
    with closing(open_db(conflicted)) as conn:
        with pytest.raises(ReviewRefused, match=message):
            dismiss(conn, conflict_id, note, now=LATER)
        assert list_conflicts(conn)[0].status == "open"


def test_a_dismissed_conflict_stays_dismissed(conflicted):
    with closing(open_db(conflicted)) as conn:
        dismiss(conn, 1, "first", now=LATER)
        with pytest.raises(ReviewRefused, match="conflict 1 is already dismissed"):
            dismiss(conn, 1, "second", now=LATER)
        assert list_conflicts(conn, include_dismissed=True)[0].note == "first"


def imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def uses(path: Path, module: str) -> bool:
    return any(name == module or name.startswith(f"{module}.") for name in imports(path))


def test_review_never_imports_the_applier_and_sync_never_imports_the_baseline_commands():
    assert not uses(SRC / "review.py", "schedule_guard.progress")
    assert not uses(SRC / "review.py", "schedule_guard.guard")
    assert uses(SRC / "sync.py", "schedule_guard.progress")  # the check can see an import
    for path in [SRC / "sync.py", SRC / "review.py", *sorted((SRC / "sources").glob("*.py"))]:
        assert not uses(path, "schedule_guard.baselines"), path
