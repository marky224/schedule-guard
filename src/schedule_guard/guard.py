"""The guard: one pure decision per progress update (PLAN.md section 5).

`decide` takes the update, the current state of the task it names and the
clock, and returns exactly one Outcome. It reads nothing else and writes
nothing. The caller loads the task (`target_uid` says which), says whether
the update's idempotency key already has an outcome, and stores the result.
The checks run in this order; the first one that fails decides:

- G0 `duplicate`: the key already has an outcome.
- G1 `malformed`: the payload isn't an object with an integer `task_uid` and
  `made_against_version`, a non-empty `fields` object, a `submitted_by`
  text and a `submitted_at` date-time with a UTC offset.
- G2 `unknown_task`: the project has no task with that UID.
- G3 `field_not_allowed`: a field outside the five progress fields.
- G4 `stale_version`: made against a version that isn't the task's current one.
- G5 `invalid_value`: `percent_complete` not an integer from 0 to 100;
  `actual_start` or `actual_finish` not an MSPDI date-time
  (2026-07-16T08:00:00) or later than today in America/Chicago (Q6);
  `remaining_work` not a number of hours from 0 to MAX_REMAINING_HOURS;
  `notes` not text an MSPDI file can hold. Null is no value: an update
  can't clear a field.
- G6 `inconsistent_actuals`, after merging with the current values:
  `actual_finish` before `actual_start`, or 100 % complete without an
  `actual_finish` (Q6).
- G7 `summary_task`: Project rolls a summary task's progress up from its
  subtasks (Q4).

Otherwise the update is `applied`: its fields replace the current ones,
remaining work stored as an MSPDI duration, except `notes`, which gets a new
line "[date, source, submitter] text" with the date the update was
submitted (Q3); the version rises by 1.
"""

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from schedule_guard.clock import SITE_TIMEZONE, Clock, today

PROGRESS_FIELDS = ("percent_complete", "actual_start", "actual_finish", "remaining_work", "notes")
MAX_REMAINING_HOURS = 1_000_000  # about 114 years of work; anything above is a mistake

_DATETIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}")
# A character XML 1.0 can't hold, so text with one couldn't go back into an MSPDI file.
_NOT_XML = re.compile("[^\t\n\r\x20-퟿-�\U00010000-\U0010ffff]")


@dataclass(frozen=True, slots=True)
class Progress:
    """A task's progress fields and its task version."""

    version: int
    percent_complete: int | None = None
    actual_start: str | None = None
    actual_finish: str | None = None
    remaining_work: str | None = None
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class TaskState:
    uid: int
    is_summary: bool
    progress: Progress


@dataclass(frozen=True, slots=True)
class Update:
    source: str  # the source's name, such as "file"; set by this program
    payload: object  # the item as its source decoded it: untrusted, anything at all


@dataclass(frozen=True, slots=True)
class Outcome:
    status: Literal["applied", "conflict", "duplicate"]
    reason: str | None = None  # a conflict's reason code
    detail: str | None = None
    task_uid: int | None = None  # known once G1 passes
    made_against_version: int | None = None
    before: Progress | None = None  # an applied update's task progress, before and after
    after: Progress | None = None


@dataclass(frozen=True, slots=True)
class _Payload:
    task_uid: int
    made_against_version: int
    fields: Mapping[str, object]
    submitted_by: str
    submitted_on: date  # submitted_at's date in America/Chicago


def target_uid(update: Update) -> int | None:
    """The task UID the update names, if it is an integer: the task the caller loads."""
    payload = update.payload
    if isinstance(payload, Mapping) and _is_int(uid := payload.get("task_uid")):
        return uid
    return None


def decide(update: Update, task: TaskState | None, *, already_decided: bool, clock: Clock) -> Outcome:
    """The one outcome for `update`, given its task's current state (None if there is none)."""
    if already_decided:  # G0
        return Outcome("duplicate")
    payload = _parse(update.payload)
    if isinstance(payload, str):  # G1
        return Outcome("conflict", "malformed", payload)
    uid, version, fields = payload.task_uid, payload.made_against_version, payload.fields

    def conflict(reason: str, detail: str) -> Outcome:
        return Outcome("conflict", reason, detail, task_uid=uid, made_against_version=version)

    if task is None or task.uid != uid:  # G2
        return conflict("unknown_task", f"no task with UID {uid}")
    not_allowed = sorted(name for name in fields if name not in PROGRESS_FIELDS)
    if not_allowed:  # G3
        return conflict(
            "field_not_allowed",
            f"not progress fields: {', '.join(map(_show, not_allowed))};"
            f" the update's fields: {', '.join(map(_show, sorted(fields)))}",
        )
    current = task.progress
    if version != current.version:  # G4
        return conflict("stale_version", f"made against version {version}; the task is at version {current.version}")
    invalid = _invalid_value(fields, today(clock))
    if invalid:  # G5
        return conflict("invalid_value", invalid)
    after = _merge(current, fields, f"{payload.submitted_on.isoformat()}, {update.source}, {payload.submitted_by}")
    inconsistent = _inconsistent(after)
    if inconsistent:  # G6
        return conflict("inconsistent_actuals", inconsistent)
    if task.is_summary:  # G7
        return conflict("summary_task", f"task UID {uid} is a summary task; Project rolls its progress up")
    return Outcome("applied", task_uid=uid, made_against_version=version, before=current, after=after)


