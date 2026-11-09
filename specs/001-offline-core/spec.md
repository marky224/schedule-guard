# Spec: 001 offline core (PLAN.md milestones M1–M6)

Status: derived from the approved PLAN.md (approved 2026-10-23); M1 done
2026-10-29, M2, M3 and M4 done 2026-10-30, M5 done 2026-11-01, M6 done
2026-11-04 (its first CI run on GitHub, on PR #1, passed on 2026-11-08).
Source plan: [PLAN.md](../../PLAN.md) §1–8, §11. Terms: `.claude/skills/domain/`.

## Problem
Field crews report progress against an MS Project schedule. If any progress
path can write planned fields or a saved baseline, the master schedule and the
record of what was promised can be silently overwritten. Phase 1 must prove,
offline, that a progress update can change only progress fields of the
current task version, exactly once, and can never change the master schedule
or any baseline.

## In scope
- M1: deterministic fictional MSPDI fixture; safe MSPDI parser.
- M2: SQLite store with read-only triggers; `import`, `baseline lock`,
  `rebaseline --confirm --reason`.
- M3: guard engine, rules G0–G7, property tests P1, P2, P4, P5, and a test
  that proves the properties catch a broken applier.
- M4: source adapter protocol, file source, `sync now` / `sync daily`, run
  records, run lock, `rerun <run-id>`, `runs list`, `conflicts list`,
  `conflicts dismiss`, properties P3 and P6, a stateful test.
- M5: read-only FastAPI HTML report and `demo`.
- M6: README Phase 1 section, public-repo hygiene test, GitHub Actions CI
  running `uv run --locked pytest -q`.

## Out of scope
- Microsoft Graph, tenant access, alerts, systemd units (Phase 2).
- MSPDI export and P7 (M7).
- `import --replace-plan` or any re-import into a non-empty project (Q14).
- Re-applying a conflict in any form (Q5).
- Resource assignments in the fixture (PLAN.md C5).
- A real MS Project export, or checking a file opens in Project (Q1).
- Full MSPDI XSD validation (PLAN.md C7).

## Behaviors
- **US1 Import (M1, M2).** `schedule-guard import --db <path> <file.xml>`
  parses an MSPDI file with defusedxml (DTDs, entities and external
  references forbidden; over 20 MB or over 10,000 tasks rejected; root must
  be `Project` in namespace `http://schemas.microsoft.com/project`). It
  stores the raw bytes with their SHA-256, the master schedule per task
  (with its planned elements as C14N bytes), each task's progress values
  from the file at task version 0, and every `<Baseline>`
  subtree as C14N bytes, in one transaction that ends by setting the
  project's `import_finished_at`. After that, the schema refuses new task
  rows. It works only on an empty project (Q14); a second import is refused.
  In v1.0 a changed schedule goes into a fresh project database.
- **US2 Baselines (M2).** `baseline lock --db <path> [--number N]` locks an
  imported baseline (Baseline 0 by default). `rebaseline --db <path>
  --confirm --reason "..."` appends the next baseline number from the
  current planned values and locks it; old baselines never change and
  history is kept (Q7). A locked baseline refuses new rows as well as
  updates and deletes. Without `--confirm` or `--reason`, or with a reason
  that isn't UTF-8, it refuses. It
  refuses past baseline number 10 (**UNVERIFIED**: MS Project's Baseline plus
  Baseline1–10, from memory; Learn's Baseline element page gives no range;
  checked in M7). Sync, rerun and report code never call it.
- **US3 Guard (M3).** Each progress update gets exactly one outcome:
  `applied`, `conflict` or `duplicate`. Checks, first failure wins:
  - G0: key already decided → `duplicate`.
  - G1 `malformed`: The payload is malformed: missing task id or version,
    wrong types, unknown encoding.
  - G2 `unknown_task`.
  - G3 `field_not_allowed`: any field outside percent_complete,
    actual_start, actual_finish, remaining_work, notes.
  - G4 `stale_version`: made-against version ≠ current task version.
  - G5 `invalid_value`: A value is out of range or the wrong type: %
    complete outside 0–100, unparseable date, negative remaining work, or an
    actual start/finish later than today (Q6; "today" is an injectable
    clock).
  - G6 `inconsistent_actuals`, after merging with current values: actual
    finish before actual start, or % complete 100 without an actual finish
    (Q6).
  - G7 `summary_task`: progress on a summary task is rejected (Q4).

  Applied updates write only progress fields and bump the task version by 1.
  `notes` is appended as `[date, source, submitter] text` and never replaces
  existing text (Q3); the date is `submitted_at`'s date in America/Chicago,
  and entries are separated by a line feed. An applied outcome stores the
  task's progress before and after (`before_json`, `after_json`): the
  version and the other four fields, without the task's notes; `after_json`
  holds `notes_entry`, the entry the update appended, or null. So n notes
  updates to one task store note text in proportion to n; `task_progress`
  holds the whole notes.

  The guard (`guard.decide`) is pure: it takes the update, its task's
  current state and the clock, and returns one outcome. Its caller says
  whether the idempotency key already has an outcome (G0) and stores the
  result. An update's payload is an object with integer `task_uid` and
  `made_against_version`, a non-empty `fields` object, a `submitted_by`
  name of at most 100 characters (no control character at all, tab and
  line breaks included, and no `[` or `]`, so a name can't end its own
  notes entry or start another) and a `submitted_at` date-time with a UTC
  offset; anything else,
  including an item its source couldn't decode, is `malformed`. Values:
  `percent_complete` an integer from 0 to 100; `actual_start` and
  `actual_finish` MSPDI date-times (`2026-07-16T08:00:00`, local, no
  offset); `remaining_work` hours from 0 to 1,000,000, stored as an MSPDI
  duration (`PT12H30M0S`); `notes` text an MSPDI file can hold: only
  characters XML 1.0 allows (no lone surrogates, no U+FFFE or U+FFFF) and
  no control character besides tab, LF and CR (so no DEL and no C1
  control), at most 4,000 characters, and short enough that the task's
  notes, the new entry and its heading included, stay within 100,000
  characters (characters as code points). A lone surrogate in notes is
  `invalid_value` to the guard on its own; through the file source the
  strict decoder refuses it first, so the line is an unkeyed `malformed`
  conflict (US4). A note's own line breaks stay as typed. Null is no
  value, so
  an update can't clear a field. G6 checks the merged state, so a task at
  100 % complete with no actual finish (as a file may import it) refuses
  every update that leaves it that way, a notes-only one too; the same
  update with an actual finish, or one that lowers % complete, applies.
- **US4 Sync (M4).** `sync now` and `sync daily` read every item from the
  file source (JSON Lines) and process them in `(submitted_at,
  source_item_id)` order. Each item commits its outcome in its own
  `BEGIN IMMEDIATE` transaction. Each run gets a UUID4 id, trigger, counts,
  status and error. A second concurrent run is refused by an `fcntl` lock
  file next to the database, named from its resolved path, so a `--db`
  given through a symlink shares the real path's lock; a hard link is a
  name of its own and still gets its own lock file. `rerun <run-id>`
  reprocesses the items that run captured and records
  a new run with `rerun_of`. `conflicts dismiss <id> --note` is the only
  review action: dismiss only, and the field resubmits against the current
  version (Q5).

  Every command takes `--db <path>`; `sync now|daily` also takes `--source
  file:<path>`. The file source checks each line's idempotency key itself
  (`source_item_id` 1 to 200 letters, digits and `. _ : -`, unique across
  the files one database reads; `item_version` an integer from 0) and hands
  the decoded object to the guard untouched. A line that isn't UTF-8,
  isn't strict JSON (no NaN, no key twice, no lone-surrogate escape such
  as `\ud800` in any string or key) or has no usable key is stored under
  `unkeyed/` plus the SHA-256 of its bytes and becomes one `malformed`
  conflict; reading the same bytes again is a duplicate. No line stops a
  run, and the lines around it are still decided. A run first captures
  every line (a key given twice in one run counts once, the rest as
  duplicates), then processes the captured items. A failed run records
  its error. A killed run is left `running` until the next run starts,
  which holds the lock and marks it `interrupted`. A live run can be
  marked too when the next run takes another lock file: through a hard
  link to the database, or after the lock file was deleted or the
  database renamed while the live run held it; that run then ends `ok`
  or `failed`. `rerun` finishes a failed or interrupted
  run, and refuses, with one error line and exit 1, a run that captured
  nothing. A missing or oversize (over 20 MB) update file fails the run
  before anything is captured, so there is nothing to rerun. An
  update-file name, a `--note`, a rerun `RUN_ID` or a rebaseline `--reason`
  that isn't UTF-8 is refused the same way, before anything is recorded.
