"""Command-line entry point: `schedule-guard`."""

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from schedule_guard import __version__
from schedule_guard.baselines import BaselineRefused, lock_baseline, rebaseline
from schedule_guard.importer import ImportRefused, import_source, read_source
from schedule_guard.mspdi.parse import MspdiError
from schedule_guard.store.db import StoreError, open_db

REFUSALS = (MspdiError, StoreError, ImportRefused, BaselineRefused, OSError)  # OSError: e.g. a missing file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="schedule-guard",
        description="Keep field progress updates from overwriting a master schedule or baseline.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    db = argparse.ArgumentParser(add_help=False)
    db.add_argument(
        "--db", required=True, type=Path, metavar="PATH",
        help="the schedule-guard database; keep it outside the repo or in a gitignored path",
    )

    imp = commands.add_parser("import", parents=[db], help="store an MSPDI file in a new, empty database")
    imp.add_argument("file", type=Path, help="the MSPDI (MS Project XML) file")
    imp.set_defaults(run=_import)

    baseline = commands.add_parser("baseline", help="lock an imported baseline")
    baseline_commands = baseline.add_subparsers(dest="baseline_command", required=True, metavar="COMMAND")
    lock = baseline_commands.add_parser(
        "lock", parents=[db], help="lock an imported baseline; a locked baseline is read-only"
    )
    lock.add_argument("--number", type=int, default=0, metavar="N", help="baseline number (default: 0)")
    lock.set_defaults(run=_lock)

    reb = commands.add_parser(
        "rebaseline", parents=[db], help="append a new locked baseline from the current planned values"
    )
    reb.add_argument("--confirm", action="store_true", help="required: the new baseline can never be removed")
    reb.add_argument("--reason", metavar="TEXT", help="required: why the baseline is being reset")
    reb.set_defaults(run=_rebaseline)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.run(args)
    except REFUSALS as exc:
        print(f"schedule-guard: error: {exc}", file=sys.stderr)
        return 1


def _now() -> datetime:
    return datetime.now(UTC)


def _printable(text: str) -> str:
    """`text` with every character str.isprintable() rejects escaped the way !r does.

    That covers C0, DEL and C1 controls, format characters such as U+202E,
    line and paragraph separators, and the lone surrogates a non-UTF-8 file
    name decodes to, so a file can't drive or reorder the terminal.
    """
    return "".join(ch if ch.isprintable() else repr(ch)[1:-1] for ch in text)


def _import(args: argparse.Namespace) -> int:
    source = read_source(args.file)  # a refused file leaves no database file behind
    conn = open_db(args.db, create=True)
    try:
        result = import_source(conn, source, now=_now())
    finally:
        conn.close()
    numbers = ", ".join(map(str, result.baselines)) or "none"
    name = _printable(result.name or args.file.name)
    print(f"imported {name}: {result.tasks} tasks, baselines {numbers} (unlocked), sha256 {result.source_sha256}")
    return 0


def _lock(args: argparse.Namespace) -> int:
    conn = open_db(args.db)
    try:
        lock_baseline(conn, args.number, now=_now())
    finally:
        conn.close()
    print(f"locked baseline {args.number}")
    return 0


def _rebaseline(args: argparse.Namespace) -> int:
    conn = open_db(args.db)
    try:
        number = rebaseline(conn, confirm=args.confirm, reason=args.reason, now=_now())
    finally:
        conn.close()
    print(f"added baseline {number} from the current planned values and locked it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
