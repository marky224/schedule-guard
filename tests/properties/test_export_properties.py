"""P7 (PLAN.md section 8): the export round-trip, on the real sync engine.

Each example runs batches of updates through sync on a fresh copy of the
imported fixture, adds 0-2 baselines with rebaseline, and exports. The file
is read back with plain ElementTree, not the exporter: every <Baseline> in
it is C14N-identical to the import (an imported one to its subtree in the
stored file, an added one to baseline_task.xml_c14n); the whole file,
without the five progress elements of each task and without the added
Baselines, is C14N-identical to the stored file without its progress
elements; and the progress elements read back equal task_progress. Then the
file imports into an empty project with the same task count, plan_c14n,
baseline xml_c14n and progress.
"""

from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree as ET

from harness import BATCHES, ENGINE, run_batches
from hypothesis import event, given
from hypothesis import strategies as st

from schedule_guard.baselines import rebaseline
from schedule_guard.importer import Source, import_source
from schedule_guard.mspdi.export import export_mspdi
from schedule_guard.mspdi.parse import NS, c14n, parse_mspdi
from schedule_guard.store.db import open_db

PROGRESS = ("PercentComplete", "ActualStart", "ActualFinish", "RemainingWork", "Notes")
COLUMNS = "percent_complete, actual_start, actual_finish, remaining_work, notes"
REIMPORTED = datetime(2026, 11, 8, 20, 0, tzinfo=UTC)
REBASELINES = st.integers(0, 2)  # how many baselines rebaseline adds before the export


def q(tag: str) -> str:
    return f"{{{NS}}}{tag}"


def tasks_of(root: ET.Element) -> list[ET.Element]:
    return root.find(q("Tasks")).findall(q("Task"))


def baselines_of(data: bytes) -> dict[tuple[int, int], bytes]:
    """(task UID, Baseline number): the C14N bytes of that <Baseline>, as import computes them."""
    return {
        (int(task.findtext(q("UID"))), int(b.findtext(q("Number")))): c14n(b, "Baseline")
        for task in tasks_of(ET.fromstring(data)) for b in task.findall(q("Baseline"))
    }


def without_progress(data: bytes, added: set[int]) -> str:
    """The whole file as C14N, without each task's progress elements and its Baselines numbered in `added`."""
    root = ET.fromstring(data)
    for task in tasks_of(root):
        for child in list(task):
            if child.tag in {q(tag) for tag in PROGRESS} or (
                child.tag == q("Baseline") and int(child.findtext(q("Number"))) in added
            ):
                task.remove(child)  # with the whitespace after it
    return ET.canonicalize(ET.tostring(root, encoding="unicode"))


def assert_round_trip(db: Path, data: bytes) -> None:
    """P7: the Baselines and every non-progress element as imported, and the progress as task_progress holds it."""
    with closing(open_db(db)) as conn:
        (source,) = conn.execute("SELECT source_xml FROM project").fetchone()
        added = {n for (n,) in conn.execute("SELECT number FROM baseline WHERE locked_by = 'rebaseline'")}
        stored = {(uid, n): xml for uid, n, xml in conn.execute("SELECT task_uid, number, xml_c14n FROM baseline_task")}
        progress = {uid: values for uid, *values in conn.execute(f"SELECT task_uid, {COLUMNS} FROM task_progress")}
    imported, exported = baselines_of(source), baselines_of(data)
    assert exported.keys() == stored.keys(), f"P7: Baselines {sorted(exported.keys() ^ stored.keys())} differ"
    for (uid, number), xml in exported.items():
        assert xml == stored[uid, number], f"P7: task {uid}'s Baseline {number} isn't the stored one"
        assert number in added or xml == imported.get((uid, number)), f"P7: task {uid}'s Baseline {number} changed"
    assert without_progress(data, added) == without_progress(source, set()), "P7: a non-progress element changed"
    read = {t.uid: [t.percent_complete, t.actual_start, t.actual_finish, t.remaining_work, t.notes]
            for t in parse_mspdi(data).tasks}
    assert read == progress, "P7: the progress written isn't task_progress"


def assert_reimports(db: Path, data: bytes) -> None:
    """The exported file imports into an empty project with the same tasks, baselines and progress."""
    again = db.with_name("reimported.db")
    with closing(open_db(again, create=True)) as conn:
        import_source(conn, Source(data=data, project=parse_mspdi(data)), now=REIMPORTED)
    with closing(open_db(db)) as first, closing(open_db(again)) as second:
        for what, sql in [
            ("task count", "SELECT count(*) FROM task"),
            ("plan_c14n", "SELECT uid, plan_c14n FROM task ORDER BY uid"),
            ("baseline xml_c14n", "SELECT number, task_uid, xml_c14n FROM baseline_task ORDER BY number, task_uid"),
            ("progress", f"SELECT task_uid, {COLUMNS} FROM task_progress ORDER BY task_uid"),
        ]:
            assert first.execute(sql).fetchall() == second.execute(sql).fetchall(), f"P7: the re-import's {what} differs"


@ENGINE
@given(BATCHES, REBASELINES)
def test_p7_the_export_keeps_the_master_schedule_and_baselines_and_re_imports(fresh_db, batches, rebaselines):
    db = fresh_db()
    results = run_batches(db, batches)
    assert [r.status for r in results] == ["ok"] * len(results), [r.error for r in results]
    with closing(open_db(db)) as conn:
        for day in range(rebaselines):
            rebaseline(conn, confirm=True, reason="Owner approved", now=datetime(2026, 11, 1 + day, tzinfo=UTC))
    exported = export_mspdi(db)
    event("progress written" if exported.progress_tasks else "no progress written")
    assert_round_trip(db, exported.data)
    assert_reimports(db, exported.data)