- **US5 Report (M5).** `report --db <path> [--port 8000]` serves GET-only
  HTML on 127.0.0.1 only: summary, planned vs actual with the start and
  finish variance (planned minus baseline, in calendar days) from the
  newest baseline `rebaseline` added, else Baseline 0, never an imported
  baseline with a higher number (each page names the number used, or
  none), overdue leaf tasks (planned finish before today and % complete
  under 100, today from the injectable clock as the America/Chicago date),
  milestones (reached, overdue or upcoming), conflicts, runs. Any other
  method is 405. It answers only a Host of `127.0.0.1` or `localhost`
  (any port); any other Host gets 400 on every route. It only reads:
  each page opens the database read-only (`mode=ro` and `query_only`)
  and reads one snapshot. Jinja2 autoescape
  on, no `|safe`; no JS, no forms, FastAPI's docs pages off, and a
  Content-Security-Policy that allows only inline style. A summary task's
  % complete is shown as imported, so summary tasks stay on `/tasks`,
  marked, and are never listed as overdue. A project with no name shows
  "(no name)". `demo [--db <path>] [--port 8000]`
  builds a new database (an existing file is refused, never written; no
  `--db` means a temporary directory, removed when the demo stops on
  Ctrl+C, SIGTERM or SIGHUP; a SIGKILL leaves it) from the fixture
  plus `tests/fixtures/demo-updates.jsonl`, written by hand with fake
  "(demo)" crews, with Baseline 0 locked and a fixed demo clock
  (2026-10-30 in Chicago): applied progress, overdue tasks, two reached
  milestones and open conflicts of seven kinds. Then it serves the report.
  Ctrl+C, SIGTERM or SIGHUP stops `report` or `demo`: exit 0 and no
  traceback once it listens and uvicorn has shut down, one line on stderr
  and exit 130 before it listens, also under `uv run`, which delivers a
  terminal's Ctrl+C twice: a stop signal within 1 s of the first is the
  same stop, and one more than 1 s later forces the exit. The demo's
  temporary directory is removed either way, and once the command has
  ended after a stop, further stop signals are ignored until the process
  exits. Two cases still differ: a stop in roughly the first 0.1 s, before
  the CLI has installed its handlers, ends the process by the signal
  (SIGINT with Python's KeyboardInterrupt traceback, SIGTERM or SIGHUP
  with no line) before any demo directory exists; and a stop inside the
  FastAPI and pydantic import can print a traceback and exit 1. A SIGHUP
  that was already ignored (nohup) stays ignored.
