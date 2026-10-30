"""Open a schedule-guard database and set up its schema.

Connections keep SQLite's defaults (triggers on, recursive_triggers OFF);
the schema's protection doesn't depend on any pragma. They run in
autocommit mode, and every write goes through `transaction`, which holds
the write lock from BEGIN IMMEDIATE to COMMIT.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1
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
