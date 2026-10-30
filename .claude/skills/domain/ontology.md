# Domain ontology: schedule-guard

Status: draft (from PLAN.md §3–5; no src/ schemas yet, M2 adds them)
Last verified: 2026-10-29
Executable source of truth: src/ schemas. If this page disagrees with code, code wins.

## Terms (use only these)
- **Project**: one imported MSPDI schedule.
- **Task**: an MSPDI `<Task>`, identified by its unique `UID`.
- **Master schedule**: the planned fields of every task: name, WBS, outline
  level, summary and milestone flags, Start, Finish, Duration, Work,
  predecessor links. Only `import` writes it.
- **Baseline**: a numbered snapshot of planned values per task (MSPDI
  `<Baseline>`: Number, Start, Finish, Duration, Work, Cost). Append-only.
- **Locked baseline**: a baseline whose rows are read-only (DB triggers).
- **Re-baseline**: the admin command `rebaseline --confirm --reason` that
  appends a new baseline number from the current planned values.
- **Progress fields**: the allowlist `percent_complete`, `actual_start`,
  `actual_finish`, `remaining_work`, `notes` (MSPDI `PercentComplete`,
  `ActualStart`, `ActualFinish`, `RemainingWork`, `Notes`).
- **Task version**: an integer per task that rises by 1 each time a progress
  update is applied to it.
- **Progress update**: one submitted change: task UID, made-against task
  version, progress fields, submitter, submitted time.
- **Source**: where progress updates come from (`file` in Phase 1, the Graph
  Progress Updates list in Phase 2).
- **Source item / item version**: the update's id in its source, and its
  version there.
- **Idempotency key**: `(source, source_item_id, item_version)`.
- **Outcome**: the decision for one idempotency key.
- **Conflict**: the stored record of an update that was not applied, with a
  reason code.
- **Sync run**: one execution of sync, with an id, trigger, counts, status
  and error.
- **Rerun**: a sync run that reprocesses the source items another run
  captured.

Forbidden synonyms: "plan" for baseline, "edit" for update, "job" for run.

## Entities (planned tables, PLAN.md §4)
- `project`, `task` (master schedule), `baseline`, `baseline_task`
- `task_progress` (progress fields + task version)
- `source_update`, `outcome`, `conflict`
- `sync_run`, `run_item`, `source_cursor`, `writeback` (Phase 2 outbox)

## Relations
- Project —has→ Task (1..n)
- Project —has→ Baseline (0..11, numbered 0..10; the cap is UNVERIFIED, checked in M7)
- Baseline —has→ Baseline task row —for→ Task
- Task —has→ Task progress (1) —carries→ Task version
- Source —emits→ Source item —versioned as→ Item version
- Progress update —identified by→ Idempotency key —decided as→ Outcome (exactly 1)
- Outcome `conflict` —creates→ Conflict (exactly 1)
- Sync run —captures→ Progress update (via run_item)
- Rerun —rerun_of→ Sync run

## Outcome set
- `applied`: progress fields written, task version + 1, before/after stored.
- `conflict`: nothing written to progress; a Conflict record is created.
- `duplicate`: the key already had an outcome; no effect, not a conflict.

## Conflict status
- `open` → `dismissed` (by a reviewer, with a note). No other transition.
- Never `open` → applied. To change the schedule, the scheduler edits it in
  MS Project; in v1.0 the changed schedule goes into a fresh project
  database. Re-import into a non-empty project is out of scope (Q14).

Reason codes: `malformed`, `unknown_task`, `field_not_allowed`,
`stale_version`, `invalid_value`, `inconsistent_actuals`, `summary_task`,
`edited_after_submit` (Phase 2).

## Roles (authz model)
- **Tenant admin** (Phase 2 only): consents the app permission, grants the
  list roles, creates the Team, lists, Workflows flow and List rule, and
  sets user rights on the lists. Has no CLI role.
- **Scheduler**: owns the master schedule in MS Project. Runs `import`,
  `baseline lock` and `rebaseline --confirm --reason`.
- **Reviewer**: reviews conflicts. Runs `conflicts list` and
  `conflicts dismiss <id> --note`; dismiss is the only review action.
- **Repo owner**: approves the plan, new dependencies and live tenant calls,
  and receives alerts.
- Sync (`sync now|daily`, `rerun`) is run by the timer or any operator; it
  never needs a role, because it can only apply progress fields.
- Phase 1 is a local CLI with no authentication: anyone with shell access to
  the host and the DB file can run every command. The roles above are
  conventions, not enforced checks. Whoever runs a command, the DB triggers
  and the WITHOUT ROWID tables refuse UPDATE, DELETE, `INSERT OR REPLACE`
  and `UPDATE OR REPLACE` on the master schedule and baselines, on any
  connection that leaves triggers enabled (the default). They can't stop
  someone who can write the DB file from switching triggers off on their
  connection, dropping a trigger or replacing the file.

## Constraints
- Sync never writes the master schedule or any baseline; DB triggers
  enforce it a second time.
- An update is applied only if every field is a progress field and its
  made-against version equals the current task version (all or nothing).
- A key is applied at most once: the apply and its outcome commit in one
  SQLite transaction.
- G5/G6 validation: % complete 0–100, remaining work ≥ 0, valid dates,
  actual dates not later than today (injectable clock), actual finish ≥
  actual start, 100 % requires an actual finish.
- Progress on summary tasks is rejected (`summary_task`).
- `notes` is appended as dated entries; existing text is never replaced.
- Re-baseline appends; old baselines never change. It refuses past baseline
  number 10 (UNVERIFIED, checked in M7).
