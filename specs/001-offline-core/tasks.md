# Tasks: 001 offline core

Format: `- [ ] T000 [P?] [USn] Description (path)`

## Setup
- [x] T001 Scaffold: pyproject, uv.lock, package, CLI `--version`, smoke test, domain skill, this spec (M0; `pyproject.toml`, `tests/test_smoke.py`)

## Implement
### M1: fixture and parser
- [x] T010 [US1] Seeded generator for the fictional ~40-task clinic build with 5 milestones, predecessor links and Baseline 0, no assignments (`scripts/make_fixture.py`)
- [x] T011 [US1] Generated fixture checked in (`tests/fixtures/riverbend-clinic.xml`)
- [x] T012 [US1] Safe parser: defusedxml with `forbid_dtd=True`, size/task limits, root and namespace check, task subset and C14N baseline bytes (`src/schedule_guard/mspdi/parse.py`)
- [x] T013 [P] [US1] Fixture determinism test (`tests/test_fixture.py`)
- [x] T014 [P] [US1] Parser tests: fields read, billion laughs, XXE, external DTD, oversize, wrong root or namespace (`tests/test_mspdi_parse.py`)
### M2: store, import, baselines
- [ ] T020 [US1] Schema with read-only triggers on `project`, `task`, `baseline`, `baseline_task`, including BEFORE INSERT triggers that refuse an existing key (`INSERT OR REPLACE`) or a missing or closed parent; all four tables WITHOUT ROWID with no UNIQUE constraint besides the primary key (`src/schedule_guard/store/schema.sql`)
- [ ] T021 [US1] DB open/migrate helpers (`src/schedule_guard/store/db.py`)
- [ ] T022 [US1] `import` into an empty project only; refuse a second import (Q14) (`src/schedule_guard/importer.py`, `src/schedule_guard/cli.py`)
- [ ] T023 [US2] `baseline lock` and `rebaseline --confirm --reason`, append-only (Q7), refuse past number 10 (UNVERIFIED, checked in M7); rebaseline inserts the new baseline's `baseline_task` rows before it sets `locked_at`, since the INSERT trigger refuses rows for a locked baseline (`src/schedule_guard/baselines.py`, `src/schedule_guard/cli.py`)
- [ ] T024 [P] [US1] Trigger tests, including `INSERT OR REPLACE` with an existing key or an explicit rowid (all four tables, including the project delete and re-insert), `UPDATE OR REPLACE ... SET rowid`, project DELETE and id change, orphan inserts, and a schema check for WITHOUT ROWID and no extra UNIQUE constraint, with the default `recursive_triggers = OFF` (`tests/test_store_triggers.py`)
- [ ] T025 [P] [US1] Import round-trip tests (`tests/test_import.py`)
- [ ] T026 [P] [US2] Baseline command tests (`tests/test_baselines.py`)
### M3: guard
- [ ] T030 [US3] Injectable clock (`src/schedule_guard/clock.py`)
- [ ] T031 [US3] Pure guard G0–G7 with Q6 checks in G5/G6, summary-task reject (Q4), notes append (Q3) (`src/schedule_guard/guard.py`)
- [ ] T032 [P] [US3] Unit tests per rule (`tests/test_guard_rules.py`)
- [ ] T033 [US3] Shared fixtures: temp DB from the fixture (`tests/conftest.py`)
- [ ] T034 [US3] Hypothesis P1, P2, P4, P5 (`tests/properties/test_invariants.py`)
- [ ] T035 [US3] Teeth test: broken applier makes P1/P2 fail; trigger raises (`tests/properties/test_teeth.py`)
### M4: sources and sync
- [ ] T040 [US4] `SourceAdapter` protocol (`src/schedule_guard/sources/base.py`)
- [ ] T041 [US4] JSON Lines file source (`src/schedule_guard/sources/file.py`)
- [ ] T042 [US4] Sync engine: ordering, per-item transactions, run records, lock file, rerun (`src/schedule_guard/sync.py`)
- [ ] T043 [US4] CLI: `sync now|daily`, `rerun`, `runs list`, `conflicts list|dismiss` (dismiss only, Q5) (`src/schedule_guard/cli.py`)
- [ ] T044 [P] [US4] Update files with fake crew names (`tests/fixtures/updates/*.jsonl`)
- [ ] T045 [P] [US4] Sync tests: duplicates, same-version pair, concurrent run refused, crash then rerun (`tests/test_sync.py`)
- [ ] T046 [US4] Hypothesis P3, P6 (`tests/properties/test_sync_properties.py`)
- [ ] T047 [US4] `RuleBasedStateMachine` checking P1–P5 (`tests/properties/test_state_machine.py`)
### M5: report and demo
- [ ] T050 [US5] GET-only FastAPI app and Jinja2 templates (`src/schedule_guard/report/app.py`, `src/schedule_guard/report/templates/`)
- [ ] T051 [US5] `demo` command and scripted updates (`src/schedule_guard/demo.py`, `tests/fixtures/demo-updates.jsonl`)
- [ ] T052 [P] [US5] Report tests: 405s, escaping, overdue with fixed today (`tests/test_report.py`)
### M6: wrap-up
- [ ] T060 [US6] Hygiene test with fixture GUID allowlist (`tests/test_hygiene.py`)
- [ ] T061 [US6] GitHub Actions running `uv run pytest -q`; max-effort review of the config (Q15) (`.github/workflows/ci.yml`)
- [ ] T062 [US6] README Phase 1 section (`README.md`)

## Verify
- [ ] T070 Run proof command `uv run pytest -q` after each milestone
- [ ] T071 Update ontology status and CLAUDE.md map (`.claude/skills/domain/ontology.md`, `CLAUDE.md`)
