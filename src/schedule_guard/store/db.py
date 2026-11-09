"""Open a schedule-guard database and set up its schema, or open one read-only for the report.

Connections keep SQLite's defaults (triggers on, recursive_triggers OFF);
the schema's protection doesn't depend on any pragma. They run in
autocommit mode, and every write goes through `transaction`, which holds
the write lock from BEGIN IMMEDIATE to COMMIT.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# Version 1 (the M2 build) had no task_progress, version 2 (M3) no sync
# tables, and version 3 (M4 as first built) no 'interrupted' run status.
# Nothing was released, so there is no upgrade from any of them: such a file
# is refused like any other version, not opened to fail at its first sync.
SCHEMA_VERSION = 4
SCHEMA = Path(__file__).with_name("schema.sql")


class StoreError(Exception):
    """The database can't be opened, or isn't a schedule-guard database."""


def open_db(path: str | Path, *, create: bool = False) -> sqlite3.Connection:
    """Open the database at `path`, setting up the schema in a new, empty file.

    Without `create`, a missing file is refused rather than created, so a
    mistyped path leaves nothing behind.
    """
    uri = f"{Path(path).absolute().as_uri()}?mode={'rwc' if create else 'rw'}"
    try:
        conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    except sqlite3.Error as exc:
        raise StoreError(f"can't open database {path}: {exc}") from exc
    try:
        migrate(conn)
    except sqlite3.DatabaseError as exc:  # e.g. "file is not a database"
        conn.close()
        raise StoreError(f"can't use database {path}: {exc}") from exc
    except BaseException:
        conn.close()
        raise
    return conn


def open_db_read_only(path: str | Path) -> sqlite3.Connection:
    """Open an existing database for reading only, as the report does.

    The file is opened with mode=ro, so SQLite refuses every write on the
    connection, and query_only is set too. Nothing is set up or migrated:
    a missing file, or one at another schema version, is refused.
    """
    try:
        conn = sqlite3.connect(f"{Path(path).absolute().as_uri()}?mode=ro", uri=True, isolation_level=None)
    except sqlite3.Error as exc:
        raise StoreError(f"can't open database {path}: {exc}") from exc
    try:
        conn.execute("PRAGMA query_only = ON")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.DatabaseError as exc:  # e.g. "file is not a database"
        conn.close()
        raise StoreError(f"can't use database {path}: {exc}") from exc
    if version != SCHEMA_VERSION:
        conn.close()
        raise StoreError(f"database schema version {version}; this schedule-guard uses {SCHEMA_VERSION}")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Bring the schema to SCHEMA_VERSION. Only an empty file is set up."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == SCHEMA_VERSION:
        return
    if version != 0:
        raise StoreError(f"database schema version {version}; this schedule-guard uses {SCHEMA_VERSION}")
    if conn.execute("SELECT 1 FROM sqlite_schema").fetchone():
        raise StoreError("not a schedule-guard database: it already holds other tables")
    conn.executescript(
        f"BEGIN IMMEDIATE;\n{SCHEMA.read_text(encoding='utf-8')}\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;"
    )


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT, or ROLLBACK if the block raises."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
