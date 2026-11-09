"""Shared fixtures: databases built from the Riverbend Clinic fixture, and the Hypothesis profile."""

import shutil
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from hypothesis import settings

from schedule_guard.importer import import_file
from schedule_guard.store.db import open_db

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "riverbend-clinic.xml"
IMPORTED = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)

# The same examples on every run and every machine, no per-example deadline
# for a slow CI runner, and no example database written anywhere.
settings.register_profile("schedule-guard", max_examples=40, deadline=None, derandomize=True, database=None)
settings.load_profile("schedule-guard")


@pytest.fixture(scope="session")
def fixture_db_template(tmp_path_factory) -> Path:
    """The fixture imported once into a database under the session's temp directory."""
    path = tmp_path_factory.mktemp("template") / "riverbend.db"
    with closing(open_db(path, create=True)) as conn:
        import_file(conn, FIXTURE, now=IMPORTED)
    return path


@pytest.fixture(scope="session")
def fresh_db(fixture_db_template, tmp_path_factory):
    """A function that copies the imported fixture into a new temp file and returns its path."""

    def make() -> Path:
        path = tmp_path_factory.mktemp("db") / "riverbend.db"
        shutil.copyfile(fixture_db_template, path)
        return path

    return make


@pytest.fixture
def fixture_db(fresh_db) -> Path:
    return fresh_db()
