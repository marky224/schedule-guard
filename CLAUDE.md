# CLAUDE.md

Repo-specific rules for schedule-guard. The full plan, decisions and sources
are in [PLAN.md](PLAN.md); the current work is in `specs/`.

## Stack
- Language / framework / DB / test runner: Python 3.12, FastAPI + Jinja2
  (read-only report), SQLite (stdlib `sqlite3`), defusedxml (MSPDI parsing),
  pytest + Hypothesis
- Package manager: uv (`uv.lock` committed; build backend `uv_build`;
  `[tool.uv] exclude-newer` cutoff 2026-10-22 in `pyproject.toml`; moving the
  cutoff is a dependency change, so ask first)
- CLI: `schedule-guard` (argparse, `src/schedule_guard/cli.py`)

## Commands
- Install / sync: `uv sync` (later runs: `uv sync --offline`; ask before adding
  any dependency)
- Dev: `uv run schedule-guard --help`; database commands take
  `--db <path>`: keep the file outside the repo (`*.db` is gitignored)
- Sync an update file: `uv run schedule-guard sync now --db <path> --source
  file:tests/fixtures/updates/week-44.jsonl`; then `runs list`, `conflicts
  list`, `conflicts dismiss <id> --note "..."`, `rerun <run-id>`
- Report: `uv run schedule-guard report --db <path> [--port 8000]` (read-only,
  127.0.0.1 only, and only a Host of 127.0.0.1 or localhost);
  `uv run schedule-guard demo [--db <new path>]` builds a demo database from
  the fixture and serves the report on it; Ctrl+C, SIGTERM or SIGHUP stops
  either: exit 0 once it listens, one line and exit 130 before (the demo's
  temporary directory, with no `--db`, goes then; a SIGKILL leaves it; under
  nohup a SIGHUP stays ignored)
- Test all: `uv run pytest -q` (Hypothesis is derandomized: the same examples every run)
- Test one: `uv run pytest -q tests/test_smoke.py::test_cli_version_prints_package_version`
- Regenerate the MSPDI fixture: `uv run python scripts/make_fixture.py`
  (seeded; `tests/test_fixture.py` fails if the checked-in bytes differ)
- Lint / types: none configured yet
- Proof command for a change: `uv run pytest -q`
- Use the system interpreter: `UV_PYTHON_DOWNLOADS=never`, `/usr/bin/python3.12`

## Map
- src/schedule_guard/ — application code (`cli.py`, `mspdi/parse.py` safe
  MSPDI reader, `store/schema.sql` SQLite schema and read-only triggers,
  the executable domain model, opened by `store/db.py`; `importer.py`,
  `baselines.py`; `clock.py`, `guard.py` the pure guard G0–G7,
  `progress.py` the applier; `sources/` the source protocol and the JSON
  Lines file source, `sync.py` the sync engine, `review.py` conflict review;
  `report/` the read-only FastAPI report and its Jinja2 templates,
  `demo.py` the demo database)
- tests/ — oracles (smoke, fixture, MSPDI parser, store trigger, import,
  baseline, clock, guard rule, applier, source, sync, review, report and
  demo tests; `test_hygiene.py` scans tracked files for GUIDs, tenant or
  secret assignments and key files;
  `conftest.py` shared fixtures and the Hypothesis profile)
- tests/properties/ — Hypothesis P1–P6 on the sync engine, a property that
  no line stops a run, the state machine and the teeth test (`harness.py`
  writes the update files)
- tests/fixtures/ — generated, fictional inputs (`riverbend-clinic.xml`,
  `updates/week-44.jsonl`, `demo-updates.jsonl`)
- scripts/ — `make_fixture.py`, the seeded fixture generator
- .github/ — `workflows/ci.yml` runs `uv run --locked pytest -q` on pull
  requests and pushes to main (Ubuntu 24.04, its system Python 3.12, uv
  0.11.15), least privilege, actions pinned by full commit SHA;
  `dependabot.yml` proposes updates to those pins monthly
- specs/001-offline-core/ — spec.md, plan.md, tasks.md for M1–M6
- .claude/skills/domain/ — ontology, glossary (present)
- PLAN.md — architecture, guard rules, milestones, decisions (§15), sources

## Invariants (PLAN.md §1; every change must keep them)
1. Two things are protected: the master schedule (planned fields) and the
   baseline (saved snapshots). Sync never writes either. Only an import or
   an explicit admin command does.
2. Progress fields only: % complete, actual start, actual finish, remaining
   work, notes. Anything else is a conflict.
3. Optimistic concurrency: an update made against a task version that is not
   current is a conflict.
4. All or nothing per update: one disallowed field means no field is applied.
5. Exactly once: idempotency key = source + source item id + item version;
   the apply and its outcome commit in one SQLite transaction.
6. Conflicts are never applied. Review can only dismiss with a note.
7. Untrusted input everywhere: MSPDI files, update files and list rows are
   parsed defensively.
8. Least privilege: Phase 2 gets write on two named lists only; never
   `Sites.Read.All`, `Sites.ReadWrite.All` or `Sites.FullControl.All`.
   Single exception (Q12): `Sites.Selected` with write on the demo site
   only, if the M9 spike shows list-level access fails, and only after the
   spike results are shown to the repo owner.

## Public repo rules
- This repo is public. Never commit tenant, client, site or list ids,
  real people's or company names, emails, secrets, certificates or keys.
- Fixture and demo data are generated and fictional only.
- Tenant settings and secrets live in a gitignored `.env`; key files live
  outside the repo. `*.db` (and its `-wal`, `-shm`, `-journal` and `.lock`
  files),
  `*.pem`, `*.pfx`, `*.p12`, `*.key`, `*.cer`, `*.crt`, `*.der` are
  gitignored.
- Use role names in docs: the tenant admin, the scheduler, the reviewer, the
  repo owner.

## Off-limits (repo-specific)
- `.env` and any key or certificate file: never read or edit.
- Never weaken a DB read-only trigger or a property test to make a change pass.
