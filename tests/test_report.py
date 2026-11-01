"""The read-only report: GET-only pages read from the database (PLAN.md section 7)."""

import ast
import hashlib
import json
import re
import warnings
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
import uvicorn

from schedule_guard import cli
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


def project_db(tmp_path, tasks: str, *, name: str = "Small project") -> Path:
    """A database imported from a small MSPDI file with the given <Task> elements."""
    xml = tmp_path / "small.xml"
    xml.write_text(
        f'<Project xmlns="http://schemas.microsoft.com/project"><Name>{name}</Name><Tasks>{tasks}</Tasks></Project>'
    )
    db = tmp_path / "small.db"
    with closing(open_db(db, create=True)) as conn:
        import_file(conn, xml, now=IMPORTED)
    return db


def task(uid: int, name: str, finish: str, percent: int = 0, actual_finish: str | None = None) -> str:
    done = f"<ActualFinish>{actual_finish}</ActualFinish>" if actual_finish else ""
    return (
        f"<Task><UID>{uid}</UID><Name>{name}</Name><Summary>0</Summary><Milestone>0</Milestone>"
        f"<Start>2026-10-01T08:00:00</Start><Finish>{finish}</Finish><PercentComplete>{percent}</PercentComplete>"
        f"{done}</Task>"
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
