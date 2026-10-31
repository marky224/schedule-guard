"""One group of tests per guard rule, G0-G7, with a fixed clock."""

import copy
from datetime import UTC, datetime

import pytest

from schedule_guard.clock import fixed_clock
from schedule_guard.guard import (
    MAX_NOTE_CHARS, MAX_REMAINING_HOURS, MAX_SUBMITTER_CHARS, MAX_TASK_NOTES_CHARS, Outcome, Progress, TaskState, Update,
    decide, target_uid,
)

NOON = fixed_clock(datetime(2026, 10, 30, 17, 0, tzinfo=UTC))  # 12:00 in Chicago on 2026-10-30
LEAF = TaskState(
    uid=12,
    is_summary=False,
    progress=Progress(
        version=3, percent_complete=40, actual_start="2026-07-16T08:00:00", remaining_work="PT96H0M0S",
        notes="Pump truck booked",
    ),
)
SUMMARY = TaskState(uid=2, is_summary=True, progress=Progress(version=0, percent_complete=0))


def payload(**changes) -> dict:
    item = {
        "source_item_id": "row-1",
        "item_version": 1,
        "task_uid": 12,
        "made_against_version": 3,
        "fields": {"percent_complete": 60},
        "submitted_at": "2026-10-30T09:15:00-05:00",
        "submitted_by": "Crew lead A",
    }
    item.update(changes)
    return item


def run(item, task=LEAF, *, already_decided=False, clock=NOON) -> Outcome:
    return decide(Update("file", item), task, already_decided=already_decided, clock=clock)


def conflict_reason(item, task=LEAF, **kwargs) -> str:
    outcome = run(item, task, **kwargs)
    assert outcome.status == "conflict", outcome
    assert outcome.before is outcome.after is None  # a conflict carries no progress to write
    return outcome.reason


# G0


@pytest.mark.parametrize("item", [payload(), None, "not an update"], ids=["valid", "none", "text"])
def test_g0_a_key_that_already_has_an_outcome_is_a_duplicate(item):
    assert run(item, already_decided=True) == Outcome("duplicate")


# G1

MALFORMED = {
    "not-an-object": ["percent_complete", 60],
    "text": "{task_uid: 12}",
    "none": None,
    "no-task-uid": {k: v for k, v in payload().items() if k != "task_uid"},
    "task-uid-text": payload(task_uid="12"),
    "task-uid-bool": payload(task_uid=True),
    "task-uid-float": payload(task_uid=12.0),
    "no-version": {k: v for k, v in payload().items() if k != "made_against_version"},
    "version-text": payload(made_against_version="3"),
    "version-null": payload(made_against_version=None),
    "no-fields": {k: v for k, v in payload().items() if k != "fields"},
    "fields-list": payload(fields=[["percent_complete", 60]]),
    "fields-empty": payload(fields={}),
    "fields-name-not-text": payload(fields={1: 60}),
    "no-submitter": {k: v for k, v in payload().items() if k != "submitted_by"},
    "submitter-blank": payload(submitted_by="  "),
    "submitter-number": payload(submitted_by=7),
    "submitter-control": payload(submitted_by="Crew\x00lead"),
    # No control character at all in a name, so it can't break or forge a notes entry.
    "submitter-line-feed": payload(submitted_by="Crew\nlead"),
    "submitter-carriage-return": payload(submitted_by="Crew\rlead"),
    "submitter-tab": payload(submitted_by="Crew\tlead"),
    "submitter-del": payload(submitted_by="Crew\x7flead"),
    "submitter-c1-csi": payload(submitted_by="Crew\x9blead"),
    "submitter-open-bracket": payload(submitted_by="Crew [A"),
    "submitter-close-bracket": payload(submitted_by="Crew A]"),
    "submitter-forged-entry": payload(submitted_by="Crew A] forged\n[2026-10-01, file, Owner"),
    "submitter-too-long": payload(submitted_by="C" * (MAX_SUBMITTER_CHARS + 1)),
    "no-submitted-at": {k: v for k, v in payload().items() if k != "submitted_at"},
    "submitted-at-words": payload(submitted_at="yesterday"),
    "submitted-at-naive": payload(submitted_at="2026-10-30T09:15:00"),
    "submitted-at-date-only": payload(submitted_at="2026-10-30"),
    "submitted-at-before-year-1": payload(submitted_at="0001-01-01T00:00:00+05:00"),
}


@pytest.mark.parametrize("item", MALFORMED.values(), ids=MALFORMED.keys())
def test_g1_a_malformed_payload_is_a_conflict(item):
    assert conflict_reason(item) == "malformed"


def test_g1_detail_says_what_is_missing():
    assert run(payload(task_uid="12")).detail == "task_uid is not an integer: '12'"


