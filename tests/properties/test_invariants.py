"""P1, P2, P4 and P5 (PLAN.md section 8) over arbitrary sequences of updates.

Each example runs a sequence through the guard and the applier on a fresh
copy of the imported fixture: valid, stale, duplicate (a key seen before),
unknown-task, forbidden-field, bad-value and malformed updates, mixed.
"""

import re
from collections import Counter
from contextlib import closing
from datetime import UTC, date, datetime
from decimal import Decimal

from harness import CURRENT, NEXT, assert_unchanged, baselines, master_schedule, run
from hypothesis import event, given
from hypothesis import strategies as st

from schedule_guard.clock import fixed_clock
from schedule_guard.guard import PROGRESS_FIELDS, Update
from schedule_guard.store.db import open_db

CLOCK = fixed_clock(datetime(2026, 10, 30, 17, 0, tzinfo=UTC))
LEAF = 3  # "Mobilize and site fencing", at version 0 as imported
LEAVES = [2, 3, 4, 5, 7, 8]  # a few tasks that aren't summaries, so their versions climb
SUMMARIES = [0, 1, 6, 12]

DATE_TIMES = st.builds(
    lambda day, time: f"{day.isoformat()}T{time}",
    st.dates(date(2026, 6, 1), date(2026, 11, 3)),  # a few days past the clock's today
    st.sampled_from(["08:00:00", "17:00:00"]),
)
GOOD = {
    "percent_complete": st.integers(0, 100),
    "actual_start": DATE_TIMES,
    "actual_finish": DATE_TIMES,
    "remaining_work": st.one_of(st.integers(0, 400), st.floats(0, 400)),
    "notes": st.text(st.characters(codec="utf-8", categories=["L", "N", "P", "Zs"]), max_size=12),
}
BAD = st.one_of(
    st.none(), st.booleans(), st.integers(-50, -1), st.integers(101, 200), st.just("soon"), st.just(float("nan")),
    st.just("2026-13-01T08:00:00"), st.just("a\x00b"),
)
FORBIDDEN = st.sampled_from(
    ["planned_finish", "planned_start", "name", "wbs", "is_summary", "baseline_finish", "Proposed Finish", "version"]
)


# Hypothesis draws the simplest choice most often, so the simplest one here
# is a good value and a valid shape, and each bad one is a rare roll.


def rarely(draw, odds: int = 10) -> bool:
    return draw(st.integers(1, odds)) == odds


@st.composite
def fields(draw) -> dict:
    chosen = {}
    for name in draw(st.lists(st.sampled_from(PROGRESS_FIELDS), unique=True, min_size=1, max_size=3)):
        chosen[name] = draw(BAD if rarely(draw) else GOOD[name])
    if rarely(draw, 5):
        chosen[draw(FORBIDDEN)] = draw(st.one_of(DATE_TIMES, st.integers(0, 9)))
    return chosen


@st.composite
def items(draw):
    key = ("file", draw(st.integers(0, 6)), draw(st.integers(0, 1)))  # small pools, so some keys repeat
    shape = draw(st.integers(0, 19))
    if shape == 19:
        payload = draw(st.one_of(st.none(), st.text(max_size=4), st.lists(st.integers(), max_size=2)))
    else:
        payload = {
            "task_uid": draw(st.sampled_from(LEAVES) if not rarely(draw, 4) else st.sampled_from(
                [*SUMMARIES, 40, 41, "3"]  # a summary task, no such task, not an integer
            )),
            "made_against_version": draw(st.sampled_from([NEXT, 0, 1, 2])) if rarely(draw, 4) else CURRENT,
            "fields": draw(fields()),
            "submitted_at": "2026-10-30T09:15:00-05:00",
            "submitted_by": draw(st.sampled_from(["Crew lead A", "Crew lead B"])),
        }
        if shape == 18:
            del payload[draw(st.sampled_from(sorted(payload)))]
        elif shape == 17:
            payload["fields"] = {}
    return key, Update("file", payload)


SEQUENCES = st.lists(items(), min_size=2, max_size=12)


def run_on_a_fresh_copy(fresh_db, sequence, read=lambda conn: None):
    with closing(open_db(fresh_db())) as conn:
        before = read(conn)
        steps = run(conn, sequence, CLOCK)
        return before, read(conn), steps


