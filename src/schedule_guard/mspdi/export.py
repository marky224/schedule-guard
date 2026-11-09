"""`schedule-guard export`: the imported MSPDI file with its progress written back (PLAN.md section 9.7).

The export starts from the file the project was imported from
(project.source_xml), parsed again with the safe parser and the checks
import uses, and changes two things in it:

- On each task, the five progress elements, PercentComplete, ActualStart,
  ActualFinish, RemainingWork and Notes, from task_progress. A value equal
  to the file's leaves its element as it is; a changed one replaces the
  element's text; one the file has no element for gets a new element at
  its place in the MSPDI Task sequence (TASK_SEQUENCE), after the last
  element that comes before it there, with the whitespace that followed
  that element. An update can't clear a field, so a NULL means the file had
  no element either, and none is written.
- Each baseline rebaseline added becomes a <Baseline> element after the
  task's last one, in number order, with the bytes baseline_task.xml_c14n
  holds (Q39).

Every other element, the master schedule and the imported Baselines
included, is written as parsed, so it stays C14N-identical to the import
(P7). The file is UTF-8 with MSPDI as the default namespace, so a file
with a name in no namespace, or an attribute in the MSPDI namespace, is
refused: ElementTree can't write the one and would write the other without
its namespace. The database is opened read-only, as the report opens it,
and read in one snapshot; this module runs no write statement.
`write_new_file` then puts the bytes at a path that must not exist yet
(Q97).
"""

import os
import stat
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from defusedxml import ElementTree as SafeET

from schedule_guard.mspdi.parse import NS, MspdiTask, parse_tree, read_project, task_elements
from schedule_guard.store.db import open_db_read_only

PROGRESS_ELEMENTS = {  # task_progress column: MSPDI element
    "percent_complete": "PercentComplete",
    "actual_start": "ActualStart",
    "actual_finish": "ActualFinish",
    "remaining_work": "RemainingWork",
    "notes": "Notes",
}
# The MSPDI Task sequence ([L14] Tasks schema) as far as Baseline, the last
# element export adds. A new element goes after the last child that comes no
# later here; a child whose name isn't listed is never that place. The order
# is UNVERIFIED against a real Project export or the XSD (Q1, PLAN.md C7).
TASK_SEQUENCE = (
    "UID", "GUID", "ID", "Name", "Active", "Manual", "Type", "IsNull", "CreateDate", "Contact", "WBS", "WBSLevel",
    "OutlineNumber", "OutlineLevel", "Priority", "Start", "Finish", "Duration", "ManualStart", "ManualFinish",
    "ManualDuration", "DurationFormat", "Work", "Stop", "Resume", "ResumeValid", "EffortDriven", "Recurring",
    "OverAllocated", "Estimated", "Milestone", "Summary", "DisplayAsSummary", "Critical", "IsSubproject",
    "IsSubprojectReadOnly", "SubprojectName", "ExternalTask", "ExternalTaskProject", "EarlyStart", "EarlyFinish",
    "LateStart", "LateFinish", "StartVariance", "FinishVariance", "WorkVariance", "FreeSlack", "TotalSlack",
    "StartSlack", "FinishSlack", "FixedCost", "FixedCostAccrual", "PercentComplete", "PercentWorkComplete", "Cost",
    "OvertimeCost", "OvertimeWork", "ActualStart", "ActualFinish", "ActualDuration", "ActualCost",
    "ActualOvertimeCost", "ActualWork", "ActualOvertimeWork", "RegularWork", "RemainingDuration", "RemainingCost",
    "RemainingWork", "RemainingOvertimeCost", "RemainingOvertimeWork", "ACWP", "CV", "ConstraintType", "CalendarUID",
    "ConstraintDate", "Deadline", "LevelAssignments", "LevelingCanSplit", "LevelingDelay", "LevelingDelayFormat",
    "PreLeveledStart", "PreLeveledFinish", "Hyperlink", "HyperlinkAddress", "HyperlinkSubAddress",
    "IgnoreResourceCalendar", "Notes", "HideBar", "Rollup", "BCWS", "BCWP", "PhysicalPercentComplete",
    "EarnedValueMethod", "PredecessorLink", "ActualWorkProtected", "ActualOvertimeWorkProtected", "ExtendedAttribute",
    "Baseline",
)
_RANK = {f"{{{NS}}}{name}": rank for rank, name in enumerate(TASK_SEQUENCE)}


class ExportRefused(Exception):
    """No file was written: there is no project to export, it can't be written as MSPDI, or the path exists."""


@dataclass(frozen=True, slots=True)
class MspdiExport:
    data: bytes  # the MSPDI file
    name: str | None
    tasks: int
    progress_tasks: int  # tasks with a progress element written
    added_baselines: tuple[int, ...]  # the baselines rebaseline added, written as <Baseline> elements