- **US6 Hygiene and CI (M6).** A test scans tracked files for GUID-shaped
  strings outside an explicit fixture allowlist, tenant or secret
  assignments, appSettings key and value pairs and XML elements with a value
  and a closing tag (camelCase and acronym-prefixed names too), PRIVATE KEY
  blocks (PGP ones too), and key files (the seven suffixes `.gitignore`
  lists); a UTF-16 file is decoded as such first, and a NUL-padded one isn't. A GitHub Actions workflow runs
  `uv run --locked pytest -q` (Q15), so a stale `uv.lock` fails it; its
  config gets a max-effort review.

## Edge cases / failures
- Billion-laughs, XXE file read, external DTD, oversize file, wrong root or
  namespace: rejected with a clear error, no file read, nothing stored, and
  no database file left at a new `--db` path (the file is parsed first).
- Inside a `<Baseline>` or a master-schedule element (the bytes kept as
  C14N), an element outside the MSPDI namespace, any attribute, or an
  element more than 3 levels below the Baseline or PredecessorLink: rejected
  before C14N, so the C14N work stays linear with a small constant. A
  Baseline `Number` above 10: rejected. (All UNVERIFIED against a real
  Project export; checked in M7.)
- A carriage return in text (`&#13;`): stored as a line feed in the C14N
  bytes, which are those of the subtree as ElementTree writes it out again;
  the values read into the other columns keep it.
- Update with allowed and forbidden fields together: whole update is a
  `field_not_allowed` conflict; no field is applied.
- Two updates made against the same version in one run: the first applies,
  the second is `stale_version`.
- The same line read on every run of the file source: `duplicate`, no effect.
- Crash mid-run: items without an outcome are processed by `rerun`; the final
  state equals an uninterrupted run. The next run to start marks the
  crashed run `interrupted`.
- A line with a lone-surrogate escape in `submitted_by`, `submitted_at`, a
  field value or a key: one `malformed` conflict keyed by its bytes; the
  other lines of the file are decided as usual.
- A conflict is never applied by `rerun` (its key returns `duplicate`).
- Direct SQL UPDATE or DELETE on `task` or `baseline_task`, DELETE of the
  `project` row, or any UPDATE of it other than the one-time set of
  `import_finished_at` (so its id, raw source bytes and hash never change):
  the trigger aborts.
- `INSERT OR REPLACE` into `project`, `task`, `baseline` or `baseline_task`
  with an existing primary key: the BEFORE INSERT trigger aborts, so a
  locked baseline stays locked. This holds with SQLite's default
  `recursive_triggers = OFF`; no connection pragma is needed.
- `INSERT OR REPLACE ... (rowid, ...)` with an existing row's rowid and a
  new key, on any of the four tables (including deleting the `project` row
  that way and re-inserting it with a new hash and `import_finished_at`
  NULL), and `UPDATE OR REPLACE ... SET rowid = ...`: refused, because the
  four tables are WITHOUT ROWID ("table <t> has no column named rowid" for
  the INSERT, "no such column: rowid" for the UPDATE).
