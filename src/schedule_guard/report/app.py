"""The read-only HTML report (PLAN.md section 7).

GET-only pages: the summary, tasks (planned against actual, with the
variance from the newest baseline rebaseline added, else Baseline 0),
overdue leaf tasks, milestones, conflicts and runs. Each request opens the database with store.db.open_db_read_only
(mode=ro and query_only, so SQLite refuses any write) and reads one
snapshot of it; this package runs no INSERT, UPDATE, DELETE or DDL and
imports none of the commands that write.

Pages are rendered on the server by Jinja2 with autoescape on, so text
from update files (notes, submitter names, conflict details) and from the
MSPDI file reaches HTML escaped. There is no JS and no form, and FastAPI's
own docs pages are off, since they load scripts. The Content-Security-Policy
header allows the page's inline style and nothing else. The report listens
on 127.0.0.1 only and answers only a Host of 127.0.0.1 or localhost (any
port), with 400 for any other, so a page whose own domain is pointed at
127.0.0.1 (DNS rebinding) can't read the report as same-origin.
"""

import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import jinja2
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from schedule_guard.clock import Clock, system_clock, today
from schedule_guard.store.db import open_db_read_only

HOST = "127.0.0.1"  # the only address the report listens on
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]  # the only Host headers it answers, with any port
TEMPLATES = jinja2.Environment(
    loader=jinja2.FileSystemLoader(Path(__file__).with_name("templates")),
    autoescape=True,
    undefined=jinja2.StrictUndefined,
)
CSP = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
PAGES = {  # route: (template, title)
    "/": ("summary.html", "Summary"),
    "/tasks": ("tasks.html", "Tasks"),
    "/overdue": ("overdue.html", "Overdue"),
    "/milestones": ("milestones.html", "Milestones"),
    "/conflicts": ("conflicts.html", "Conflicts"),
    "/runs": ("runs.html", "Runs"),
}


@dataclass(frozen=True, slots=True)
class Task:
    uid: int
    wbs: str | None
    name: str | None
    outline_level: int | None
    is_summary: bool
    is_milestone: bool
    planned_start: str | None
    planned_finish: str | None
    version: int
    percent_complete: int | None
    actual_start: str | None
    actual_finish: str | None
    remaining_work: str | None
    notes: str | None
    baseline_start: str | None
    baseline_finish: str | None
    start_variance: str | None  # planned minus baseline, in calendar days
    finish_variance: str | None
    overdue: bool  # planned finish before today and % complete under 100

    @property
    def milestone_status(self) -> str:
        return "reached" if self.percent_complete == 100 else "overdue" if self.overdue else "upcoming"


