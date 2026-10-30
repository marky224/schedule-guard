# Spec: 001 offline core (PLAN.md milestones M1–M6)

Status: derived from the approved PLAN.md (approved 2026-10-23); M1 done
2026-10-29, M2 done 2026-10-30, M3–M6 not started.
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
  running `uv run pytest -q`.

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
  updates and deletes. Without `--confirm` or `--reason` it refuses. It
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
  existing text (Q3).
- **US4 Sync (M4).** `sync now` and `sync daily` read every item from the
  file source (JSON Lines) and process them in `(submitted_at,
  source_item_id)` order. Each item commits its outcome in its own
  `BEGIN IMMEDIATE` transaction. Each run gets a UUID4 id, trigger, counts,
  status and error. A second concurrent run is refused by an `fcntl` lock
  file. `rerun <run-id>` reprocesses the items that run captured and records
  a new run with `rerun_of`. `conflicts dismiss <id> --note` is the only
  review action: dismiss only, and the field resubmits against the current
  version (Q5).
- **US5 Report (M5).** `report` serves GET-only HTML on 127.0.0.1: summary,
  planned vs actual with baseline variance, overdue (planned finish < today
  and % complete < 100, today injectable), milestones, conflicts, runs.
  Jinja2 autoescape on. `demo` builds a throwaway DB from the fixture plus
  scripted updates covering applied, overdue, a reached milestone and several
  conflict types, then serves the report.
- **US6 Hygiene and CI (M6).** A test scans tracked files for GUID-shaped
  strings outside an explicit fixture allowlist, tenant or secret
  assignments, and key files. A GitHub Actions workflow runs
  `uv run pytest -q` (Q15); its config gets a max-effort review.

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
  state equals an uninterrupted run.
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
- Imported baselines stay unlocked until `baseline lock` (Q24). Until then
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
  Hypothesis P1 (baseline bytes and SHA-256 identical), P2 (master schedule
  bytes identical), P4 (all or nothing), P5 (stale never applies) pass; with
  a deliberately broken applier, P1 or P2 fails and the trigger raises.
- M4: P3 (idempotent) and P6 (rerun equivalence, crash then rerun) pass; the
  `RuleBasedStateMachine` keeps P1–P5 after every step; a concurrent second
  run is refused.
- M5: POST/PUT/DELETE return 405; a `<script>` task name is escaped; overdue
  is correct for a fixed today; `demo` serves a page with every section
  populated.
- M6: the hygiene test passes on the repo and fails on a planted GUID in a
  temp file; CI runs the suite on GitHub.

## Open questions
- None blocking. The baseline-number cap (10) is UNVERIFIED and is checked
  in M7.