def export_mspdi(db_path: str | Path) -> MspdiExport:
    """The project in the database at `db_path` as MSPDI, with its progress written back."""
    with closing(open_db_read_only(db_path)) as conn:
        conn.execute("BEGIN")  # one snapshot for the whole export
        row = conn.execute("SELECT id, name, source_xml FROM project WHERE import_finished_at IS NOT NULL").fetchone()
        if row is None:
            raise ExportRefused("no project has been imported into this database")
        project_id, name, source = row
        progress = {
            uid: dict(zip(PROGRESS_ELEMENTS, values, strict=True))
            for uid, *values in conn.execute(
                f"SELECT task_uid, {', '.join(PROGRESS_ELEMENTS)} FROM task_progress WHERE project_id = ?",
                (project_id,),
            )
        }
        added = tuple(number for (number,) in conn.execute(
            "SELECT number FROM baseline WHERE project_id = ? AND locked_by = 'rebaseline' ORDER BY number",
            (project_id,),
        ))
        added_rows: dict[int, list[bytes]] = {}
        for uid, xml in conn.execute(
            "SELECT bt.task_uid, bt.xml_c14n FROM baseline_task bt JOIN baseline b"
            " ON b.project_id = bt.project_id AND b.number = bt.number"
            " WHERE bt.project_id = ? AND b.locked_by = 'rebaseline' ORDER BY bt.task_uid, bt.number",
            (project_id,),
        ):
            added_rows.setdefault(uid, []).append(xml)

    root = parse_tree(source)
    project = read_project(root)
    written = 0
    for element, task in zip(task_elements(root), project.tasks, strict=True):
        written += _write_progress(element, task, progress[task.uid])
        for xml in added_rows.get(task.uid, []):
            _insert(element, SafeET.fromstring(xml, forbid_dtd=True))
    for node in root.iter():
        for name in node.attrib:
            if name.startswith(f"{{{NS}}}"):  # p:flag with p bound to MSPDI; ElementTree would write it as flag
                raise ExportRefused(
                    f"the file has an attribute in the MSPDI namespace ({name.rpartition('}')[2]} on"
                    f" {node.tag.rpartition('}')[2]}), which would lose its namespace with MSPDI as the default one"
                )
    try:
        data = ET.tostring(root, encoding="utf-8", xml_declaration=True, default_namespace=NS)
    except ValueError as exc:  # ElementTree can't write a name in no namespace under a default one
        raise ExportRefused(
            "the file has an element or attribute outside any namespace, so it can't be written with MSPDI as"
            " the default namespace"
        ) from exc
    # ElementTree writes a carriage return in text as is, which a parser reads back as a line feed. Every raw
    # CR in its output is one (it writes CRs in attribute values as &#13;), so &#13; keeps it a carriage return.
    data = data.replace(b"\r", b"&#13;") + b"\n"
    return MspdiExport(data, name, len(project.tasks), written, added)


def _write_progress(task: ET.Element, parsed: MspdiTask, progress: dict[str, object]) -> bool:
    """Set the task's progress elements whose value differs from the file's; whether any did."""
    changed = False
    for column, tag in PROGRESS_ELEMENTS.items():
        value = progress[column]
        if value is None or value == getattr(parsed, column):
            continue
        element = task.find(f"{{{NS}}}{tag}")  # the parser allows at most one
        if element is None:
            element = ET.Element(f"{{{NS}}}{tag}")
            _insert(task, element)
        element.text = str(value)
        changed = True
    return changed


def _insert(task: ET.Element, new: ET.Element) -> None:
    """Put `new` after the task's last child that comes no later in TASK_SEQUENCE, with that child's whitespace.

    UID comes first, and every task has one, so there always is such a child.
    """
    rank = _RANK[new.tag]
    at = max(i for i, child in enumerate(task) if _RANK.get(child.tag, rank + 1) <= rank)
    new.tail = task[at].tail
    task.insert(at + 1, new)


def write_new_file(path: str | Path, data: bytes) -> None:
    """Write `data` to `path`, which must not exist (Q97).

    `path` is created exclusively first, so an existing file, folder or
    symlink (a dangling one too) is refused and left as it was. The bytes go
    to a temporary file in the same folder, which is then renamed over
    `path` with the mode the new file got. If anything fails after `path`
    was created, both files are removed.
    """
    path = Path(path)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    except FileExistsError:
        raise ExportRefused(f"{path} already exists; export writes a new file and never replaces one") from None
    try:
        try:
            mode = stat.S_IMODE(os.fstat(fd).st_mode)  # 0o666 less the umask, as for any new file
        finally:
            os.close(fd)
        tmp_fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with open(tmp_fd, "wb") as f:
                f.write(data)
                os.fchmod(f.fileno(), mode)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except BaseException:
        path.unlink(missing_ok=True)
        raise