def create_app(db_path: str | Path, *, clock: Clock = system_clock) -> FastAPI:
    """The report on the database at `db_path`, refused now if it can't be read."""
    db_path = Path(db_path)
    open_db_read_only(db_path).close()
    app = FastAPI(title="schedule-guard report", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
    readers = {"/": _summary, "/tasks": _tasks_page, "/overdue": _overdue, "/milestones": _milestones,
               "/conflicts": _conflicts, "/runs": _runs}

    def page(route: str) -> Callable[[], HTMLResponse]:
        template, title = PAGES[route]

        def get() -> HTMLResponse:
            day = today(clock)
            with closing(open_db_read_only(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("BEGIN")  # one snapshot for the whole page
                values = {"project": conn.execute("SELECT name FROM project").fetchone(),
                          **readers[route](conn, day)}
            html = TEMPLATES.get_template(template).render(title=title, today=day.isoformat(), **values)
            return HTMLResponse(html, headers={"Content-Security-Policy": CSP})

        return get

    for route in PAGES:
        app.add_api_route(route, page(route), methods=["GET"], response_class=HTMLResponse)
    return app


def server(app: FastAPI, port: int) -> uvicorn.Server:
    """A server for the report on 127.0.0.1 only; its run() serves until stopped."""
    return uvicorn.Server(uvicorn.Config(app, host=HOST, port=port))


# Reading. Every value is shown as stored; the templates escape it.

_TASKS = (
    "SELECT t.uid, t.wbs, t.name, t.outline_level, t.is_summary, t.is_milestone, t.planned_start, t.planned_finish,"
    " p.version, p.percent_complete, p.actual_start, p.actual_finish, p.remaining_work, p.notes, b.start, b.finish"
    " FROM task t JOIN task_progress p ON p.project_id = t.project_id AND p.task_uid = t.uid"
    " LEFT JOIN baseline_task b ON b.project_id = t.project_id AND b.task_uid = t.uid AND b.number = ?"
    " ORDER BY t.uid"
)


def _baseline(conn: sqlite3.Connection) -> int | None:
    """The baseline the variance uses: the newest one rebaseline added, else Baseline 0; None if neither exists.

    An imported baseline with a higher number is never used: a file may keep
    an archive in Baseline1-10 while Baseline 0 is the one it tracks.
    """
    return conn.execute(
        "SELECT coalesce((SELECT max(number) FROM baseline WHERE locked_by = 'rebaseline'),"
        " (SELECT number FROM baseline WHERE number = 0))"
    ).fetchone()[0]


def _read_tasks(conn: sqlite3.Connection, day: date) -> list[Task]:
    tasks = []
    for row in conn.execute(_TASKS, (_baseline(conn),)):
        (uid, wbs, name, level, summary, milestone, start, finish, version, percent, actual_start, actual_finish,
         remaining, notes, baseline_start, baseline_finish) = row
        finish_day = _day(finish)
        tasks.append(Task(
            uid, wbs, name, level, bool(summary), bool(milestone), start, finish, version, percent, actual_start,
            actual_finish, remaining, notes, baseline_start, baseline_finish, _variance(start, baseline_start),
            _variance(finish, baseline_finish),
            finish_day is not None and finish_day < day and (percent or 0) < 100,
        ))
    return tasks


def _summary(conn: sqlite3.Connection, day: date) -> dict:
    tasks = _read_tasks(conn, day)
    milestones = [t for t in tasks if t.is_milestone]
    return {
        "imported": conn.execute("SELECT source_sha256, imported_at, import_finished_at FROM project").fetchone(),
        "task_count": len(tasks),
        "summary_count": sum(t.is_summary for t in tasks),
        "overdue_count": len(_overdue_tasks(tasks)),
        "milestone_count": len(milestones),
        "reached_count": sum(t.milestone_status == "reached" for t in milestones),
        "baselines": conn.execute(
            "SELECT number, created_at, locked_at, locked_by, reason FROM baseline ORDER BY number"
        ).fetchall(),
        "applied_count": conn.execute("SELECT count(*) FROM outcome WHERE status = 'applied'").fetchone()[0],
        "conflict_counts": conn.execute(
            "SELECT reason_code, sum(status = 'open') AS open, sum(status = 'dismissed') AS dismissed FROM conflict"
            " GROUP BY reason_code ORDER BY reason_code"
        ).fetchall(),
        "run_count": conn.execute("SELECT count(*) FROM sync_run").fetchone()[0],
        "last_run": conn.execute(f"SELECT {_RUN_COLUMNS} FROM sync_run ORDER BY started_at DESC, rowid DESC").fetchone(),
    }


def _tasks_page(conn: sqlite3.Connection, day: date) -> dict:
    return {"tasks": _read_tasks(conn, day), "baseline": _baseline(conn)}


def _overdue(conn: sqlite3.Connection, day: date) -> dict:
    return {"tasks": _overdue_tasks(_read_tasks(conn, day))}


def _overdue_tasks(tasks: list[Task]) -> list[Task]:
    """Overdue leaf tasks: a summary task's % complete stays as imported (G7), so it isn't listed."""
    return [t for t in tasks if t.overdue and not t.is_summary]


def _milestones(conn: sqlite3.Connection, day: date) -> dict:
    return {"tasks": [t for t in _read_tasks(conn, day) if t.is_milestone], "baseline": _baseline(conn)}


def _conflicts(conn: sqlite3.Connection, day: date) -> dict:
    return {"conflicts": conn.execute(
        "SELECT c.id, c.status, c.reason_code, c.detail, su.task_uid, su.source, su.source_item_id, su.item_version,"
        " su.submitted_by, su.submitted_at, o.decided_at, c.reviewed_at, c.note FROM conflict c"
        " JOIN source_update su ON su.id = c.update_id LEFT JOIN outcome o ON o.update_id = c.update_id"
        " ORDER BY c.status = 'dismissed', c.id"
    ).fetchall()}


_RUN_COLUMNS = (
    "id, trigger, rerun_of, source, started_at, finished_at, status, seen, applied, conflicts, duplicates, error"
)


def _runs(conn: sqlite3.Connection, day: date) -> dict:
    return {"runs": conn.execute(f"SELECT {_RUN_COLUMNS} FROM sync_run ORDER BY started_at, rowid").fetchall()}


def _day(text: str | None) -> date | None:
    try:
        return datetime.fromisoformat(text).date()
    except (TypeError, ValueError):  # no date, or one the MSPDI file got wrong
        return None


def _variance(planned: str | None, baseline: str | None) -> str | None:
    """planned minus baseline in calendar days, such as +4 d, -2.5 d or 0 d; None if either is missing."""
    try:
        days = round((datetime.fromisoformat(planned) - datetime.fromisoformat(baseline)).total_seconds() / 86400, 1)
    except (TypeError, ValueError):
        return None
    return "0 d" if days == 0 else f"{days:+g} d"
