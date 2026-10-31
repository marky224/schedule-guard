"""P3 and P6 (PLAN.md section 8) on the real sync engine, and no line that stops a run."""

import json
from contextlib import closing
from itertools import zip_longest

from harness import BATCHES, CLOCK, ENGINE, items, normalized, run_batches
from hypothesis import given
from hypothesis import strategies as st

from schedule_guard import progress
from schedule_guard.sources.base import decode_json
from schedule_guard.sources.file import FileSource
from schedule_guard.store.db import open_db
from schedule_guard.sync import rerun, sync


@ENGINE
@given(BATCHES)
def test_p3_syncing_the_same_file_again_changes_nothing(fresh_db, batches):
    db = fresh_db()
    run_batches(db, batches)
    before = normalized(db)
    again = sync(db, FileSource(db.with_name("updates.jsonl")), clock=CLOCK)
    assert (again.status, again.applied, again.conflicts, again.duplicates) == ("ok", 0, 0, again.seen)
    assert normalized(db) == before


@ENGINE
@given(BATCHES)
def test_p3_every_line_twice_leaves_the_same_state_as_once(fresh_db, batches):
    once, twice = fresh_db(), fresh_db()
    run_batches(once, batches)
    run_batches(twice, batches, copies=2)
    assert normalized(twice) == normalized(once)


@ENGINE
@given(BATCHES)
def test_p6_a_rerun_after_a_run_gives_only_duplicates(fresh_db, batches):
    db = fresh_db()
    results = run_batches(db, batches)
    before = normalized(db)
    for result in results:
        again = rerun(db, result.run_id, clock=CLOCK)
        assert (again.status, again.applied, again.conflicts, again.duplicates) == ("ok", 0, 0, again.seen)
        assert again.rerun_of == result.run_id
    assert normalized(db) == before


class Stop(BaseException):
    """Stands in for the process dying halfway through a run."""


def stopping_after(nth: int):
    """An applier that stops the run right after its nth progress write."""
    real, writes = progress.apply_progress, []

    def apply_then_stop(conn, outcome, **kwargs):
        real(conn, outcome, **kwargs)
        writes.append(outcome)
        if len(writes) == nth:
            raise Stop

    return apply_then_stop


@ENGINE
@given(batch=st.lists(items(), min_size=1, max_size=8), nth=st.integers(1, 3))
def test_p6_a_run_stopped_halfway_and_rerun_ends_as_an_uninterrupted_run(fresh_db, batch, nth):
    uninterrupted, stopped = fresh_db(), fresh_db()
    run_batches(uninterrupted, [batch])
    real = progress.apply_progress
    progress.apply_progress = stopping_after(nth)
    try:
        run_batches(stopped, [batch])
    except Stop:
        pass
    finally:
        progress.apply_progress = real
    with closing(open_db(stopped)) as conn:
        (run_id,) = conn.execute("SELECT id FROM sync_run").fetchone()
    assert rerun(stopped, run_id, clock=CLOCK).status == "ok"
    assert normalized(stopped) == normalized(uninterrupted)


# No line stops a run (invariant 7): whatever one line holds, the run ends
# ok, every line gets an outcome, and the lines around it are decided.


def deepest_nesting() -> int:
    """The deepest list nesting decode_json accepts here; one level more and a line is not JSON."""
    low, high = 1, 100_000
    while low < high:
        depth = (low + high + 1) // 2
        try:
            decode_json("[" * depth + "]" * depth)
            low = depth
        except ValueError:
            high = depth - 1
    return low


DEEPEST = deepest_nesting()
HOSTILE_ITEM = {  # on task 3, which none of the valid lines name
    "source_item_id": "h-1", "item_version": 1, "task_uid": 3, "made_against_version": 0,
    "fields": {"notes": "Gate hung"}, "submitted_at": "2026-10-30T09:15:00-05:00", "submitted_by": "Crew lead A",
}
TEXT_PLACES = ["source_item_id", "submitted_at", "submitted_by", "fields.notes", "key", "fields.key"]
VALUE_PLACES = ["task_uid", "made_against_version", "item_version", "fields", "fields.percent_complete",
                "fields.remaining_work", "submitted_by", "extra"]
