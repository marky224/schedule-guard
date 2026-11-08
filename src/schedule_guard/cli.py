"""Command-line entry point: `schedule-guard`."""

import argparse
import signal
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path

from schedule_guard import __version__
from schedule_guard.baselines import BaselineRefused, lock_baseline, rebaseline
from schedule_guard.clock import Clock
from schedule_guard.demo import DEMO_CLOCK, DemoRefused, build_demo
from schedule_guard.importer import ImportRefused, import_source, read_source
from schedule_guard.mspdi.parse import MspdiError
from schedule_guard.review import ReviewRefused, dismiss, list_conflicts
from schedule_guard.sources.file import FileSource
from schedule_guard.store.db import StoreError, open_db
from schedule_guard.sync import RunResult, SyncRefused, list_runs, rerun, sync

# A refusal prints one error line and exits 1; OSError covers a missing file.
REFUSALS = (MspdiError, StoreError, ImportRefused, BaselineRefused, SyncRefused, ReviewRefused, DemoRefused, OSError)


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

    sync_parser = commands.add_parser("sync", help="run progress updates through the guard")
    sync_commands = sync_parser.add_subparsers(dest="sync_command", required=True, metavar="COMMAND")
    for name, help_text in (("now", "run a sync now"), ("daily", "the same, recorded as the daily run")):
        run = sync_commands.add_parser(name, parents=[db], help=help_text)
        run.add_argument(
            "--source", required=True, metavar="file:PATH", help="where the updates come from: a JSON Lines file"
        )
        run.set_defaults(run=_sync, trigger=name)

    again = commands.add_parser("rerun", parents=[db], help="process again what a run captured, as a new run")
    again.add_argument("run_id", metavar="RUN_ID")
    again.set_defaults(run=_rerun)

    runs = commands.add_parser("runs", help="list sync runs")
    runs_commands = runs.add_subparsers(dest="runs_command", required=True, metavar="COMMAND")
    runs_commands.add_parser("list", parents=[db], help="every run, oldest first").set_defaults(run=_runs_list)

    conflicts = commands.add_parser("conflicts", help="review conflicts: list them, dismiss one with a note")
    conflict_commands = conflicts.add_subparsers(dest="conflicts_command", required=True, metavar="COMMAND")
    listing = conflict_commands.add_parser("list", parents=[db], help="open conflicts")
    listing.add_argument("--all", action="store_true", help="dismissed ones too")
    listing.set_defaults(run=_conflicts_list)
    dismissal = conflict_commands.add_parser(
        "dismiss", parents=[db], help="dismiss an open conflict; the update is never applied"
    )
    dismissal.add_argument("id", type=int, metavar="ID")
    dismissal.add_argument("--note", metavar="TEXT", help="required: why it is dismissed")
    dismissal.set_defaults(run=_conflicts_dismiss)

    report = commands.add_parser("report", parents=[db], help="serve the read-only HTML report on 127.0.0.1")
    report.add_argument(
        "--port", type=_port, default=8000, metavar="PORT", help="the port on 127.0.0.1 (default: 8000)"
    )
    report.set_defaults(run=_serve_report)

    demo = commands.add_parser("demo", help="build a new demo database from the fixture and serve the report on it")
    demo.add_argument(
        "--db", type=Path, metavar="PATH",
        help="where to build it; it must not exist (default: a temporary directory, removed when the demo"
        " stops on Ctrl+C, SIGTERM or SIGHUP; a SIGKILL leaves it)",
    )
    demo.add_argument(
        "--port", type=_port, default=8000, metavar="PORT", help="the port on 127.0.0.1 (default: 8000)"
    )
    demo.set_defaults(run=_demo)
    return parser


def _port(text: str) -> int:
    try:
        port = int(text)
    except ValueError:
        port = -1
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"not a port from 0 to 65535: {text!r}")
    return port


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


def _sync(args: argparse.Namespace) -> int:
    kind, _, path = args.source.partition(":")
    if kind != "file" or not path:
        raise SyncRefused(f"the source must be file:<path>, not {args.source!r}")
    return _report(sync(args.db, FileSource(path), trigger=args.trigger, clock=_now))


def _rerun(args: argparse.Namespace) -> int:
    return _report(rerun(args.db, args.run_id, clock=_now))


def _report(result: RunResult) -> int:
    print(_run_line(result))
    if result.status != "ok":
        print(f"schedule-guard: error: run {result.run_id} failed: {_printable(result.error or '')}", file=sys.stderr)
        return 1
    return 0


def _run_line(run: RunResult) -> str:
    line = (
        f"run {run.run_id} {run.trigger} {run.status}: {run.seen} seen, {run.applied} applied,"
        f" {run.conflicts} conflicts, {run.duplicates} duplicates"
    )
    if run.rerun_of:
        line += f"; rerun of {run.rerun_of}"
    return line


def _runs_list(args: argparse.Namespace) -> int:
    conn = open_db(args.db)
    try:
        runs = list_runs(conn)
    finally:
        conn.close()
    for run in runs:
        error = f"; error: {_printable(run.error)}" if run.error else ""
        print(f"{run.started_at} {_run_line(run)}{error}")
    if not runs:
        print("no runs yet")
    return 0


