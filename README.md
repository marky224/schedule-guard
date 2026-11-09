# schedule-guard
Guard that keeps field progress updates from overwriting an MS Project master schedule or baseline

## Phase 1

Phase 1 is the offline core: a command-line tool and a read-only report that
run on one machine, from files. It needs no Microsoft 365 account and makes no
network calls. It has been tested on generated, fictional files only, not yet
on a real MS Project export.

### What it guards

Field crews report progress against a schedule. If any path for those reports
can write planned dates or a saved baseline, the schedule and the record of
what was promised can be overwritten without anyone noticing. schedule-guard
imports the schedule from an MSPDI (MS Project XML) file into SQLite and runs
every progress update through a guard, which keeps these rules:

- **The master schedule and the baselines are protected.** Sync never writes
  the planned fields or a saved baseline, and database triggers refuse the
  change a second time. Only `import` and the explicit baseline commands do.
- **Progress fields only.** An update can change % complete, actual start,
  actual finish, remaining work and notes. Anything else is a conflict.
- **Current version only.** Each update names the task version it was made
  against. If that isn't the task's current version, it is a conflict.
- **All or nothing.** One bad field means none of the update's fields apply.
- **Exactly once.** An update is known by its source, item id and item version.
  It and its outcome commit in one SQLite transaction, so running the same file
  again changes nothing.
- **Conflicts are never applied.** A reviewer can only dismiss one with a note,
  and the field resubmits against the current version.
- **Notes are appended.** Each one is added with its date, source and
  submitter; earlier text is never replaced.
- **Files are untrusted.** MSPDI and update files are parsed defensively. A bad
  line in an update file becomes a conflict and doesn't stop the run.

### Install

