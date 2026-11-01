"""The read-only report: GET-only pages read from the database (PLAN.md section 7)."""

import ast
import re
import warnings
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from schedule_guard import cli
from schedule_guard.clock import fixed_clock
from schedule_guard.report.app import CSP, PAGES, create_app
from schedule_guard.review import dismiss
from schedule_guard.sources.file import FileSource
from schedule_guard.store.db import StoreError, open_db
from schedule_guard.sync import sync

with warnings.catch_warnings():
    # Starlette now asks for httpx2; the lock has httpx, the dev dependency the plan names for TestClient.
    warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient` is deprecated")
    from fastapi.testclient import TestClient

CLOCK = fixed_clock(datetime(2026, 10, 30, 17, 0, tzinfo=UTC))  # 12:00 on 2026-10-30 in Chicago
WEEK_44 = Path(__file__).resolve().parent / "fixtures" / "updates" / "week-44.jsonl"
REPORT = Path(__file__).resolve().parent.parent / "src" / "schedule_guard" / "report"


@pytest.fixture
def week_44(fixture_db):
    """The fixture after one run of the week-44 file, with conflict 1 dismissed."""
    sync(fixture_db, FileSource(WEEK_44), clock=CLOCK)
    with closing(open_db(fixture_db)) as conn:
        dismiss(conn, 1, "Resubmit against the current version", now=datetime(2026, 10, 30, 21, tzinfo=UTC))
    return fixture_db


@pytest.fixture
def client(week_44):
    return TestClient(create_app(week_44, clock=CLOCK))


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