- A REPLACE through another unique key: the four tables carry no unique
  index of any kind besides their primary key (no UNIQUE constraint, no
  `CREATE UNIQUE INDEX`, partial or on an expression), so there is none to
  conflict on; the schema check fails if one is added.
- Direct SQL INSERT of a `baseline_task` row unless its baseline exists
  with `locked_at` not set and its task exists: the trigger aborts.
- Imported baselines stay unlocked until `baseline lock`. Until then
  the schema still accepts a new row in one for a task it has no row for,
  though never a changed or deleted row; `baseline lock` ends that.
- Direct SQL INSERT of a `task` row unless its project exists with
  `import_finished_at` not set: the trigger aborts.
- Direct SQL INSERT of a second `project` row, or of a `baseline` row that
  is already locked (`locked_at` or `locked_by` set) or numbered outside
  0–10: the trigger aborts. A new, unlocked baseline numbered 0–10 is
  accepted, since that is how `rebaseline` appends one.
- The one-time lock or the one-time set of `import_finished_at` together
  with a change to any other column, including an `UPDATE OR REPLACE` that
  renumbers a baseline onto a locked one: the trigger aborts.
- Dates in MSPDI are local without offset. At run time "today" is the
  current date in America/Chicago (zoneinfo), whatever the host's time
  zone. Every test injects the clock; no test reads the host clock or time
  zone, so a UTC CI runner gives the same results.
- `rebaseline` on a project already at baseline number 10: refused.
  `baseline lock --number` outside 0–10: refused with an error.
- A project name with control characters: escaped when `import` prints
  it; the stored name keeps them.
- Task names or notes containing HTML: escaped in the report.

## Acceptance (testable)
- `uv run pytest -q` is green after each milestone.
- M1: regenerating the fixture yields identical bytes; each malicious XML case
  is rejected.
- M2: trigger tests raise on direct writes, run with SQLite's default
  `recursive_triggers = OFF`: UPDATE or DELETE on `task`, `baseline_task`
  and `baseline` (except the one-time lock); DELETE of the `project` row,
  `UPDATE project SET id = ...`, any change to its raw bytes or hash, and a
  second set of `import_finished_at`; the lock or the `import_finished_at`
  set paired with a change to any other column (including an `UPDATE OR
  REPLACE` renumber); a second `project` row; a `baseline` row inserted
  locked or numbered outside 0–10; `INSERT OR REPLACE` with an existing
  key on all four tables (a locked baseline stays locked); `INSERT OR
  REPLACE` with an explicit rowid on all four tables (including the
  project delete and re-insert) and `UPDATE OR REPLACE ... SET rowid`; a
  schema check that `PRAGMA table_list` shows `wr` = 1 for the four tables
  and that every `PRAGMA index_list` row with `unique` = 1 has origin `pk`;
  INSERT into `task` after `import_finished_at` or for a missing project;
  INSERT into `baseline` for a missing project; INSERT into `baseline_task`
  for a locked or missing baseline or a missing task. Imported C14N bytes
  round-trip; rebaseline refuses without `--confirm`/`--reason` and past
  number 10.
- M3: unit tests for G0–G7, including the Q6 checks with a fixed clock;
  Hypothesis P1 (baseline bytes and SHA-256 identical, and every baseline
  column), P2 (master schedule bytes and every task column identical), P4
  (all or nothing), P5 (stale never applies) pass; with a deliberately
  broken applier, P1 or P2 fails and the trigger raises.
- M4: P3 (idempotent) and P6 (rerun equivalence, crash then rerun) pass; the
  `RuleBasedStateMachine` keeps P1–P5 after every step, and its baseline
  lock changes only `baseline.locked_at` and `locked_by`, no
  `baseline_task` row; a concurrent second run is refused; a property puts
  arbitrary lines (lone surrogates, nesting at the decoder's limit, huge
  numbers, bytes that aren't UTF-8) between valid ones, and every run ends
  ok with every line decided.
- M5: POST/PUT/PATCH/DELETE return 405 on every route; a `<script>` task
  name and note are escaped; the database file is byte-identical after a
  GET of every route; the report binds 127.0.0.1; a foreign Host gets 400
  on every route; Ctrl+C, SIGTERM and SIGHUP stop `report` and `demo` with
  exit 0 once they listen and exit 130 before; overdue
  is correct for a fixed today; `demo` serves a page with every section
  populated.
- M6: the hygiene test passes on the repo and fails on a planted GUID in a
  temp file; CI runs the suite on GitHub.

## Open questions
- None blocking. The baseline-number cap (10) is UNVERIFIED and is checked
  in M7.
