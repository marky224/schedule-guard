"""Generate update files, run them through the real sync engine, and check what P1-P6 compare.

An item may say it was made against CURRENT, the version its task is at
when the item is written to the update file, as a crew that just read it
would, or NEXT, one past it, as a stale copy would be. Each batch of items
is written to the file just before a sync run, so CURRENT is the version
the run starts from; earlier lines stay in the file and come back as
duplicates, as they do for the file source. A notes chain is made against
CURRENT, NEXT and AFTER_NEXT in turn, as a crew that saw each of its own
updates applied would.
"""

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from hypothesis import settings
from hypothesis import strategies as st

from schedule_guard.clock import fixed_clock
from schedule_guard.guard import PROGRESS_FIELDS
from schedule_guard.sources.file import FileSource
from schedule_guard.store.db import open_db
from schedule_guard.sync import RunResult, sync

CLOCK = fixed_clock(datetime(2026, 10, 30, 17, 0, tzinfo=UTC))
# Every example runs real sync runs, one fsync-ed commit per item on SQLite's
# default settings, so the engine properties take fewer examples than the
# profile's 40.
ENGINE = settings(max_examples=15)
CURRENT, NEXT, AFTER_NEXT = "<current version>", "<next version>", "<two past the current version>"
OFFSETS = {CURRENT: 0, NEXT: 1, AFTER_NEXT: 2}
PROGRESS = "task_uid, version, percent_complete, actual_start, actual_finish, remaining_work, notes"
LEAVES = [2, 3, 4, 5, 7, 8]  # a few tasks that aren't summaries, so their versions climb
SUMMARIES = [0, 1, 6, 12]

# Strategies. Hypothesis draws the simplest choice most often, so the
# simplest one here is a good value and a valid shape, and each bad one is a
# rare roll.

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
SUBMITTED_AT = st.sampled_from(["2026-10-30T09:15:00-05:00", "2026-10-30T14:00:00Z", "2026-10-29T17:00:00-05:00"])


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
    """One line of an update file: an item to render, or a line that isn't JSON."""
    shape = draw(st.integers(0, 19))
    if shape == 19:
        return draw(st.sampled_from(["not json", "[1, 2]", '{"source_item_id": "u-1"']))
    item = {
        "source_item_id": f"u-{draw(st.integers(0, 6))}",  # small pools, so some keys repeat
        "item_version": draw(st.integers(0, 1)),
        "task_uid": draw(st.sampled_from(LEAVES) if not rarely(draw, 4) else st.sampled_from(
            [*SUMMARIES, 40, 41, "3"]  # a summary task, no such task, not an integer
        )),
        "made_against_version": draw(st.sampled_from([NEXT, 0, 1, 2])) if rarely(draw, 4) else CURRENT,
        "fields": draw(fields()),
        "submitted_at": draw(SUBMITTED_AT),
        "submitted_by": draw(st.sampled_from(["Crew lead A", "Crew lead B"])),
    }
    if shape == 18:
        del item[draw(st.sampled_from(sorted(item)))]
    elif shape == 17:
        item["fields"] = {}
    return item


@st.composite
def notes_chain(draw) -> list[dict]:
    """Two or three notes updates to one task, each made against the version the one before leaves.

    They are submitted after every other item's time, in order, so they
    usually all apply and the task's notes get more than one entry in one
    run: a guard that replaced the notes instead of appending can't pass.
    """
    uid, tag = draw(st.sampled_from(LEAVES)), draw(st.integers(0, 99))
    chain = []
    for step, made_against in enumerate([CURRENT, NEXT, AFTER_NEXT][: draw(st.integers(2, 3))]):
        chain.append({
            "source_item_id": f"n-{uid}-{tag}-{step}", "item_version": 0, "task_uid": uid,
            "made_against_version": made_against, "fields": {"notes": draw(GOOD["notes"])},
            "submitted_at": f"2026-10-30T1{step}:00:00-05:00",
            "submitted_by": draw(st.sampled_from(["Crew lead A", "Crew lead B"])),
        })
    return chain