def _conflicts_list(args: argparse.Namespace) -> int:
    conn = open_db(args.db)
    try:
        conflicts = list_conflicts(conn, include_dismissed=args.all)
    finally:
        conn.close()
    for c in conflicts:
        task = "-" if c.task_uid is None else c.task_uid
        line = (
            f"{c.id} {c.status} {c.reason}: task {task}, item {c.source}:{_printable(c.source_item_id)}"
            f" version {_printable(c.item_version)}: {_printable(c.detail or '')}"
        )
        if c.note:
            line += f"; note: {_printable(c.note)}"
        print(line)
    if not conflicts:
        print("no conflicts" if args.all else "no open conflicts")
    return 0


def _conflicts_dismiss(args: argparse.Namespace) -> int:
    conn = open_db(args.db)
    try:
        dismiss(conn, args.id, args.note, now=_now())
    finally:
        conn.close()
    print(f"dismissed conflict {args.id}; the update stays unapplied")
    return 0


STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
REPEAT_WINDOW = 1.0  # seconds; `uv run` hands a terminal's one Ctrl+C to the command twice, about 0.2 s apart
_first_stop: float | None = None  # when the running report or demo saw its first stop signal


def _repeat_stop() -> bool:
    """Whether this stop signal repeats the first one within REPEAT_WINDOW; the first one starts the window."""
    global _first_stop
    now = time.monotonic()
    if _first_stop is None:
        _first_stop = now
        return False
    return now - _first_stop < REPEAT_WINDOW


def _stop(signum: int, frame: object) -> None:
    """A stop signal while uvicorn's own handlers aren't installed: KeyboardInterrupt, unless it is a repeat."""
    if not _repeat_stop():
        raise KeyboardInterrupt


def _as_sigterm(signum: int, frame: object) -> None:
    """SIGHUP's handler: the same stop as SIGTERM, so uvicorn shuts down for it too."""
    signal.raise_signal(signal.SIGTERM)


def _stoppable(command: Callable[[argparse.Namespace], int]) -> Callable[[argparse.Namespace], int]:
    """Make SIGINT, SIGTERM and SIGHUP stop `command` once, and let cleanup finish.

    uvicorn catches SIGINT and SIGTERM while it serves, shuts down, puts the
    old handlers back and raises the signal again; outside that, SIGINT and
    SIGTERM raise KeyboardInterrupt here, so `finally` and `with` exits still
    run (SIGTERM's default would end the process first). SIGHUP (the
    terminal closed) is raised again as SIGTERM, unless it is already ignored
    (nohup); so is an ignored SIGINT (a background job) left alone.

    A repeat within REPEAT_WINDOW of the first stop is the same stop and is
    dropped, here and in _serve's wrapper of uvicorn's handler, since `uv run`
    delivers a terminal's Ctrl+C twice. A later one forces the exit, as before.
    Once the command has ended after a stop, the three are ignored for the
    rest of the exit: Python puts the default handlers back as it finishes,
    and a repeat landing then would end the process by the signal. A SIGKILL
    can't be caught.

    A stop before the server listens reaches here as KeyboardInterrupt: one
    line on stderr and exit 130.
    """

    @wraps(command)
    def run(args: argparse.Namespace) -> int:
        global _first_stop
        _first_stop = None
        previous = {signal.SIGTERM: signal.signal(signal.SIGTERM, _stop)}
        for signum, handler in ((signal.SIGINT, _stop), (signal.SIGHUP, _as_sigterm)):
            if signal.getsignal(signum) is not signal.SIG_IGN:
                previous[signum] = signal.signal(signum, handler)
        try:
            return command(args)
        except KeyboardInterrupt:
            print("schedule-guard: stopped before the report started serving", file=sys.stderr)
            return 130
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler if _first_stop is None else signal.SIG_IGN)

    return run


@_stoppable
def _serve_report(args: argparse.Namespace) -> int:
    return _serve(args.db, _now, args.port)


@_stoppable
def _demo(args: argparse.Namespace) -> int:
    if args.db is not None:
        return _build_and_serve_demo(args.db, args.port)
    scratch = tempfile.TemporaryDirectory(prefix="schedule-guard-demo-")
    try:
        return _build_and_serve_demo(Path(scratch.name) / "demo.db", args.port)
    finally:
        held = signal.pthread_sigmask(signal.SIG_BLOCK, STOP_SIGNALS)  # a stop now waits until the removal is done
        try:
            scratch.cleanup()
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, held)


def _build_and_serve_demo(db: Path, port: int) -> int:
    runs = build_demo(db)
    print(f"built the demo database {_printable(str(db))}; today is {DEMO_CLOCK().date()} for the demo")
    for run in runs:
        print(_run_line(run))
    return _serve(db, DEMO_CLOCK, port)


def _serve(db: Path, clock: Clock, port: int) -> int:
    """Serve the report on `db` on 127.0.0.1 until a stop signal, then return 0 with no traceback."""
    from schedule_guard.report.app import create_app, server  # FastAPI and uvicorn load only to serve

    report = server(create_app(db, clock=clock), port)
    handle_exit = report.handle_exit

    def handle_stop(signum: int, frame: object) -> None:
        """uvicorn's handler while it serves, minus repeats; it forces the exit on a second SIGINT."""
        if not _repeat_stop():
            handle_exit(signum, frame)

    report.handle_exit = handle_stop
    try:
        report.run()
    except KeyboardInterrupt:  # uvicorn has shut down and raised the stop signal again, after the window
        if not report.started:  # it stopped before it listened
            raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