def _parse(payload: object) -> _Payload | str:
    """The payload's parts, or what is malformed about it."""
    if not isinstance(payload, Mapping):
        return f"the update is not an object: {_show(payload)}"
    for name in ("task_uid", "made_against_version"):
        if name not in payload:
            return f"{name} is missing"
        if not _is_int(payload[name]):
            return f"{name} is not an integer: {_show(payload[name])}"
    fields = payload.get("fields")
    if not isinstance(fields, Mapping) or not fields:
        return f"fields is not a non-empty object: {_show(fields)}"
    if not all(isinstance(name, str) for name in fields):
        return "fields has a name that is not text"
    by = payload.get("submitted_by")
    if not isinstance(by, str) or not by.strip() or _NOT_XML.search(by):
        return f"submitted_by is not a name: {_show(by)}"
    submitted_on = _site_date(payload.get("submitted_at"))
    if submitted_on is None:
        return f"submitted_at is not a date-time with a UTC offset: {_show(payload.get('submitted_at'))}"
    return _Payload(payload["task_uid"], payload["made_against_version"], fields, by, submitted_on)


def _invalid_value(fields: Mapping[str, object], today: date) -> str | None:
    """What is wrong with the first bad value, in PROGRESS_FIELDS order."""
    for name in PROGRESS_FIELDS:
        if name not in fields:
            continue
        value = fields[name]
        if name == "percent_complete":
            if not (_is_int(value) and 0 <= value <= 100):
                return f"percent_complete is not an integer from 0 to 100: {_show(value)}"
        elif name in ("actual_start", "actual_finish"):
            day = _mspdi_date(value)
            if day is None:
                return f"{name} is not a date-time like 2026-07-16T08:00:00: {_show(value)}"
            if day > today:
                return f"{name} {value} is later than today ({today.isoformat()})"
        elif name == "remaining_work":
            if not (_is_number(value) and 0 <= value <= MAX_REMAINING_HOURS):
                return f"remaining_work is not a number of hours from 0 to {MAX_REMAINING_HOURS}: {_show(value)}"
        elif not isinstance(value, str) or _NOT_XML.search(value):
            return f"notes is not text an MSPDI file can hold: {_show(value)}"
    return None


def _merge(current: Progress, fields: Mapping[str, object], note_prefix: str) -> Progress:
    values = {name: fields[name] for name in ("percent_complete", "actual_start", "actual_finish") if name in fields}
    if "remaining_work" in fields:
        values["remaining_work"] = _duration(fields["remaining_work"])
    if "notes" in fields:
        entry = f"[{note_prefix}] {fields['notes']}"
        values["notes"] = f"{current.notes}\n{entry}" if current.notes else entry  # appended, never replaced
    return replace(current, version=current.version + 1, **values)


def _inconsistent(progress: Progress) -> str | None:
    start, finish = progress.actual_start, progress.actual_finish
    if start is not None and finish is not None and datetime.fromisoformat(finish) < datetime.fromisoformat(start):
        return f"actual_finish {finish} is before actual_start {start}"
    if progress.percent_complete == 100 and finish is None:
        return "percent_complete is 100 without an actual_finish"
    return None


def _duration(hours: int | float) -> str:
    """Hours as an MSPDI duration, like PT12H30M0S, exactly."""
    seconds = Decimal(str(abs(hours))) * 3600  # str() is a float's shortest form; abs() turns -0.0 into 0
    whole_hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"PT{whole_hours:f}H{minutes:f}M{secs.normalize():f}S"


def _mspdi_date(value: object) -> date | None:
    if not isinstance(value, str) or not _DATETIME.fullmatch(value):
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:  # e.g. month 13
        return None


def _site_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        instant = datetime.fromisoformat(value)
        if instant.tzinfo is None or instant.utcoffset() is None:
            return None
        return instant.astimezone(SITE_TIMEZONE).date()
    except (ValueError, OverflowError):  # unparseable, or past year 1-9999 in Chicago
        return None


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return _is_int(value) or (isinstance(value, float) and math.isfinite(value))


def _show(value: object) -> str:
    """repr() of an untrusted value, cut short."""
    text = repr(value)
    return text if len(text) <= 80 else f"{text[:77]}..."
