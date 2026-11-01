# Plan: 001 offline core

**Spec:** specs/001-offline-core/spec.md

## Knowledge classification
- Domain pages (rules/): not required yet. The guard rules live in the spec
  and PLAN.md §5; write `rules/guard.md` only if a rule needs explaining twice.
- Ontology: required (6+ entities, conflict status machine, outcome set).
  Drafted in M0 at `.claude/skills/domain/ontology.md` and `glossary.md`.
- Domain pages to read: ontology.md, glossary.md.
- Domain pages to update after merge: ontology.md (status → current once
  `store/schema.sql` exists in M2; entity list matched to the schema).

## Challenge list
- [skip] Real Project export / "opens in Project": no MS Project desktop
  (Q1). The generated fixture and structural tests are the oracle.
- [skip] Resource assignments: out of scope (PLAN.md C5); the fixture has
  none.
- PLAN.md stays at the repo root.
- No blockers.

## Files
- Create (M1): `scripts/make_fixture.py`, `src/schedule_guard/mspdi/__init__.py`,
  `src/schedule_guard/mspdi/parse.py`, `tests/fixtures/riverbend-clinic.xml`,
  `tests/test_fixture.py`, `tests/test_mspdi_parse.py`
- Create (M2): `src/schedule_guard/store/__init__.py`,
  `src/schedule_guard/store/schema.sql`, `src/schedule_guard/store/db.py`,
  `src/schedule_guard/importer.py`, `src/schedule_guard/baselines.py`,
  `tests/test_store_triggers.py`, `tests/test_import.py`, `tests/test_baselines.py`
- Create (M3): `src/schedule_guard/guard.py`, `src/schedule_guard/clock.py`,
  `src/schedule_guard/progress.py`, `tests/test_guard_rules.py`,
  `tests/test_clock.py`, `tests/test_progress.py`,
  `tests/properties/harness.py`, `tests/properties/test_invariants.py`,
  `tests/properties/test_teeth.py`, `tests/conftest.py`
- Create (M4): `src/schedule_guard/sources/__init__.py`,
  `src/schedule_guard/sources/base.py`, `src/schedule_guard/sources/file.py`,
  `src/schedule_guard/sync.py`, `src/schedule_guard/review.py`,
  `tests/fixtures/updates/*.jsonl`, `tests/test_sources.py`,
  `tests/test_sync.py`, `tests/test_review.py`,
  `tests/properties/test_sync_properties.py`,
  `tests/properties/test_state_machine.py`
- Create (M5): `src/schedule_guard/report/__init__.py`,
  `src/schedule_guard/report/app.py`, `src/schedule_guard/report/templates/*.html`,
  `src/schedule_guard/demo.py`, `tests/fixtures/demo-updates.jsonl`,
  `tests/test_report.py`, `tests/test_demo.py`
- Create (M6): `tests/test_hygiene.py`, `.github/workflows/ci.yml`
- Modify: `src/schedule_guard/cli.py` (subcommands per milestone),
  `src/schedule_guard/store/db.py` (the report's read-only open, M5),
  `README.md` (M6), `CLAUDE.md` (map and commands as directories appear),
  `.claude/skills/domain/ontology.md` (after M2)
- Do not touch: `PLAN.md` decisions (§15), `LICENSE`, `uv.lock` except via
  `uv lock` when the repo owner approves a new dependency

## Sequence
1. M1 fixture + parser → verify: fixture determinism and malicious-XML tests.
2. M2 store + import + baselines → verify: trigger and round-trip tests.
3. M3 guard + P1/P2/P4/P5 + teeth → verify: properties green, teeth test
   shows they fail on a broken applier.
4. M4 sources + sync + rerun + P3/P6 + state machine → verify: all
   properties green, crash-and-rerun test.
5. M5 report + demo → verify: report tests; `demo` serves.
6. M6 README + hygiene + CI → verify: suite green locally and in CI.

## Tests / oracle
- Command: `uv run pytest -q` (offline; deps from `uv.lock`).
- Cases: the spec's Acceptance list. Hypothesis runs on a temp-file SQLite DB
  built from the fixture; the teeth test monkeypatches the applier to write
  `planned_finish` or a baseline field and expects P1/P2 to fail.

## Component / ontology updates after merge
- `.claude/skills/domain/ontology.md`: mark current and match entity names to
  `store/schema.sql` (after M2).
- `CLAUDE.md`: map entries for new directories; commands for the CLI.