def test_g1_a_name_of_up_to_100_characters_is_a_name():
    assert MAX_SUBMITTER_CHARS == 100
    assert run(payload(submitted_by="C" * 100)).status == "applied"
    outcome = run(payload(submitted_by="C" * 101))
    assert outcome.reason == "malformed"
    assert outcome.detail.startswith("submitted_by is 101 characters; a name is at most 100: 'CCC")


# G2


@pytest.mark.parametrize("task", [None, TaskState(uid=13, is_summary=False, progress=Progress(version=3))])
def test_g2_an_update_for_a_task_that_isnt_there_is_a_conflict(task):
    outcome = run(payload(), task)
    assert (outcome.status, outcome.reason, outcome.detail) == ("conflict", "unknown_task", "no task with UID 12")


# G3


@pytest.mark.parametrize(
    "fields",
    [
        {"planned_finish": "2026-12-01T17:00:00"},
        {"name": "Pour slab"},
        {"baseline_finish": "2026-12-01T17:00:00"},
        {"Proposed Finish": "2026-12-01T17:00:00"},
        {"version": 4},
        {"percent_complete": 60, "planned_finish": "2026-12-01T17:00:00"},  # one allowed field doesn't help
    ],
    ids=["planned-finish", "name", "baseline-field", "proposed-finish", "version", "mixed"],
)
def test_g3_a_field_outside_the_progress_fields_is_a_conflict(fields):
    assert conflict_reason(payload(fields=fields)) == "field_not_allowed"


def test_g3_detail_lists_every_field_of_the_update():
    fields = {"percent_complete": 60, "planned_finish": "2026-12-01T17:00:00", "notes": "x"}
    assert run(payload(fields=fields)).detail == (
        "not progress fields: 'planned_finish'; the update's fields: 'notes', 'percent_complete', 'planned_finish'"
    )


# G4


@pytest.mark.parametrize("version", [2, 4, 0, -1])
def test_g4_an_update_made_against_another_version_is_a_conflict(version):
    outcome = run(payload(made_against_version=version))
    assert (outcome.status, outcome.reason) == ("conflict", "stale_version")
    assert outcome.detail == f"made against version {version}; the task is at version 3"


# G5

INVALID = {
    "percent-101": {"percent_complete": 101},
    "percent-negative": {"percent_complete": -1},
    "percent-fraction": {"percent_complete": 50.5},
    "percent-text": {"percent_complete": "50"},
    "percent-bool": {"percent_complete": True},
    "percent-null": {"percent_complete": None},
    "start-words": {"actual_start": "tomorrow"},
    "start-month-13": {"actual_start": "2026-13-01T08:00:00"},
    "start-date-only": {"actual_start": "2026-10-30"},
    "start-with-offset": {"actual_start": "2026-10-30T08:00:00-05:00"},
    "start-number": {"actual_start": 20261030},
    "start-null": {"actual_start": None},
    "start-tomorrow": {"actual_start": "2026-10-31T08:00:00"},
    "finish-next-year": {"actual_finish": "2027-01-04T17:00:00"},
    "remaining-negative": {"remaining_work": -1},
    "remaining-negative-fraction": {"remaining_work": -0.5},
    "remaining-nan": {"remaining_work": float("nan")},
    "remaining-infinite": {"remaining_work": float("inf")},
    "remaining-too-big": {"remaining_work": MAX_REMAINING_HOURS + 1},
    "remaining-text": {"remaining_work": "8"},
    "remaining-bool": {"remaining_work": True},
    "remaining-null": {"remaining_work": None},
    "notes-number": {"notes": 7},
    "notes-null": {"notes": None},
    "notes-nul": {"notes": "a\x00b"},
    "notes-escape": {"notes": "a\x1b[2Jb"},
    "notes-lone-surrogate": {"notes": "a\ud800b"},
    "notes-del": {"notes": "x\x7fy"},
    "notes-c1-csi": {"notes": "a\x9b2Jb"},
    "notes-noncharacter-fffe": {"notes": "a" + chr(0xFFFE)},
    "notes-noncharacter-ffff": {"notes": "a" + chr(0xFFFF)},
    "notes-vertical-tab": {"notes": "a\x0bb"},
    "notes-too-long": {"notes": "n" * (MAX_NOTE_CHARS + 1)},
}


@pytest.mark.parametrize("fields", INVALID.values(), ids=INVALID.keys())
def test_g5_a_value_of_the_wrong_type_or_out_of_range_is_a_conflict(fields):
    assert conflict_reason(payload(fields=fields)) == "invalid_value"