You need [uv](https://docs.astral.sh/uv/) and Python 3.12. Phase 1 runs on
Linux only: the sync run lock uses `fcntl` (`src/schedule_guard/sync.py`), and
the tests read `/proc`. Windows and macOS are not supported. From a checkout of
this repo:

```sh
uv sync
```

### Commands

Every command but `demo` takes `--db PATH`. Keep the database outside the repo.
These examples run in order from the repo's root, on its fictional fixture:

```sh
DB="$(mktemp -d)/riverbend.db"

# Parse an MSPDI file, then store it in a new, empty database, one project per database.
uv run schedule-guard import --db "$DB" tests/fixtures/riverbend-clinic.xml

# Lock an imported baseline, Baseline 0 unless --number says otherwise. A locked baseline is read-only.
uv run schedule-guard baseline lock --db "$DB"

# Run an update file through the guard now.
uv run schedule-guard sync now --db "$DB" --source file:tests/fixtures/updates/week-44.jsonl

# The same, recorded as the daily run. This file has been read, so every line is a duplicate.
uv run schedule-guard sync daily --db "$DB" --source file:tests/fixtures/updates/week-44.jsonl

# List the runs, oldest first, with their ids and counts.
uv run schedule-guard runs list --db "$DB"

# List the open conflicts; --all adds the dismissed ones.
uv run schedule-guard conflicts list --db "$DB"

# Dismiss a conflict by its id, with a note. The update stays unapplied.
uv run schedule-guard conflicts dismiss --db "$DB" 1 --note "Resubmit against the current version"

# Append a new locked baseline from the current planned values. It can never be removed.
uv run schedule-guard rebaseline --db "$DB" --confirm --reason "Owner approved the new dates"

# Write the imported MSPDI file, with the progress applied, to a new file. The database is only read.
uv run schedule-guard export --db "$DB" --out "$(dirname "$DB")/riverbend-progress.xml"
```

`export` starts from the file the project was imported from and changes only
each task's % complete, actual start, actual finish, remaining work and notes,
to the values in the database. It adds the baselines `rebaseline` made, and
leaves the master schedule and the imported baselines exactly as imported. It
refuses a path that already exists. Ctrl+C, SIGTERM or SIGHUP while it writes
removes what it wrote, with one line and exit 130; one that comes once the file
is complete is too late, and the export finishes as usual. Whether MS Project
opens the file is untested: there is no MS Project desktop here, only the
generated fixture.

The exported file imports into a new database like any MSPDI file, with two
limits. The baselines `rebaseline` made come back as imported ones, so that
database's report measures variance against Baseline 0. And import refuses a
file over 20 MB, which long notes can push an export past.

`rerun` processes again what an earlier run captured, as a new run, which is how
a run that failed or was killed gets finished. It takes a run id from `runs list`,
and refuses a run that captured nothing. Keys already decided come back as
duplicates:

```sh
uv run schedule-guard rerun --db "$DB" "$(uv run schedule-guard runs list --db "$DB" | head -1 | cut -d' ' -f3)"
```

`report` serves the database as HTML until you stop it:

```sh
uv run schedule-guard report --db "$DB" --port 8000
```

An update file is JSON Lines, one update per line:

```json
{"source_item_id": "u-001", "item_version": 1, "task_uid": 3, "made_against_version": 0, "fields": {"percent_complete": 40, "actual_start": "2026-06-08T08:00:00", "remaining_work": 58}, "submitted_at": "2026-10-26T16:10:00-05:00", "submitted_by": "Fencing crew (demo)"}
```

A conflict records one reason: `malformed`, `unknown_task`, `field_not_allowed`,
`stale_version`, `invalid_value`, `inconsistent_actuals` or `summary_task`.

### Try it: the demo

```sh
uv run schedule-guard demo
```

This builds a new database from the fictional fixture and its scripted updates,
then serves the report on <http://127.0.0.1:8000>. The demo's date is fixed at
2026-10-30, so it looks the same whenever you run it: applied progress, overdue
tasks, two reached milestones and open conflicts of seven kinds.

It reads those files from `tests/fixtures/`, so run it from a checkout. Without
`--db` the database goes in a temporary directory; `--db PATH` builds it there
instead, and refuses a path that already exists. `--port` works as it does for
`report`.

### The report and its limits

The report has six pages: `/` (summary), `/tasks` (planned against actual, with
the variance from a baseline), `/overdue`, `/milestones`, `/conflicts` and
`/runs`.

- **127.0.0.1 only.** It can't listen on any other address, and there is no
  option for one.
- **Host allow-list.** It answers only a `Host` of `127.0.0.1` or `localhost`,
  on any port. Any other gets 400, so a web page that points its own domain at
  127.0.0.1 can't read it.
- **GET only.** Any other method, HEAD included, gets 405.
- **Read-only.** Each page opens the database read-only, so SQLite refuses any
  write. There are no forms and no JavaScript.
- **No login.** Anyone who can reach 127.0.0.1 on the machine can read it.

Variance uses the newest baseline `rebaseline` added, else Baseline 0, and the
page names the number. An imported baseline with a higher number is never used.
`/overdue` lists leaf tasks only, since a summary task's % complete stays as
imported; summary tasks stay on `/tasks`, marked.

Ctrl+C, SIGTERM or SIGHUP stops `report` and `demo`: exit 0 once the server
listens, or one line on stderr and exit 130 before it does. That holds under
`uv run` too, which hands the command a terminal's Ctrl+C twice: a stop signal
within 1 s of the first is the same stop. A second Ctrl+C more than 1 s later
forces the exit. Either way the demo's temporary directory is removed, and once
the command has ended after a stop, further stop signals are ignored until the
process exits. Two cases still differ. A stop in roughly the first 0.1 s, before
the CLI has installed its handlers, comes before any demo directory exists.
SIGTERM or SIGHUP then ends the process by the signal with no line. SIGINT
depends on when it lands: in the first few milliseconds, before Python has its
own handler, it ends the process by the signal with no output; while Python
imports its `site` module it exits 1 with a "Fatal Python error" (mostly
init_import_site); after that it mostly ends the process by the signal with
Python's KeyboardInterrupt traceback. Now and then, most often just after the
`site` import, the interrupt lands in start-up code whose errors Python only
prints, in one of two forms: "Failed checking if argv[0] is an import path
entry", then a KeyboardInterrupt traceback from zipimport; or "Exception ignored
in: ...", then KeyboardInterrupt. Either way the command runs on; `demo` then
builds its database and serves. (Measured on one Linux
machine: about 0–4 ms, about 5–6 ms, then from about 7 ms until the CLI's
handlers were in, at about 60–70 ms; the command ran on in about 2 runs in 100
between about 6 and 10 ms, and once at 30 ms.) A stop that lands inside the
FastAPI and pydantic import can print a traceback and exit 1, though the
directory is still removed.
A SIGKILL can't be caught and leaves it, and under `nohup` a SIGHUP stays
ignored.

### Tests

```sh
uv run pytest -q
```

GitHub Actions runs `uv run --locked pytest -q` on every pull request and every
push to main, on Ubuntu 24.04 with its system Python 3.12. Dependabot proposes
updates to the workflow's two pinned actions once a month.

### What Phase 2 adds

Phase 2 has begun with the offline MSPDI export (`export`, above). The rest is
planned, not built. It connects the same guard to a Microsoft 365 tenant, with a
daily sync on one Linux machine and nothing listening for inbound traffic:

- A source that reads a SharePoint "Progress Updates" list through Microsoft
  Graph, with certificate sign-in and write access to two named lists only.
- A read-only "Tasks" list the service publishes, so the field can see each
  task's current version, and each update's status and reason written back to
  its row.
- Alerts: a failed run posted to a Teams channel, and an email when an update
  becomes a conflict.
- A systemd user timer for the daily run.

The design is in [PLAN.md](PLAN.md), section 9.
