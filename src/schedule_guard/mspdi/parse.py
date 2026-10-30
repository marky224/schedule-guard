"""Safe reader for MSPDI files (MS Project XML).

The file is untrusted input (PLAN.md §6). defusedxml parses it with DTDs,
entity declarations and external references all forbidden, so billion-laughs
and XXE payloads are refused before any entity is expanded or any file is
opened. Files over MAX_FILE_BYTES or with more than MAX_TASKS tasks are
rejected, and the root must be `Project` in the MSPDI namespace.

Only the subset the guard needs is read and type-checked: per task the
master-schedule fields, the progress fields, predecessor links and
`<Baseline>` children. Everything else stays untouched in the raw bytes the
importer stores. Each `<Baseline>` subtree is also returned as C14N 2.0
bytes. Dates, durations and costs keep their MSPDI text once validated.
"""

import copy
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import defusedxml
from defusedxml import ElementTree as SafeET

NS = "http://schemas.microsoft.com/project"
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TASKS = 10_000

_INT = re.compile(r"-?[0-9]{1,18}")
_DECIMAL = re.compile(r"-?[0-9]{1,18}(?:\.[0-9]{1,6})?")
_DATETIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}")
_DURATION = re.compile(r"P(?:[0-9]+Y)?(?:[0-9]+M)?(?:[0-9]+D)?(?:T(?:[0-9]+H)?(?:[0-9]+M)?(?:[0-9]+(?:\.[0-9]+)?S)?)?")
_BOOL = {"0": False, "1": True, "false": False, "true": True}


class MspdiError(ValueError):
    """The file is not an MSPDI file this tool accepts."""


@dataclass(frozen=True, slots=True)
class PredecessorLink:
    predecessor_uid: int
    type: int | None  # 0 FF, 1 FS, 2 SF, 3 SS
    link_lag: int | None  # tenths of a minute


@dataclass(frozen=True, slots=True)
class MspdiBaseline:
    number: int
    start: str | None
    finish: str | None
    duration: str | None
    work: str | None
    cost: str | None
    xml_c14n: bytes  # C14N 2.0 of the whole <Baseline> subtree


@dataclass(frozen=True, slots=True)
class MspdiTask:
    uid: int
    name: str | None
    wbs: str | None
    outline_level: int | None
    is_summary: bool
    is_milestone: bool
    planned_start: str | None
    planned_finish: str | None
    planned_duration: str | None
    planned_work: str | None
    percent_complete: int | None
    actual_start: str | None
    actual_finish: str | None
    remaining_work: str | None
    notes: str | None
    predecessors: tuple[PredecessorLink, ...]
    baselines: tuple[MspdiBaseline, ...]


@dataclass(frozen=True, slots=True)
class MspdiProject:
    name: str | None
    title: str | None
    tasks: tuple[MspdiTask, ...]


def read_mspdi_bytes(path: str | Path) -> bytes:
    """Read the file, but never more than MAX_FILE_BYTES + 1 bytes of it."""
    with open(path, "rb") as f:
        data = f.read(MAX_FILE_BYTES + 1)
    _check_size(data)
    return data


def parse_mspdi(data: bytes) -> MspdiProject:
    """Parse MSPDI bytes, or raise MspdiError saying why they were refused."""
    _check_size(data)
    try:
        root = SafeET.fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except defusedxml.DTDForbidden as exc:
        raise MspdiError("refused: the file has a DTD (<!DOCTYPE>); MSPDI files need none") from exc
    except defusedxml.DefusedXmlException as exc:
        raise MspdiError(f"refused unsafe XML: {exc!r}") from exc
    except ET.ParseError as exc:
        raise MspdiError(f"not well-formed XML: {exc}") from exc
    except (LookupError, ValueError) as exc:  # e.g. encoding="x-nope", "utf-7", "rot13"
        raise MspdiError(f"unknown or unsupported encoding: {exc}") from exc
    if root.tag != _q("Project"):
        raise MspdiError(f"root element must be Project in namespace {NS}, not {root.tag!r}")

    containers = root.findall(_q("Tasks"))
    if len(containers) > 1:
        raise MspdiError("more than one Tasks element")
    elements = containers[0].findall(_q("Task")) if containers else []
    if len(elements) > MAX_TASKS:
        raise MspdiError(f"more than {MAX_TASKS} tasks ({len(elements)})")

    tasks: list[MspdiTask] = []
    seen: set[int] = set()
    for index, el in enumerate(elements, start=1):
        task = _task(el, index)
        if task.uid in seen:
            raise MspdiError(f"duplicate task UID {task.uid}")
        seen.add(task.uid)
        tasks.append(task)
    return MspdiProject(
        name=_text(root, "Name", "Project"),
        title=_text(root, "Title", "Project"),
        tasks=tuple(tasks),
    )


def _check_size(data: bytes) -> None:
    if len(data) > MAX_FILE_BYTES:
        raise MspdiError(f"file is over the {MAX_FILE_BYTES // 2**20} MB limit ({MAX_FILE_BYTES} bytes)")


def _q(tag: str) -> str:
    return f"{{{NS}}}{tag}"