@pytest.mark.parametrize(
    "text",
    [chr(0xD7FF), chr(0xE000), chr(0xFFFD), chr(0x10000), chr(0x10FFFF), "tab\tthen", "line\nbreak", "cr\rlf"],
    ids=["d7ff", "e000", "fffd", "10000", "10ffff", "tab", "line-feed", "carriage-return"],
)
def test_g5_notes_take_any_xml_character_and_keep_their_line_breaks(text):
    # The ends of the ranges XML 1.0 allows, and the three control characters notes may hold.
    assert run(payload(fields={"notes": text})).after.notes == f"Pump truck booked\n[2026-10-30, file, Crew lead A] {text}"


def test_g5_one_note_is_at_most_4000_characters():
    assert MAX_NOTE_CHARS == 4_000
    assert run(payload(fields={"notes": "n" * 4_000})).status == "applied"
    assert run(payload(fields={"notes": "n" * 4_001})).detail == "notes is 4,001 characters; one note is at most 4,000"


def test_g5_a_note_may_take_the_tasks_notes_to_100000_characters_and_no_further():
    assert MAX_TASK_NOTES_CHARS == 100_000
    entry = "\n[2026-10-30, file, Crew lead A] note"
    full = TaskState(12, False, Progress(version=3, notes="n" * (100_000 - len(entry))))
    assert len(run(payload(fields={"notes": "note"}), full).after.notes) == 100_000
    outcome = run(payload(fields={"notes": "notes"}), full)
    assert (outcome.status, outcome.reason, outcome.detail) == (
        "conflict", "invalid_value", "the note would take the task's notes to 100,001 characters; they hold at most 100,000"
    )


def test_g5_a_task_whose_notes_are_already_past_the_cap_takes_updates_without_a_note():
    imported = TaskState(12, False, Progress(version=3, notes="n" * 200_000))  # as a file may import them
    assert run(payload(fields={"percent_complete": 60}), imported).status == "applied"
    assert conflict_reason(payload(fields={"notes": "x"}), imported) == "invalid_value"


def test_g5_today_is_the_chicago_date_not_the_utc_one():
    # 22:30 on 2026-10-30 in Chicago, while UTC is already on 2026-10-31.
    evening = fixed_clock(datetime(2026, 10, 31, 3, 30, tzinfo=UTC))
    assert run(payload(fields={"actual_finish": "2026-10-30T17:00:00"}), clock=evening).status == "applied"
    outcome = run(payload(fields={"actual_finish": "2026-10-31T08:00:00"}), clock=evening)
    assert (outcome.reason, outcome.detail) == (
        "invalid_value",
        "actual_finish 2026-10-31T08:00:00 is later than today (2026-10-30)",
    )


def test_g5_a_later_time_today_is_not_later_than_today():
    assert run(payload(fields={"actual_finish": "2026-10-30T23:59:59"})).status == "applied"


# G6


@pytest.mark.parametrize(
    ("task", "fields"),
    [
        pytest.param(LEAF, {"actual_finish": "2026-07-15T17:00:00"}, id="finish-before-current-start"),
        pytest.param(
            TaskState(12, False, Progress(version=3)),
            {"actual_start": "2026-07-16T08:00:00", "actual_finish": "2026-07-16T07:59:59"},
            id="finish-before-start-in-the-update",
        ),
        pytest.param(
            TaskState(12, False, Progress(version=3, actual_start="2026-07-16T08:00:00",
                                          actual_finish="2026-07-22T17:00:00")),
            {"actual_start": "2026-07-23T08:00:00"},
            id="start-after-current-finish",
        ),
        pytest.param(LEAF, {"percent_complete": 100}, id="complete-without-a-finish"),
        pytest.param(
            TaskState(12, False, Progress(version=3, percent_complete=100)),
            {"notes": "x"},
            id="already-complete-without-a-finish",
        ),
    ],
)
def test_g6_inconsistent_actuals_after_the_merge_are_a_conflict(task, fields):
    assert conflict_reason(payload(fields=fields), task) == "inconsistent_actuals"


@pytest.mark.parametrize(
    "fields",
    [
        {"percent_complete": 100, "actual_finish": "2026-07-22T17:00:00"},
        {"actual_finish": "2026-07-16T08:00:00"},  # finishing the moment it started
    ],
    ids=["complete-with-a-finish", "finish-equals-start"],
)
def test_g6_consistent_actuals_apply(fields):
    assert run(payload(fields=fields)).status == "applied"


