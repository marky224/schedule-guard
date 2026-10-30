# Glossary: schedule-guard

| Use this | Do not use | Meaning |
|---|---|---|
| master schedule | plan, schedule (alone), baseline | The planned fields of every task; only `import` writes it |
| baseline | plan, snapshot (alone), master schedule | A numbered, append-only snapshot of planned values |
| re-baseline | reset baseline, update baseline | Admin command that appends a new baseline number |
| progress update | edit, change, patch | One submitted change to progress fields |
| progress fields | editable fields, safe fields | The allowlist: percent_complete, actual_start, actual_finish, remaining_work, notes |
| task version | revision, item version | Per-task counter, +1 per applied update |
| item version | task version | The source item's own version (Phase 2: listItem eTag) |
| idempotency key | update id, dedupe key | (source, source_item_id, item_version) |
| outcome | result, status (alone) | applied, conflict, or duplicate |
| conflict | error, rejection, failure | A stored, never-applied update awaiting review |
| dismiss | resolve, accept, approve | The only review action on a conflict |
| sync run | job, batch, sync (alone, as a noun) | One execution of sync with an id |
| rerun | retry, replay | A run that reprocesses another run's captured items |
| Tasks list | task table | Phase 2 SharePoint list the service publishes |
| task | Tasks list row | An MSPDI task, identified by UID |