def _task(el: ET.Element, index: int) -> MspdiTask:
    uid = _int(el, "UID", f"Task #{index}", minimum=0)
    if uid is None:
        raise MspdiError(f"Task #{index}: missing UID")
    where = f"task UID {uid}"

    predecessors = []
    for link in el.findall(_q("PredecessorLink")):
        pred = _int(link, "PredecessorUID", f"{where} PredecessorLink", minimum=0)
        if pred is None:
            raise MspdiError(f"{where}: PredecessorLink without PredecessorUID")
        predecessors.append(PredecessorLink(
            predecessor_uid=pred,
            type=_int(link, "Type", f"{where} PredecessorLink", minimum=0, maximum=3),
            link_lag=_int(link, "LinkLag", f"{where} PredecessorLink"),
        ))

    baselines = []
    for b in el.findall(_q("Baseline")):
        number = _int(b, "Number", f"{where} Baseline", minimum=0)
        if number is None:
            raise MspdiError(f"{where}: Baseline without Number")
        if any(existing.number == number for existing in baselines):
            raise MspdiError(f"{where}: more than one Baseline {number}")
        bwhere = f"{where} Baseline {number}"
        baselines.append(MspdiBaseline(
            number=number,
            start=_datetime(b, "Start", bwhere),
            finish=_datetime(b, "Finish", bwhere),
            duration=_duration(b, "Duration", bwhere),
            work=_duration(b, "Work", bwhere),
            cost=_decimal(b, "Cost", bwhere),
            xml_c14n=_c14n(b, bwhere),
        ))

    return MspdiTask(
        uid=uid,
        name=_text(el, "Name", where),
        wbs=_text(el, "WBS", where),
        outline_level=_int(el, "OutlineLevel", where, minimum=0),
        is_summary=_bool(el, "Summary", where),
        is_milestone=_bool(el, "Milestone", where),
        planned_start=_datetime(el, "Start", where),
        planned_finish=_datetime(el, "Finish", where),
        planned_duration=_duration(el, "Duration", where),
        planned_work=_duration(el, "Work", where),
        percent_complete=_int(el, "PercentComplete", where, minimum=0, maximum=100),
        actual_start=_datetime(el, "ActualStart", where),
        actual_finish=_datetime(el, "ActualFinish", where),
        remaining_work=_duration(el, "RemainingWork", where),
        notes=_text(el, "Notes", where),
        predecessors=tuple(predecessors),
        baselines=tuple(baselines),
    )


def _text(parent: ET.Element, tag: str, where: str) -> str | None:
    """Text of the single `tag` child; None if absent. Two of them is ambiguous, so refused."""
    found = parent.findall(_q(tag))
    if not found:
        return None
    if len(found) > 1:
        raise MspdiError(f"{where}: more than one {tag} element")
    return found[0].text or ""


def _value(parent: ET.Element, tag: str, where: str, pattern: re.Pattern, kind: str) -> str | None:
    text = _text(parent, tag, where)
    if text is None:
        return None
    value = text.strip()
    if not pattern.fullmatch(value):
        raise MspdiError(f"{where}: {tag} is not {kind}: {text!r}")
    return value


def _int(
    parent: ET.Element, tag: str, where: str, minimum: int | None = None, maximum: int | None = None
) -> int | None:
    value = _value(parent, tag, where, _INT, "an integer")
    if value is None:
        return None
    number = int(value)
    if (minimum is not None and number < minimum) or (maximum is not None and number > maximum):
        raise MspdiError(f"{where}: {tag} {number} is out of range")
    return number


def _bool(parent: ET.Element, tag: str, where: str) -> bool:
    text = _text(parent, tag, where)
    if text is None:
        return False
    if text.strip() not in _BOOL:
        raise MspdiError(f"{where}: {tag} is not a boolean: {text!r}")
    return _BOOL[text.strip()]


def _datetime(parent: ET.Element, tag: str, where: str) -> str | None:
    value = _value(parent, tag, where, _DATETIME, "a date-time (YYYY-MM-DDTHH:MM:SS)")
    if value is not None:
        try:
            datetime.fromisoformat(value)
        except ValueError as exc:
            raise MspdiError(f"{where}: {tag} is not a valid date-time: {value!r}") from exc
    return value


def _duration(parent: ET.Element, tag: str, where: str) -> str | None:
    value = _value(parent, tag, where, _DURATION, "a duration (like PT8H0M0S)")
    if value is not None and value.endswith(("P", "T")):
        raise MspdiError(f"{where}: {tag} is not a duration (like PT8H0M0S): {value!r}")
    return value


def _decimal(parent: ET.Element, tag: str, where: str) -> str | None:
    return _value(parent, tag, where, _DECIMAL, "a decimal number")


def _c14n(el: ET.Element, where: str) -> bytes:
    """C14N 2.0 bytes of one subtree, with the MSPDI namespace as the default."""
    clone = copy.copy(el)
    clone.tail = None  # the whitespace after </Baseline> isn't part of it
    try:
        text = ET.tostring(clone, encoding="unicode", default_namespace=NS)
    except ValueError as exc:
        raise MspdiError(f"{where}: holds an element outside any namespace") from exc
    except RecursionError as exc:
        raise MspdiError(f"{where}: elements are nested too deeply") from exc
    # Re-parsing our own serialization of an already-parsed tree: it has no DTD.
    return ET.canonicalize(xml_data=text).encode("utf-8")
