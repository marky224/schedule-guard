"""The read-only report: GET-only pages read from the database (PLAN.md section 7)."""

import ast
import functools
import hashlib
import http.client
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import sysconfig
import time
import types
import warnings
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
import uvicorn

from schedule_guard import cli
from schedule_guard.baselines import lock_baseline, rebaseline
from schedule_guard.clock import fixed_clock
from schedule_guard.importer import import_file
from schedule_guard.report.app import CSP, PAGES, create_app, server
from schedule_guard.review import dismiss
from schedule_guard.sources.file import FileSource
from schedule_guard.store.db import StoreError, open_db
from schedule_guard.sync import sync

with warnings.catch_warnings():
    # Starlette now asks for httpx2; the lock has httpx, the dev dependency the plan names for TestClient.
    warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient` is deprecated")
    from fastapi.testclient import TestClient

CLOCK = fixed_clock(datetime(2026, 10, 30, 17, 0, tzinfo=UTC))  # 12:00 on 2026-10-30 in Chicago
IMPORTED = datetime(2026, 10, 30, 12, tzinfo=UTC)
WEEK_44 = Path(__file__).resolve().parent / "fixtures" / "updates" / "week-44.jsonl"
REPORT = Path(__file__).resolve().parent.parent / "src" / "schedule_guard" / "report"


@pytest.fixture
def week_44(fixture_db):
    """The fixture after one run of the week-44 file, with conflict 1 dismissed."""
    sync(fixture_db, FileSource(WEEK_44), clock=CLOCK)
    with closing(open_db(fixture_db)) as conn:
        dismiss(conn, 1, "Resubmit against the current version", now=datetime(2026, 10, 30, 21, tzinfo=UTC))
    return fixture_db


def local(app) -> TestClient:
    """A client that sends Host 127.0.0.1:8000, as a browser on the host does."""
    return TestClient(app, base_url="http://127.0.0.1:8000")


@pytest.fixture
def client(week_44):
    return local(create_app(week_44, clock=CLOCK))


def rows(html: str) -> list[list[str]]:
    """Each table row's cells, as text."""
    return [
        [re.sub(r"<[^>]+>", "", cell).strip() for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        for row in re.findall(r"<tr>(.*?)</tr>", html, re.S)
    ]


def row(html: str, uid: int) -> list[str]:
    (found,) = [cells for cells in rows(html) if cells and cells[0] == str(uid)]
    return found


def test_every_page_is_html_with_a_policy_that_allows_no_script(client):
    for route, (_, title) in PAGES.items():
        response = client.get(route)
        assert response.status_code == 200, route
        assert response.headers["content-type"] == "text/html; charset=utf-8"
        assert response.headers["content-security-policy"] == CSP
        assert f"<h1>{title}</h1>" in response.text
        assert "Today is 2026-10-30 (America/Chicago)" in response.text


def test_the_summary(client):
    html = client.get("/").text
    facts = dict(re.findall(r"<tr><th>(.*?)</th><td>(.*?)</td></tr>", html))
    assert facts["Project"] == "Riverbend Clinic"
    assert facts["Tasks"] == "40 (8 summary tasks)"
    assert facts["Milestones reached"] == '<a href="/milestones">1 of 5</a>'
    assert facts["Applied updates"] == "5"
    assert facts["Sync runs"].endswith("; the last now run, 2026-10-30T17:00:00+00:00, is ok")
    found = rows(html)
    assert ["0", "2026-10-30T12:00:00+00:00", "not locked", "", ""] in found  # Baseline 0
    assert ["stale_version", "1", "0"] in found and ["summary_task", "1", "0"] in found


def test_tasks_show_planned_against_actual_with_the_baseline_variance(client):
    html = client.get("/tasks").text
    assert "baseline 0" in html
    assert row(html, 4)[3:13] == [
        "2026-06-04T08:00:00", "2026-06-15T17:00:00", "2026-06-04T08:00:00", "2026-06-11T17:00:00", "0 d", "+4 d",
        "20", "2026-06-15T08:00:00", "", "PT120H0M0S",
    ]
    fence = row(html, 3)
    assert fence[9:11] == ["40", "2026-06-08T08:00:00"]
    assert fence[-2:] == ["2", "[2026-10-28, file, Site lead (demo)] Fence inspected, gate hung"]


def test_overdue_lists_tasks_due_before_today_and_not_complete(client):
    uids = [cells[0] for cells in rows(client.get("/overdue").text) if cells]
    assert "3" in uids and "27" in uids  # due 2026-06-03 at 40 %, and 2026-10-26 at 0 %
    assert "2" not in uids  # complete
    assert "29" not in uids  # due 2026-11-18


def test_milestones_are_reached_overdue_or_upcoming(client):
    html = client.get("/milestones").text
    assert [(cells[0], cells[5], cells[-1]) for cells in rows(html) if cells] == [
        ("2", "0 d", "reached"), ("11", "+7 d", "overdue"), ("20", "+8 d", "overdue"),
        ("37", "+12 d", "upcoming"), ("39", "+12 d", "upcoming"),
    ]


def test_conflicts_show_open_ones_first_then_the_dismissed_with_their_note(client):
    found = [cells for cells in rows(client.get("/conflicts").text) if cells]
    assert [cells[1] for cells in found] == ["open"] * 7 + ["dismissed"]
    assert found[-1][0] == "1" and found[-1][-1] == "Resubmit against the current version"
    assert {cells[2] for cells in found} == {
        "stale_version", "field_not_allowed", "summary_task", "invalid_value", "unknown_task", "inconsistent_actuals",
        "malformed",
    }


def test_runs(client):
    (cells,) = [cells for cells in rows(client.get("/runs").text) if cells]
    assert cells[2:8] == ["now", "ok", "14", "5", "8", "1"]


def test_fastapis_docs_pages_are_off(client):
    for path in ["/docs", "/redoc", "/openapi.json"]:
        assert client.get(path).status_code == 404, path


def test_the_report_refuses_a_database_it_cant_read(tmp_path):
    with pytest.raises(StoreError, match="can't open database"):
        create_app(tmp_path / "typo.db")
    assert not (tmp_path / "typo.db").exists()


def sql_like_strings(path: Path) -> list[str]:
    """Every string constant in the module but its docstrings."""
    tree = ast.parse(path.read_text())
    docstrings = {
        id(node.body[0].value) for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)) and ast.get_docstring(node) is not None
    }
    return [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
    ]


def imports(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_the_report_imports_nothing_that_writes_and_runs_no_write_statement():
    for path in REPORT.glob("*.py"):
        schedule_guard = {name for name in imports(path) if name.startswith("schedule_guard.")}
        assert schedule_guard <= {
            "schedule_guard.clock.Clock", "schedule_guard.clock.system_clock", "schedule_guard.clock.today",
            "schedule_guard.store.db.open_db_read_only",
        }, path
        for text in sql_like_strings(path):
            assert not re.search(r"\b(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER|ATTACH|VACUUM)\b", text), text


def test_cli_report_refuses_a_missing_database_with_one_error_line(tmp_path, capsys):
    assert cli.main(["report", "--db", str(tmp_path / "typo.db")]) == 1
    out, err = capsys.readouterr()
    assert out == "" and err.startswith("schedule-guard: error: can't open database") and err.count("\n") == 1


@pytest.mark.parametrize("port", ["65536", "-1", "http"])
def test_cli_report_refuses_a_port_outside_0_to_65535(tmp_path, capsys, port):
    with pytest.raises(SystemExit) as exit_:
        cli.main(["report", "--db", str(tmp_path / "x.db"), "--port", port])
    assert exit_.value.code == 2
    assert "not a port from 0 to 65535" in capsys.readouterr().err


# T052: what the report must never do, and overdue against a fixed today.


def project_db(tmp_path, tasks: str, *, name: str | None = "Small project") -> Path:
    """A database imported from a small MSPDI file with the given <Task> elements (no <Name> for None)."""
    xml = tmp_path / "small.xml"
    named = "" if name is None else f"<Name>{name}</Name>"
    xml.write_text(f'<Project xmlns="http://schemas.microsoft.com/project">{named}<Tasks>{tasks}</Tasks></Project>')
    db = tmp_path / "small.db"
    with closing(open_db(db, create=True)) as conn:
        import_file(conn, xml, now=IMPORTED)
    return db


def task(uid: int, name: str, finish: str, percent: int = 0, actual_finish: str | None = None, *,
         summary: bool = False, baselines: dict[int, str] | None = None) -> str:
    """A <Task>; `baselines` maps a Baseline number to its finish (each starts with the task)."""
    done = f"<ActualFinish>{actual_finish}</ActualFinish>" if actual_finish else ""
    saved = "".join(
        f"<Baseline><Number>{number}</Number><Start>2026-10-01T08:00:00</Start><Finish>{at}</Finish></Baseline>"
        for number, at in (baselines or {}).items()
    )
    return (
        f"<Task><UID>{uid}</UID><Name>{name}</Name><Summary>{int(summary)}</Summary><Milestone>0</Milestone>"
        f"<Start>2026-10-01T08:00:00</Start><Finish>{finish}</Finish><PercentComplete>{percent}</PercentComplete>"
        f"{done}{saved}</Task>"
    )


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("route", PAGES)
def test_every_route_answers_any_method_but_get_with_405(client, route, method):
    response = client.request(method, route)
    assert response.status_code == 405, (method, route)
    assert response.headers["allow"] == "GET"


@pytest.mark.parametrize("host", ["attacker.example:8000", "attacker.example", "127.0.0.1.attacker.example:8000",
                                  "localhost.attacker.example:8000"])
@pytest.mark.parametrize("route", PAGES)
def test_every_route_answers_a_foreign_host_with_400(week_44, route, host):
    # A page whose own domain is pointed at 127.0.0.1 sends that domain as the Host (DNS rebinding).
    response = TestClient(create_app(week_44, clock=CLOCK), base_url=f"http://{host}").get(route)
    assert response.status_code == 400, (host, route)
    assert response.text == "Invalid host header"


@pytest.mark.parametrize("host", ["127.0.0.1:8000", "localhost:8000", "127.0.0.1:51234", "localhost:51234"])
def test_every_route_answers_127_0_0_1_and_localhost_on_any_port(week_44, host):
    client = TestClient(create_app(week_44, clock=CLOCK), base_url=f"http://{host}")
    for route in PAGES:
        response = client.get(route)
        assert response.status_code == 200, (host, route)
        assert "Riverbend Clinic" in response.text


SCRIPT = "<script>alert(1)</script>"


def test_text_from_the_files_is_escaped_on_every_page(tmp_path):
    escaped = "&lt;script&gt;alert(1)&lt;/script&gt;"  # also how the MSPDI file writes the names
    db = project_db(tmp_path, task(1, escaped, "2026-10-29T17:00:00"), name=escaped)
    (tmp_path / "updates.jsonl").write_text("".join(json.dumps(line) + "\n" for line in [
        {"source_item_id": "u-1", "item_version": 1, "task_uid": 1, "made_against_version": 0,
         "fields": {"notes": SCRIPT}, "submitted_at": "2026-10-29T09:00:00-05:00", "submitted_by": "Crew (demo)"},
        {"source_item_id": "u-2", "item_version": 1, "task_uid": 1, "made_against_version": 1,
         "fields": {SCRIPT: 1}, "submitted_at": "2026-10-29T10:00:00-05:00", "submitted_by": f"{SCRIPT} (demo)"},
    ]))
    assert sync(db, FileSource(tmp_path / "updates.jsonl"), clock=CLOCK).applied == 1
    client = local(create_app(db, clock=CLOCK))
    pages = {route: client.get(route).text for route in PAGES}
    for route, html in pages.items():
        assert "<script" not in html.lower(), route
    assert pages["/tasks"].count(escaped) == 3  # the project name, the task name and the note
    assert f"] {escaped}</td>" in pages["/tasks"]
    assert pages["/conflicts"].count(escaped) == 4  # the header, the submitter, and twice in the detail
    assert f"{escaped} (demo)" in pages["/conflicts"]


def test_no_template_or_report_module_turns_escaping_off():
    for path in sorted((REPORT / "templates").glob("*.html")):
        text = path.read_text()
        assert "|safe" not in text.replace(" ", "") and "autoescape" not in text, path
    assert "Markup" not in (REPORT / "app.py").read_text()


def test_overdue_is_due_before_today_in_chicago_and_not_complete(tmp_path):
    db = project_db(tmp_path, "".join([
        task(1, "Due yesterday, half done", "2026-10-29T17:00:00", 50),
        task(2, "Due yesterday, done", "2026-10-29T17:00:00", 100, "2026-10-29T16:00:00"),
        task(3, "Due today", "2026-10-30T17:00:00"),
        task(4, "Due next week", "2026-11-06T17:00:00"),
    ]))

    def overdue(at: datetime) -> list[str]:
        html = local(create_app(db, clock=fixed_clock(at))).get("/overdue").text
        return [cells[0] for cells in rows(html) if cells]

    assert overdue(datetime(2026, 10, 30, 17, 0, tzinfo=UTC)) == ["1"]  # 12:00 on 2026-10-30 in Chicago
    assert overdue(datetime(2026, 10, 31, 4, 59, tzinfo=UTC)) == ["1"]  # 23:59 on 2026-10-30 in Chicago
    assert overdue(datetime(2026, 10, 31, 5, 0, tzinfo=UTC)) == ["1", "3"]  # midnight: task 3 was due yesterday
    assert overdue(datetime(2026, 11, 9, 18, 0, tzinfo=UTC)) == ["1", "3", "4"]


def variance(db: Path) -> list[tuple[str, str, str]]:
    """The baseline /tasks names, and each task's baseline finish and finish variance."""
    html = local(create_app(db, clock=CLOCK)).get("/tasks").text
    (number,) = re.findall(r"minus baseline (\S+)'s", html)
    return [(number, cells[6], cells[8]) for cells in rows(html) if cells]


def test_variance_uses_the_newest_baseline_rebaseline_added_else_baseline_0(tmp_path):
    # Baseline 0 is the one the file tracks; Baseline 3 is an older archive in a higher slot.
    db = project_db(tmp_path, task(1, "Framing", "2026-10-10T17:00:00",
                                   baselines={0: "2026-10-08T17:00:00", 3: "2026-10-05T17:00:00"}))
    with closing(open_db(db)) as conn:  # locked or not, an imported baseline in a higher slot is never used
        lock_baseline(conn, 0, now=IMPORTED)
        lock_baseline(conn, 3, now=IMPORTED)
    assert variance(db) == [("0", "2026-10-08T17:00:00", "+2 d")]  # not baseline 3's +5 d
    with closing(open_db(db)) as conn:
        assert rebaseline(conn, confirm=True, reason="Owner approved the new dates", now=IMPORTED) == 4
    assert variance(db) == [("4", "2026-10-10T17:00:00", "0 d")]
    with closing(open_db(db)) as conn:
        assert rebaseline(conn, confirm=True, reason="Second change", now=IMPORTED) == 5
    assert variance(db) == [("5", "2026-10-10T17:00:00", "0 d")]


def test_variance_uses_no_baseline_when_only_a_higher_numbered_one_was_imported(tmp_path):
    db = project_db(tmp_path, task(1, "Framing", "2026-10-10T17:00:00", baselines={3: "2026-10-05T17:00:00"}))
    assert variance(db) == [("(none)", "", "")]
    milestones = local(create_app(db, clock=CLOCK)).get("/milestones").text
    assert "minus baseline (none)'s" in milestones


def test_overdue_lists_leaf_tasks_only_and_tasks_keeps_the_summary_marked(tmp_path):
    db = project_db(tmp_path, "".join([
        task(1, "Phase one", "2026-10-29T17:00:00", 50, summary=True),
        task(2, "Due yesterday, not started", "2026-10-29T17:00:00"),
        task(3, "Due yesterday, done", "2026-10-29T17:00:00", 100, "2026-10-29T16:00:00"),
    ]))
    client = local(create_app(db, clock=CLOCK))
    assert [cells[0] for cells in rows(client.get("/overdue").text) if cells] == ["2"]
    assert row(client.get("/tasks").text, 1)[2] == "Phase one (summary)"
    facts = dict(re.findall(r"<tr><th>(.*?)</th><td>(.*?)</td></tr>", client.get("/").text))
    assert facts["Overdue"] == '<a href="/overdue">1</a>'  # the count matches the list


def test_a_project_with_no_name_shows_no_name(tmp_path):
    db = project_db(tmp_path, task(1, "Framing", "2026-10-10T17:00:00"), name=None)
    client = local(create_app(db, clock=CLOCK))
    for route in PAGES:
        html = client.get(route).text
        assert "Read-only report for (no name)." in html and "None" not in html, route
    facts = dict(re.findall(r"<tr><th>(.*?)</th><td>(.*?)</td></tr>", client.get("/").text))
    assert facts["Project"] == "(no name)"


def test_the_report_binds_127_0_0_1_only(week_44, monkeypatch):
    assert server(create_app(week_44), 0).config.host == "127.0.0.1"
    served = []
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: served.append(self))
    assert cli.main(["report", "--db", str(week_44), "--port", "8124"]) == 0
    assert [(s.config.host, s.config.port) for s in served] == [("127.0.0.1", 8124)]
    with pytest.raises(SystemExit):  # and there is no option to listen anywhere else
        cli.build_parser().parse_args(["report", "--db", str(week_44), "--host", "0.0.0.0"])


def test_a_get_of_every_route_leaves_the_database_file_byte_identical(week_44):
    files = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in week_44.parent.iterdir()}
    client = local(create_app(week_44, clock=CLOCK))
    for route in PAGES:
        assert client.get(route).status_code == 200
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in week_44.parent.iterdir()} == files


# Stopping: the installed command in a subprocess, stopped by a signal once it listens.

STOP_SIGNALS = [signal.SIGINT, signal.SIGTERM, signal.SIGHUP]


def start_with(sighup: signal.Handlers = signal.SIG_DFL) -> None:
    """In the child, before exec: SIGINT at its default handler, as a terminal's Ctrl+C finds it, and SIGHUP as given.

    So the tests see the same thing when they run with either one ignored (a background job, nohup).
    """
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGHUP, sighup)


def read_until(process: subprocess.Popen, pattern: bytes) -> tuple[re.Match | None, bytes]:
    """Read the child's stderr until `pattern` shows; the match is None if it ended or a minute passed first."""
    err = b""
    deadline = time.monotonic() + 60
    with selectors.DefaultSelector() as selector:
        selector.register(process.stderr, selectors.EVENT_READ)
        while not (found := re.search(pattern, err)):
            chunk = os.read(process.stderr.fileno(), 65536) if selector.select(deadline - time.monotonic()) else b""
            if not chunk:
                return None, err
            err += chunk
    return found, err


class Served:
    """The installed schedule-guard with `args` and --port 0, once it listens and has served one GET of /.

    Waiting reads the line uvicorn logs once its socket listens, so nothing sleeps.
    """

    def __init__(self, *args: str, env: dict[str, str] | None = None, sighup: signal.Handlers = signal.SIG_DFL):
        script = shutil.which("schedule-guard", path=sysconfig.get_path("scripts"))
        assert script, "schedule-guard console script is not installed"
        self.process = subprocess.Popen(
            [script, *args, "--port", "0"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
            preexec_fn=functools.partial(start_with, sighup),
        )
        found, self.err = read_until(self.process, rb"Uvicorn running on http://127\.0\.0\.1:(\d+)")
        if found is None:
            self.process.kill()
            pytest.fail(f"schedule-guard {' '.join(args)} never listened: {self.stop(signal.SIGKILL)}")
        self.port = int(found[1])
        try:
            status = self.get()
        except Exception as exc:
            status = repr(exc)
        if status != 200:
            self.process.kill()
            pytest.fail(f"schedule-guard {' '.join(args)} answered GET / with {status}: {self.stop(signal.SIGKILL)}")

    def __enter__(self) -> "Served":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.process.kill()  # no-op once it has ended; a failed assert must not leave a server running
        self.process.wait()

    def get(self) -> int:
        """GET / on a new connection; return the status."""
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        try:
            connection.request("GET", "/")
            return connection.getresponse().status
        finally:
            connection.close()

    def stop(self, signum: int) -> tuple[int, str]:
        """Send `signum`; return the exit code and all of stderr once it ends."""
        if self.process.returncode is None:
            self.process.send_signal(signum)
        try:
            _, err = self.process.communicate(timeout=60)
        finally:
            self.process.kill()
        return self.process.returncode, (self.err + err).decode()


@pytest.mark.parametrize("signum", STOP_SIGNALS, ids=lambda signum: signum.name)
def test_cli_report_stops_on_ctrl_c_sigterm_or_sighup_with_exit_0_and_no_traceback(week_44, signum):
    code, err = Served("report", "--db", str(week_44)).stop(signum)
    assert code == 0, err
    assert "Traceback" not in err and "KeyboardInterrupt" not in err, err
    assert "Finished server process" in err  # uvicorn shut down on its own, not killed


@pytest.mark.parametrize("command", ["report", "demo"])
def test_cli_keeps_serving_after_a_sighup_it_started_with_ignored(week_44, tmp_path, command):
    args = ["report", "--db", str(week_44)] if command == "report" else ["demo", "--db", str(tmp_path / "demo.db")]
    with Served(*args, sighup=signal.SIG_IGN) as served:
        status = Path(f"/proc/{served.process.pid}/status").read_text()
        sighup_bit = 1 << (signal.SIGHUP - 1)
        assert int(re.search(r"SigIgn:\s*(\w+)", status)[1], 16) & sighup_bit  # no handler took it, so nothing races
        served.process.send_signal(signal.SIGHUP)
        assert served.get() == 200
        code, err = served.stop(signal.SIGINT)
    assert code == 0 and "Traceback" not in err, err
    assert "Finished server process" in err


# A child that runs cli.main up to a point before the server listens and waits there, so a signal can't land too late.

WAIT_IN = """
import sys
import time
from schedule_guard import cli


def wait(*args, **kwargs):
    print("waiting", file=sys.stderr, flush=True)
    while True:
        time.sleep(0.01)


class WaitOnImport:
    def find_spec(self, name, path=None, target=None):
        if name == "schedule_guard.report.app":
            wait()


match sys.argv[1]:
    case "build":
        cli.build_demo = wait
    case "import":
        sys.meta_path.insert(0, WaitOnImport())
    case "create_app":
        from schedule_guard.report import app
        app.create_app = wait
raise SystemExit(cli.main(sys.argv[2:]))
"""
BEFORE_LISTENING = [("report", "import"), ("report", "create_app"), ("demo", "build"), ("demo", "import"),
                    ("demo", "create_app")]


@pytest.mark.parametrize("signum", STOP_SIGNALS, ids=lambda signum: signum.name)
@pytest.mark.parametrize(("command", "point"), BEFORE_LISTENING)
def test_cli_stopped_before_it_listens_prints_one_line_and_exits_130(week_44, tmp_path, command, point, signum):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    args = ["report", "--db", str(week_44)] if command == "report" else ["demo"]
    child = subprocess.Popen(
        [sys.executable, "-c", WAIT_IN, point, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "TMPDIR": str(scratch)}, preexec_fn=start_with,
    )
    try:
        found, before = read_until(child, rb"waiting\n")
        assert found, before
        assert before == b"waiting\n"  # and uvicorn has logged nothing, so it hasn't started
        if command == "demo":
            (directory,) = scratch.iterdir()
            assert directory.name.startswith("schedule-guard-demo-")
        child.send_signal(signum)
        _, err = child.communicate(timeout=60)
    finally:
        child.kill()
    assert child.returncode == 130, err
    assert err == b"schedule-guard: stopped before the report started serving\n"
    assert list(scratch.iterdir()) == []  # the demo's directory is gone


@pytest.mark.parametrize(("started", "code"), [(True, 0), (False, 130)], ids=["listened", "never-listened"])
def test_cli_report_exits_0_only_if_the_server_listened(week_44, monkeypatch, capsys, started, code):
    def run(self, sockets=None):
        self.started = started
        raise KeyboardInterrupt  # what uvicorn's run raises after a stop signal

    monkeypatch.setattr(uvicorn.Server, "run", run)
    handlers = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP)
    assert cli.main(["report", "--db", str(week_44)]) == code
    assert capsys.readouterr().err.count("stopped before") == (0 if started else 1)
    assert (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP)) == handlers  # put back


@pytest.mark.parametrize("first_get", ["not-200", "raises"])
def test_served_kills_its_child_when_the_first_get_fails(week_44, monkeypatch, first_get):
    children = []
    real_popen = subprocess.Popen

    def popen(*args, **kwargs):
        children.append(real_popen(*args, **kwargs))
        return children[-1]

    def get_response(self):
        if first_get == "raises":
            raise ConnectionResetError("the server hung up")
        return types.SimpleNamespace(status=500)

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(http.client.HTTPConnection, "getresponse", get_response)
    try:
        with pytest.raises((pytest.fail.Exception, AssertionError, ConnectionResetError)):
            Served("report", "--db", str(week_44))
        assert children[0].poll() == -signal.SIGKILL
    finally:
        children[0].kill()
        children[0].communicate()


def test_served_kills_its_child_when_a_test_fails_inside_it(week_44):
    with pytest.raises(AssertionError):
        with Served("report", "--db", str(week_44)) as served:
            raise AssertionError
    assert served.process.poll() == -signal.SIGKILL


# A repeat stop within cli.REPEAT_WINDOW of the first is the same stop: `uv run` hands a terminal's one Ctrl+C to
# the command twice, about 0.2 s apart.


@pytest.mark.parametrize("command", ["report", "demo"])
def test_cli_two_sigints_0_2_s_apart_after_it_listens_stop_it_once_with_exit_0(week_44, tmp_path, command):
    args = ["report", "--db", str(week_44)] if command == "report" else ["demo"]
    with Served(*args, env={**os.environ, "TMPDIR": str(tmp_path)}) as served:
        served.process.send_signal(signal.SIGINT)
        time.sleep(0.2)  # when uv run's second one comes
        code, err = served.stop(signal.SIGINT)
    assert code == 0, err
    assert "Traceback" not in err and "stopped before" not in err, err
    assert "Finished server process" in err
    assert list(tmp_path.iterdir()) == []  # and the demo's directory is gone


def test_cli_two_sigints_0_2_s_apart_before_it_listens_print_one_line_and_exit_130(tmp_path):
    child = subprocess.Popen(
        [sys.executable, "-c", WAIT_IN, "build", "demo"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "TMPDIR": str(tmp_path)}, preexec_fn=start_with,
    )
    try:
        found, before = read_until(child, rb"waiting\n")
        assert found, before
        child.send_signal(signal.SIGINT)
        time.sleep(0.2)
        child.send_signal(signal.SIGINT)  # skipped if it has already ended
        _, err = child.communicate(timeout=60)
    finally:
        child.kill()
    assert child.returncode == 130, err
    assert err == b"schedule-guard: stopped before the report started serving\n"
    assert list(tmp_path.iterdir()) == []


SECOND_STOP_AT_REMOVAL = """
import signal
import sys
import tempfile
import time
from schedule_guard import cli

real_rmtree = tempfile.TemporaryDirectory._rmtree  # cleanup() detaches the exit-time finalizer, then calls this
raised = []


def rmtree(name, *args, **kwargs):
    if not raised:
        raised.append(True)
        signal.raise_signal(signal.SIGINT)  # a second stop, just as the demo's directory is removed
    real_rmtree(name, *args, **kwargs)


def wait(*args, **kwargs):
    print("waiting", file=sys.stderr, flush=True)
    while True:
        time.sleep(0.01)


tempfile.TemporaryDirectory._rmtree = staticmethod(rmtree)
if sys.argv[1] == "before-listening":
    cli.build_demo = wait
if len(sys.argv) > 2:
    cli.REPEAT_WINDOW = float(sys.argv[2])  # 0: every stop after the first is a later one, which forces the exit
raise SystemExit(cli.main(["demo", "--port", "0"]))
"""


@pytest.mark.parametrize("when", ["before-listening", "after-listening"])
def test_cli_demo_removes_its_directory_even_when_a_second_stop_comes_as_it_does(tmp_path, when):
    child = subprocess.Popen(
        [sys.executable, "-c", SECOND_STOP_AT_REMOVAL, when], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "TMPDIR": str(tmp_path)}, preexec_fn=start_with,
    )
    try:
        found, before = read_until(child, rb"waiting\n" if when == "before-listening" else rb"Uvicorn running on")
        assert found, before
        child.send_signal(signal.SIGINT)
        _, err = child.communicate(timeout=60)
    finally:
        child.kill()
    text = (before + err).decode()
    assert list(tmp_path.iterdir()) == []  # demo.db, its lock file and the directory are all gone
    assert "Traceback" not in text, text
    if when == "before-listening":
        assert (child.returncode, err) == (130, b"schedule-guard: stopped before the report started serving\n")
    else:
        assert child.returncode == 0 and "Finished server process" in text and "stopped before" not in text, text


@pytest.mark.parametrize("when", ["before-listening", "after-listening"])
def test_cli_demo_removes_its_directory_even_when_a_later_stop_comes_as_it_does(tmp_path, when):
    child = subprocess.Popen(
        [sys.executable, "-c", SECOND_STOP_AT_REMOVAL, when, "0"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "TMPDIR": str(tmp_path)}, preexec_fn=start_with,
    )
    try:
        found, before = read_until(child, rb"waiting\n" if when == "before-listening" else rb"Uvicorn running on")
        assert found, before
        child.send_signal(signal.SIGINT)
        _, err = child.communicate(timeout=60)
    finally:
        child.kill()
    text = (before + err).decode()
    assert list(tmp_path.iterdir()) == []  # the stop waited for the removal, though no window dropped it
    assert "Traceback" not in text, text
    assert child.returncode in (0, 130), text  # not ended by the signal; the later stop forced the exit


@pytest.mark.parametrize("command", ["report", "demo"])
def test_cli_a_second_sigint_as_it_shuts_down_is_the_same_stop_exit_0_and_no_traceback(week_44, tmp_path, command):
    args = ["report", "--db", str(week_44)] if command == "report" else ["demo"]
    with Served(*args, env={**os.environ, "TMPDIR": str(tmp_path)}) as served:
        served.process.send_signal(signal.SIGINT)
        found, err = read_until(served.process, rb"Shutting down")
        served.err += err
        assert found and served.process.poll() is None, served.err.decode()  # uvicorn sleeps 0.1 s after the line
        code, err = served.stop(signal.SIGINT)  # without the window, uvicorn would take it to force the exit
    assert code == 0, err
    assert "Traceback" not in err and "stopped before" not in err, err
    assert "Finished server process" in err
    assert list(tmp_path.iterdir()) == []  # and the demo's directory is gone


# WAIT_IN with a second SIGINT raised as the child writes its one line, so the first stop is still being handled.
SECOND_STOP_AT_THE_LINE = """
import signal
import sys


class Stderr:
    def __init__(self, stream):
        self.stream = stream
        self.raised = False

    def write(self, text):
        if not self.raised and text.startswith("schedule-guard: stopped"):
            self.raised = True
            signal.raise_signal(signal.SIGINT)
        return self.stream.write(text)

    def __getattr__(self, name):
        return getattr(self.stream, name)


sys.stderr = Stderr(sys.stderr)
""" + WAIT_IN


@pytest.mark.parametrize(("command", "point"), [("report", "create_app"), ("demo", "build")])
def test_cli_a_second_sigint_as_it_stops_before_it_listens_keeps_one_line_and_exit_130(week_44, tmp_path, command,
                                                                                       point):
    args = ["report", "--db", str(week_44)] if command == "report" else ["demo"]
    child = subprocess.Popen(
        [sys.executable, "-c", SECOND_STOP_AT_THE_LINE, point, *args], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env={**os.environ, "TMPDIR": str(tmp_path)}, preexec_fn=start_with,
    )
    try:
        found, before = read_until(child, rb"waiting\n")
        assert found, before
        child.send_signal(signal.SIGINT)
        _, err = child.communicate(timeout=60)
    finally:
        child.kill()
    assert child.returncode == 130, err
    assert err == b"schedule-guard: stopped before the report started serving\n"
    assert list(tmp_path.iterdir()) == []


# The demo, then one more stop signal raised once cli.main has returned from the first, as `uv run`'s repeat can land.
STOP_AFTER_IT_RETURNED = """
import signal
import sys
import time
from schedule_guard import cli


def wait(*args, **kwargs):
    print("waiting", file=sys.stderr, flush=True)
    while True:
        time.sleep(0.01)


if sys.argv[1] == "before-listening":
    cli.build_demo = wait
code = cli.main(["demo", "--port", "0"])
signal.raise_signal(signal.Signals[sys.argv[2]])
raise SystemExit(code)
"""


@pytest.mark.parametrize("signum", STOP_SIGNALS, ids=lambda signum: signum.name)
@pytest.mark.parametrize(("when", "code"), [("before-listening", 130), ("after-listening", 0)],
                         ids=["before-listening", "after-listening"])
def test_cli_a_stop_after_the_command_returned_from_one_doesnt_end_the_process(tmp_path, when, code, signum):
    child = subprocess.Popen(
        [sys.executable, "-c", STOP_AFTER_IT_RETURNED, when, signum.name], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env={**os.environ, "TMPDIR": str(tmp_path)}, preexec_fn=start_with,
    )
    try:
        found, before = read_until(child, rb"waiting\n" if when == "before-listening" else rb"Uvicorn running on")
        assert found, before
        child.send_signal(signum)
        _, err = child.communicate(timeout=60)
    finally:
        child.kill()
    text = (before + err).decode()
    assert child.returncode == code, text  # it ignored the signal; the default would end the process by it
    assert "Traceback" not in text, text
    assert list(tmp_path.iterdir()) == []


def test_a_stop_within_1_s_of_the_first_is_a_repeat_and_a_later_one_is_not(monkeypatch):
    now = iter([100.0, 100.2, 100.99, 101.0, 105.0])
    monkeypatch.setattr(cli, "time", types.SimpleNamespace(monotonic=lambda: next(now)))
    monkeypatch.setattr(cli, "_first_stop", None)
    assert [cli._repeat_stop() for _ in range(5)] == [False, True, True, False, False]
