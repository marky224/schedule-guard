# CLAUDE.md

Repo-specific rules for schedule-guard. The full plan, decisions and sources
are in [PLAN.md](PLAN.md); the current work is in `specs/`.

## Stack
- Language / framework / DB / test runner: Python 3.12, FastAPI + Jinja2
  (read-only report), SQLite (stdlib `sqlite3`), defusedxml (MSPDI parsing),
  pytest + Hypothesis
- Package manager: uv (`uv.lock` committed; build backend `uv_build`)
- CLI: `schedule-guard` (argparse, `src/schedule_guard/cli.py`)

## Commands
- Install / sync: `uv sync` (later runs: `uv sync --offline`; ask before adding
  any dependency)
- Dev: `uv run schedule-guard --help`
- Test all: `uv run pytest -q`
- Test one: `uv run pytest -q tests/test_smoke.py::test_cli_version_prints_package_version`
- Lint / types: none configured yet
- Proof command for a change: `uv run pytest -q`
- Use the system interpreter: `UV_PYTHON_DOWNLOADS=never`, `/usr/bin/python3.12`

## Map
- src/schedule_guard/ — application code (`cli.py`; schemas added in M2 are
  the executable domain model)
- tests/ — oracles (smoke test now; property tests from M3)
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
   spike results are shown.

## Public repo rules
- This repo is public. Never commit tenant, client, site or list ids,
  real people's or company names, emails, secrets, certificates or keys.
- Fixture and demo data are generated and fictional only.
- Tenant settings and secrets live in a gitignored `.env`; key files live
  outside the repo. `*.db` (and its `-wal`, `-shm`, `-journal` files),
  `*.pem`, `*.pfx`, `*.p12`, `*.key` are gitignored.
- Use role names in docs: the tenant admin, the scheduler, the reviewer, the
  repo owner.

## Off-limits (repo-specific)
- `.env` and any key or certificate file: never read or edit.
- Never weaken a DB read-only trigger or a property test to make a change pass.