NUMBERS = ["1e400", "-1e400", "1e-400", "-0", "1" + "0" * 30, str(2**63), str(-(2**63) - 1), "9" * 4300, "9" * 4301,
           "123456789012345678901234567890.5"]
NOT_UTF_8 = [b"\xff", b"\x80", b"\xc0\xaf", b"\xed\xa0\x80", b"\xf4\x90\x80\x80", b"Crew \xe2\x82"]


def with_placeholder(place: str) -> bytes:
    """HOSTILE_ITEM as JSON, with "@@" as the value (or the key) at `place`."""
    item = {**HOSTILE_ITEM, "fields": dict(HOSTILE_ITEM["fields"])}
    target, name = (item["fields"], place[7:]) if place.startswith("fields.") else (item, place)
    if name == "key":
        target["@@"] = 1
    else:
        target[name] = "@@"
    return json.dumps(item).encode()


@st.composite
def hostile_lines(draw) -> bytes:
    """A line no run may stop on: a lone surrogate, nesting at the decoder's edge, a huge number, bad UTF-8, noise."""
    kind = draw(st.sampled_from(["surrogate", "nested", "number", "not-utf-8", "noise"]))
    if kind == "noise":
        return draw(st.binary(min_size=1, max_size=40)).replace(b"\n", b"")
    if kind == "surrogate":
        text = draw(st.text(max_size=3)) + chr(draw(st.integers(0xD800, 0xDFFF))) + draw(st.text(max_size=3))
        return with_placeholder(draw(st.sampled_from(TEXT_PLACES))).replace(b'"@@"', json.dumps(text).encode())
    if kind == "not-utf-8":
        bad = b'"' + draw(st.sampled_from(NOT_UTF_8)) + b'"'
        return with_placeholder(draw(st.sampled_from(TEXT_PLACES))).replace(b'"@@"', bad)
    if kind == "nested":
        depth = draw(st.integers(DEEPEST - 3, DEEPEST + 1))  # each place sits 1 or 2 levels down already
        value = draw(st.sampled_from(["[" * depth + "]" * depth, '{"a": ' * depth + "1" + "}" * depth]))
    else:
        value = draw(st.sampled_from(NUMBERS))
    return with_placeholder(draw(st.sampled_from(VALUE_PLACES))).replace(b'"@@"', value.encode())


VALID_TASKS = [2, 4, 5]


def valid_line(uid: int) -> bytes:
    return json.dumps({**HOSTILE_ITEM, "source_item_id": f"v-{uid}", "task_uid": uid,
                       "fields": {"percent_complete": 25}}).encode()


@ENGINE
@given(st.lists(hostile_lines(), min_size=1, max_size=3))
def test_no_line_stops_a_run_and_the_lines_around_it_are_decided(fresh_db, hostile):
    db = fresh_db()
    path = db.with_name("updates.jsonl")
    lines = [line for pair in zip_longest(map(valid_line, VALID_TASKS), hostile) for line in pair if line is not None]
    path.write_bytes(b"\n".join(lines) + b"\n")
    first = sync(db, FileSource(path), clock=CLOCK)
    assert first.status == "ok", first.error
    again = sync(db, FileSource(path), clock=CLOCK)
    assert (again.status, again.applied, again.conflicts, again.duplicates) == ("ok", 0, 0, again.seen), again.error
    with closing(open_db(db)) as conn:
        assert conn.execute("SELECT count(*) FROM outcome").fetchone() == conn.execute(
            "SELECT count(*) FROM source_update"
        ).fetchone()  # every line was decided
        valid = conn.execute(
            "SELECT su.source_item_id, o.status FROM outcome o JOIN source_update su ON su.id = o.update_id"
            " WHERE su.source_item_id LIKE 'v-%' ORDER BY su.source_item_id"
        ).fetchall()
    assert valid == [(f"v-{uid}", "applied") for uid in VALID_TASKS]