@st.composite
def batch(draw) -> list:
    """The lines one sync run adds to the update file; now and then a notes chain among them."""
    lines = draw(st.lists(items(), min_size=1, max_size=6))
    if rarely(draw, 3):
        at = draw(st.integers(0, len(lines)))
        lines[at:at] = draw(notes_chain())
    return lines


BATCHES = st.lists(batch(), min_size=1, max_size=3)


# Running


def versions(db: Path) -> dict[int, int]:
    with closing(open_db(db)) as conn:
        return dict(conn.execute("SELECT task_uid, version FROM task_progress"))


def render(item, current: dict[int, int]) -> str:
    if isinstance(item, str):
        return item
    made_against = item.get("made_against_version")
    if made_against in OFFSETS:
        version = current.get(item.get("task_uid"), 0) if isinstance(item.get("task_uid"), int) else 0
        item = {**item, "made_against_version": version + OFFSETS[made_against]}
    return json.dumps(item)


def write_batch(db: Path, updates: Path, batch: list, *, copies: int = 1) -> None:
    current = versions(db)
    with open(updates, "a", encoding="utf-8") as f:
        for item in batch:
            f.write(f"{render(item, current)}\n" * copies)


def run_batches(db: Path, batches: list[list], *, copies: int = 1) -> list[RunResult]:
    """One sync run per batch, each after appending the batch to the update file."""
    updates = db.with_name("updates.jsonl")
    results = []
    for batch in batches:
        write_batch(db, updates, batch, copies=copies)
        results.append(sync(db, FileSource(updates), clock=CLOCK))
    return results


# What the properties compare


def progress_rows(conn: sqlite3.Connection) -> dict:
    return {row[0]: row[1:] for row in conn.execute(f"SELECT {PROGRESS} FROM task_progress ORDER BY task_uid")}


def baselines(conn: sqlite3.Connection) -> tuple[bytes, dict[str, list]]:
    """P1: the baseline bytes plus the stored file, and every column of every baseline and baseline_task row."""
    data = b"".join(xml for (xml,) in conn.execute(
        "SELECT xml_c14n FROM baseline_task ORDER BY project_id, number, task_uid"
    ))
    data += conn.execute("SELECT source_xml FROM project").fetchone()[0]
    rows = {
        "baseline": conn.execute("SELECT * FROM baseline ORDER BY project_id, number").fetchall(),
        "baseline_task": conn.execute("SELECT * FROM baseline_task ORDER BY project_id, number, task_uid").fetchall(),
    }
    return data, rows


def master_schedule(conn: sqlite3.Connection) -> tuple[bytes, list]:
    """P2: the plan bytes, and every column of every task row."""
    data = b"".join(plan for (plan,) in conn.execute("SELECT plan_c14n FROM task ORDER BY project_id, uid"))
    return data, conn.execute("SELECT * FROM task ORDER BY project_id, uid").fetchall()


def assert_unchanged(before: tuple[bytes, list | dict], after: tuple[bytes, list | dict], what: str) -> None:
    assert hashlib.sha256(after[0]).hexdigest() == hashlib.sha256(before[0]).hexdigest(), f"{what}: bytes changed"
    assert after[0] == before[0], f"{what}: bytes changed"
    assert after[1] == before[1], f"{what}: a row changed"


def hours(duration: str) -> Decimal:
    h, m, s = re.fullmatch(r"PT([0-9]+)H([0-9]+)M([0-9.]+)S", duration).groups()
    return Decimal(h) + Decimal(m) / 60 + Decimal(s) / 3600


