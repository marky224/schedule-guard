"""schedule-guard demo: a new database from the fixture and scripted updates, for the report.

build_demo imports tests/fixtures/riverbend-clinic.xml into a new database
file, locks Baseline 0, and syncs tests/fixtures/demo-updates.jsonl twice,
once now and once as the daily run, which finds only duplicates. The
result has applied progress, overdue tasks, two reached milestones and
open conflicts of seven kinds. Every decision and page uses DEMO_CLOCK, so
the demo looks the same whenever it runs. The two files come from the repo
checkout, and the demo never writes into a file that already exists.
"""

import os
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from schedule_guard.baselines import lock_baseline
from schedule_guard.clock import fixed_clock
from schedule_guard.importer import import_file
from schedule_guard.sources.file import FileSource
from schedule_guard.store.db import open_db
from schedule_guard.sync import RunResult, sync

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
FIXTURE = FIXTURES / "riverbend-clinic.xml"
UPDATES = FIXTURES / "demo-updates.jsonl"
DEMO_CLOCK = fixed_clock(datetime(2026, 10, 30, 17, 0, tzinfo=UTC))  # 12:00 on 2026-10-30 in Chicago


class DemoRefused(Exception):
    """No demo database was built: the path exists, or the demo files are missing."""


def build_demo(db_path: str | Path) -> list[RunResult]:
    """Build the demo database at `db_path`, which must not exist; return its two sync runs."""
    for path in (FIXTURE, UPDATES):
        if not path.is_file():
            raise DemoRefused(f"the demo reads {path.name} from the repo's tests/fixtures; run it from a checkout")
    try:
        os.close(os.open(db_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))  # a new file, or none at all
    except FileExistsError:
        raise DemoRefused(f"{db_path} already exists; the demo builds a new database and never writes into one") from None
    now = DEMO_CLOCK()
    with closing(open_db(db_path, create=True)) as conn:
        import_file(conn, FIXTURE, now=now)
        lock_baseline(conn, 0, now=now)
    return [sync(db_path, FileSource(UPDATES), trigger=trigger, clock=DEMO_CLOCK) for trigger in ("now", "daily")]