@pytest.mark.parametrize(
    "fields",
    [{"notes": "Inspector on site"}, {"remaining_work": 4}, {"actual_start": "2026-07-16T08:00:00"}],
    ids=["notes", "remaining-work", "actual-start"],
)
def test_g6_a_task_at_100_percent_without_a_finish_refuses_updates_that_leave_it_so(fields):
    # G6 checks the merged state, so a task imported at 100 % with no actual
    # finish refuses every update that leaves it that way, a notes-only one
    # too; the same update with an actual finish applies.
    complete = TaskState(12, False, Progress(version=0, percent_complete=100))
    assert conflict_reason(payload(made_against_version=0, fields=fields), complete) == "inconsistent_actuals"
    with_finish = {**fields, "actual_finish": "2026-10-29T17:00:00"}
    assert run(payload(made_against_version=0, fields=with_finish), complete).status == "applied"


# G7


def test_g7_progress_on_a_summary_task_is_a_conflict():
    item = payload(task_uid=2, made_against_version=0, fields={"percent_complete": 10})
    outcome = run(item, SUMMARY)
    assert (outcome.status, outcome.reason) == ("conflict", "summary_task")
    assert outcome.detail == "task UID 2 is a summary task; Project rolls its progress up"


# Order: the first failing check decides.


@pytest.mark.parametrize(
    ("item", "task", "reason"),
    [
        pytest.param(payload(task_uid="x"), None, "malformed", id="g1-before-g2"),
        pytest.param(payload(fields={"name": "x"}), None, "unknown_task", id="g2-before-g3"),
        pytest.param(payload(made_against_version=0, fields={"name": "x"}), LEAF, "field_not_allowed",
                     id="g3-before-g4"),
        pytest.param(payload(made_against_version=0, fields={"percent_complete": 101}), LEAF, "stale_version",
                     id="g4-before-g5"),
        pytest.param(payload(fields={"percent_complete": 100, "remaining_work": -1}), LEAF, "invalid_value",
                     id="g5-before-g6"),
        pytest.param(payload(fields={"percent_complete": 100, "notes": "x"}),
                     TaskState(12, False, Progress(version=3, notes="n" * MAX_TASK_NOTES_CHARS)), "invalid_value",
                     id="g5-notes-cap-before-g6"),
        pytest.param(payload(task_uid=2, made_against_version=0, fields={"percent_complete": 100}), SUMMARY,
                     "inconsistent_actuals", id="g6-before-g7"),
        pytest.param(payload(task_uid=2, made_against_version=0, fields={"percent_complete": -1}), SUMMARY,
                     "invalid_value", id="g5-before-g7"),
    ],
)
def test_the_first_failing_check_decides(item, task, reason):
    assert conflict_reason(item, task) == reason


# Applied


def test_an_applied_update_writes_its_fields_and_bumps_the_version():
    item = payload(fields={"percent_complete": 60, "actual_finish": "2026-10-29T17:00:00", "remaining_work": 12.5})
    outcome = run(item)
    assert outcome == Outcome(
        "applied",
        task_uid=12,
        made_against_version=3,
        before=LEAF.progress,
        after=Progress(
            version=4, percent_complete=60, actual_start="2026-07-16T08:00:00", actual_finish="2026-10-29T17:00:00",
            remaining_work="PT12H30M0S", notes="Pump truck booked",
        ),
    )


@pytest.mark.parametrize(
    ("hours", "duration"),
    [(96, "PT96H0M0S"), (0, "PT0H0M0S"), (0.1, "PT0H6M0S"), (0.3333, "PT0H19M59.88S"), (-0.0, "PT0H0M0S"),
     (MAX_REMAINING_HOURS, "PT1000000H0M0S")],
)
def test_remaining_work_in_hours_is_stored_as_an_mspdi_duration(hours, duration):
    assert run(payload(fields={"remaining_work": hours})).after.remaining_work == duration


def test_notes_are_appended_as_a_dated_entry_never_replaced():
    item = payload(fields={"notes": "Slab poured"}, submitted_at="2026-10-31T02:00:00Z")  # 21:00 on 10-30 in Chicago
    assert run(item).after.notes == "Pump truck booked\n[2026-10-30, file, Crew lead A] Slab poured"
    no_notes = TaskState(12, False, Progress(version=3))
    assert run(item, no_notes).after.notes == "[2026-10-30, file, Crew lead A] Slab poured"


def test_decide_is_pure():
    item = payload(fields={"notes": "Slab poured", "percent_complete": 60})
    kept = copy.deepcopy(item)
    first = run(item)
    assert run(item) == first
    assert item == kept
    assert LEAF.progress.version == 3


@pytest.mark.parametrize(
    ("item", "uid"),
    [(payload(), 12), (payload(task_uid="12"), None), (payload(task_uid=True), None), ([], None), (None, None)],
)
def test_target_uid_is_the_integer_task_uid_or_none(item, uid):
    assert target_uid(Update("file", item)) == uid