def applied_updates(conn: sqlite3.Connection) -> list[tuple[int, dict, dict, dict, int]]:
    """(task_uid, fields, before, after, made_against_version) of every applied outcome."""
    rows = conn.execute(
        "SELECT su.task_uid, su.fields_json, o.before_json, o.after_json, su.made_against_version FROM outcome o"
        " JOIN source_update su ON su.id = o.update_id WHERE o.status = 'applied'"
    )
    return [(uid, json.loads(f), json.loads(b), json.loads(a), made) for uid, f, b, a, made in rows]


def assert_all_or_nothing(conn: sqlite3.Connection, imported: dict) -> None:
    """P4: one outcome per key; an applied update changes only its fields, and nothing else changes progress.

    An outcome keeps only the entry its update appended to the notes, so the
    task's notes are rebuilt from the imported ones and each applied entry in
    turn, and must end as task_progress holds them.
    """
    captured = conn.execute("SELECT count(*) FROM source_update").fetchone()[0]
    assert conn.execute("SELECT count(*) FROM outcome").fetchone()[0] == captured
    chains: dict[int, list[tuple[dict, dict]]] = {}
    for uid, fields, before, after, _ in applied_updates(conn):
        assert set(fields) <= set(PROGRESS_FIELDS), fields
        assert after["version"] == before["version"] + 1
        for name in PROGRESS_FIELDS:
            if name == "notes":
                entry = after["notes_entry"]
                assert (entry is None) == (name not in fields), entry
                assert entry is None or (entry.startswith("[") and entry.endswith(f"] {fields[name]}")), (
                    f"P4: notes_entry {entry!r} is not an entry for {fields[name]!r}"
                )
            elif name not in fields:
                assert after[name] == before[name], name
            elif name == "remaining_work":
                assert hours(after[name]) == Decimal(str(fields[name])), (fields[name], after[name])
            else:
                assert after[name] == fields[name], name
        chains.setdefault(uid, []).append((before, after))
    final = progress_rows(conn)
    for uid, row in final.items():
        steps = sorted(chains.get(uid, []), key=lambda step: step[0]["version"])
        state = _as_progress(imported[uid])
        for before, after in steps:  # each applied update starts where the previous one ended
            assert before == {k: v for k, v in state.items() if k != "notes"}, (uid, before, state)
            notes, entry = state["notes"], after["notes_entry"]
            state = {k: v for k, v in after.items() if k != "notes_entry"}
            state["notes"] = notes if entry is None else f"{notes}\n{entry}" if notes else entry  # appended
        # and nothing else moved the task's progress, notes included
        assert _as_progress(row) == state, f"P4: task {uid}'s progress isn't what its applied updates made it"


def assert_stale_never_applied(conn: sqlite3.Connection) -> None:
    """P5: every applied update was made against the version the task was at."""
    for _, _, before, _, made_against in applied_updates(conn):
        assert before["version"] == made_against, (before["version"], made_against)


def _as_progress(row: tuple) -> dict:
    return dict(zip(("version", *PROGRESS_FIELDS), row, strict=True))


def normalized(db: Path) -> dict:
    """What sync decided and stored, keyed by item rather than by row id or run."""
    with closing(open_db(db)) as conn:
        keys = {i: (src, sid, ver) for i, src, sid, ver in conn.execute(
            "SELECT id, source, source_item_id, item_version FROM source_update"
        )}
        return {
            "progress": sorted(
                (*row[:-1], keys.get(row[-1])) for row in conn.execute(
                    f"SELECT {PROGRESS}, last_update_id FROM task_progress"
                )
            ),
            "updates": sorted(conn.execute(
                "SELECT source, source_item_id, item_version, raw_json, problem FROM source_update"
            ).fetchall()),
            "outcomes": sorted((keys[u], *rest) for u, *rest in conn.execute(
                "SELECT update_id, status, reason_code, detail, decided_at, before_json, after_json FROM outcome"
            )),
            "conflicts": sorted((keys[u], *rest) for u, *rest in conn.execute(
                "SELECT update_id, reason_code, detail, status, reviewed_at, note FROM conflict"
            )),
        }