@given(SEQUENCES)
def test_p1_the_baselines_stay_byte_identical(fresh_db, sequence):
    before, after, _ = run_on_a_fresh_copy(fresh_db, sequence, baselines)
    assert_unchanged(before, after, "baselines")


@given(SEQUENCES)
def test_p2_the_master_schedule_stays_byte_identical(fresh_db, sequence):
    before, after, _ = run_on_a_fresh_copy(fresh_db, sequence, master_schedule)
    assert_unchanged(before, after, "master schedule")


def hours(duration: str) -> Decimal:
    h, m, s = re.fullmatch(r"PT([0-9]+)H([0-9]+)M([0-9.]+)S", duration).groups()
    return Decimal(h) + Decimal(m) / 60 + Decimal(s) / 3600


@given(SEQUENCES)
def test_p4_each_update_has_one_outcome_and_changes_all_or_nothing(fresh_db, sequence):
    _, _, steps = run_on_a_fresh_copy(fresh_db, sequence)
    assert len(steps) == len(sequence)
    for step in steps:
        event(f"outcome: {step.outcome.status} {step.outcome.reason or ''}".rstrip())
        assert step.outcome.status in ("applied", "conflict", "duplicate")
        payload = step.update.payload
        fields = payload.get("fields") if isinstance(payload, dict) else None
        forbidden = isinstance(fields, dict) and any(name not in PROGRESS_FIELDS for name in fields)
        if forbidden or step.outcome.status != "applied":
            assert step.progress_after == step.progress_before
            continue
        uid = step.outcome.task_uid
        assert {u for u in step.progress_before if step.progress_before[u] != step.progress_after[u]} == {uid}
        old, new = step.progress_before[uid], step.progress_after[uid]  # (version, *PROGRESS_FIELDS)
        assert new[0] == old[0] + 1
        for index, name in enumerate(PROGRESS_FIELDS, start=1):
            if name not in fields:
                assert new[index] == old[index], name
            elif name == "remaining_work":
                assert hours(new[index]) == Decimal(str(fields[name])), (fields[name], new[index])
            elif name == "notes":
                assert new[index].endswith(f"] {fields[name]}")
                assert old[index] is None or new[index].startswith(f"{old[index]}\n")
            else:
                assert new[index] == fields[name], name


@given(SEQUENCES)
def test_p5_a_stale_update_never_applies(fresh_db, sequence):
    _, _, steps = run_on_a_fresh_copy(fresh_db, sequence)
    for step in steps:
        if step.outcome.status == "applied":
            assert step.update.payload["made_against_version"] == step.version_before


def test_the_strategies_apply_a_fair_share_of_updates(fresh_db):
    # P1, P2, P4 and P5 can only catch a bad write if updates get applied.
    statuses = Counter()

    @given(SEQUENCES)
    def collect(sequence):
        _, _, steps = run_on_a_fresh_copy(fresh_db, sequence)
        statuses.update(step.outcome.status for step in steps)

    collect()
    assert statuses["applied"] >= 0.15 * sum(statuses.values()), statuses


def update(source_item_id, made_against_version, fields) -> tuple:
    payload = {
        "task_uid": LEAF, "made_against_version": made_against_version, "fields": fields,
        "submitted_at": "2026-10-30T09:15:00-05:00", "submitted_by": "Crew lead A",
    }
    return ("file", source_item_id, 1), Update("file", payload)


def test_the_harness_applies_detects_duplicates_and_keeps_conflicts_out(fixture_db):
    # The four kinds of step the properties run over, one of each, on the real database.
    sequence = [
        update("a", 0, {"percent_complete": 25, "actual_start": "2026-06-08T08:00:00"}),
        update("a", 0, {"percent_complete": 90}),  # the same key again
        update("b", 0, {"percent_complete": 50}),  # made against version 0, but the task is at 1
        update("c", 1, {"percent_complete": 50, "planned_finish": "2027-01-01T17:00:00"}),
    ]
    with closing(open_db(fixture_db)) as conn:
        steps = run(conn, sequence, CLOCK)
    assert [(s.outcome.status, s.outcome.reason) for s in steps] == [
        ("applied", None), ("duplicate", None), ("conflict", "stale_version"), ("conflict", "field_not_allowed"),
    ]
    assert steps[-1].progress_after[LEAF] == (1, 25, "2026-06-08T08:00:00", None, "PT96H0M0S", None)
