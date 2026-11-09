-- schedule-guard SQLite schema, version 4 (PLAN.md section 4).
--
-- project, task, baseline and baseline_task hold the imported file, the
-- master schedule and the baselines. On any connection that leaves triggers
-- enabled (the default), their triggers refuse:
--   * every DELETE;
--   * every UPDATE but two, each with every other column unchanged: setting
--     project.import_finished_at once, and locking a baseline once
--     (locked_at and locked_by, from NULL);
--   * an INSERT whose primary key already exists, a second project, a task
--     for a missing project or once its project's import has finished, a
--     baseline for a missing project, a baseline that is already locked or
--     numbered outside 0-10 (MS Project's Baseline plus Baseline1-10,
--     UNVERIFIED, no real export, Q1), or a baseline_task row for a missing task or a
--     missing or locked baseline.
-- They don't refuse a new, unlocked baseline numbered 0-10 with rows for
-- existing tasks: that is how rebaseline appends one, and the schema can't
-- tell which program is writing.
--
-- With SQLite's default recursive_triggers = OFF, a REPLACE deletes the row
-- it conflicts with without firing that row's DELETE trigger. So every
-- BEFORE INSERT trigger here refuses a primary key that already exists, and
-- the four tables are WITHOUT ROWID with no unique index besides their
-- primary key, leaving no other key to conflict on. Never add a UNIQUE
-- constraint or a CREATE UNIQUE INDEX to them.
--
-- task_progress is not protected: it holds the progress fields and the
-- task version, which import seeds and an applied update changes. Nor are
-- the sync tables at the end, which record what sync read and decided.

CREATE TABLE project (
    id                 INTEGER NOT NULL PRIMARY KEY,
    name               TEXT,
    source_sha256      TEXT NOT NULL,
    source_xml         BLOB NOT NULL,           -- the imported file, byte for byte
    imported_at        TEXT NOT NULL,
    import_finished_at TEXT                     -- set once, as the last step of import
) WITHOUT ROWID;

-- A database holds one project (Q14), so this also refuses an existing key.
CREATE TRIGGER project_insert_only_one BEFORE INSERT ON project
WHEN EXISTS (SELECT 1 FROM project)
BEGIN SELECT RAISE(ABORT, 'project: this database already holds a project'); END;

CREATE TRIGGER project_update_finish_import_once BEFORE UPDATE ON project
WHEN NOT (OLD.import_finished_at IS NULL AND NEW.import_finished_at IS NOT NULL
          AND NEW.id IS OLD.id AND NEW.name IS OLD.name
          AND NEW.source_sha256 IS OLD.source_sha256 AND NEW.source_xml IS OLD.source_xml
          AND NEW.imported_at IS OLD.imported_at)
BEGIN SELECT RAISE(ABORT, 'project: only import_finished_at changes, once'); END;

CREATE TRIGGER project_no_delete BEFORE DELETE ON project
BEGIN SELECT RAISE(ABORT, 'project: rows are never deleted'); END;

CREATE TABLE task (
    project_id       INTEGER NOT NULL,
    uid              INTEGER NOT NULL,
    name             TEXT,
    wbs              TEXT,
    outline_level    INTEGER,
    is_summary       INTEGER NOT NULL,
    is_milestone     INTEGER NOT NULL,
    planned_start    TEXT,
    planned_finish   TEXT,
    planned_duration TEXT,
    planned_work     TEXT,
    plan_c14n        BLOB NOT NULL,             -- C14N bytes of the master-schedule elements
    PRIMARY KEY (project_id, uid)
) WITHOUT ROWID;

CREATE TRIGGER task_insert_new_key BEFORE INSERT ON task
WHEN EXISTS (SELECT 1 FROM task WHERE project_id = NEW.project_id AND uid = NEW.uid)
BEGIN SELECT RAISE(ABORT, 'task: already exists'); END;

CREATE TRIGGER task_insert_during_import BEFORE INSERT ON task
WHEN NOT EXISTS (SELECT 1 FROM project WHERE id = NEW.project_id AND import_finished_at IS NULL)
BEGIN SELECT RAISE(ABORT, 'task: added only while its project is being imported'); END;

CREATE TRIGGER task_no_update BEFORE UPDATE ON task
BEGIN SELECT RAISE(ABORT, 'task: rows are read-only'); END;

CREATE TRIGGER task_no_delete BEFORE DELETE ON task
BEGIN SELECT RAISE(ABORT, 'task: rows are never deleted'); END;

CREATE TABLE baseline (
    project_id INTEGER NOT NULL,
    number     INTEGER NOT NULL,
    locked_at  TEXT,                            -- set once, by baseline lock or rebaseline
    locked_by  TEXT,                            -- the command that locked it
    reason     TEXT,                            -- rebaseline's --reason; NULL for an imported baseline
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, number)
) WITHOUT ROWID;

CREATE TRIGGER baseline_insert_new_key BEFORE INSERT ON baseline
WHEN EXISTS (SELECT 1 FROM baseline WHERE project_id = NEW.project_id AND number = NEW.number)
BEGIN SELECT RAISE(ABORT, 'baseline: number already exists'); END;

CREATE TRIGGER baseline_insert_for_project BEFORE INSERT ON baseline
WHEN NOT EXISTS (SELECT 1 FROM project WHERE id = NEW.project_id)
BEGIN SELECT RAISE(ABORT, 'baseline: project does not exist'); END;

CREATE TRIGGER baseline_insert_unlocked BEFORE INSERT ON baseline
WHEN NEW.locked_at IS NOT NULL OR NEW.locked_by IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'baseline: inserted unlocked; only the one-time lock locks it'); END;

-- Column affinity is applied before this runs, so '5' arrives as 5.
CREATE TRIGGER baseline_insert_number_range BEFORE INSERT ON baseline
WHEN typeof(NEW.number) <> 'integer' OR NEW.number NOT BETWEEN 0 AND 10
BEGIN SELECT RAISE(ABORT, 'baseline: number is not an integer from 0 to 10'); END;

CREATE TRIGGER baseline_update_lock_once BEFORE UPDATE ON baseline
WHEN NOT (OLD.locked_at IS NULL AND OLD.locked_by IS NULL
          AND NEW.locked_at IS NOT NULL AND NEW.locked_by IS NOT NULL
          AND NEW.project_id IS OLD.project_id AND NEW.number IS OLD.number
          AND NEW.reason IS OLD.reason AND NEW.created_at IS OLD.created_at)
BEGIN SELECT RAISE(ABORT, 'baseline: only the one-time lock changes it'); END;

CREATE TRIGGER baseline_no_delete BEFORE DELETE ON baseline
BEGIN SELECT RAISE(ABORT, 'baseline: rows are never deleted'); END;

CREATE TABLE baseline_task (
    project_id INTEGER NOT NULL,
    number     INTEGER NOT NULL,
    task_uid   INTEGER NOT NULL,
    start      TEXT,
    finish     TEXT,
    duration   TEXT,
    work       TEXT,
    cost       TEXT,
    xml_c14n   BLOB NOT NULL,                   -- C14N bytes of the <Baseline> subtree
    PRIMARY KEY (project_id, number, task_uid)
) WITHOUT ROWID;

CREATE TRIGGER baseline_task_insert_new_key BEFORE INSERT ON baseline_task
WHEN EXISTS (SELECT 1 FROM baseline_task
             WHERE project_id = NEW.project_id AND number = NEW.number AND task_uid = NEW.task_uid)
BEGIN SELECT RAISE(ABORT, 'baseline_task: already exists'); END;

CREATE TRIGGER baseline_task_insert_unlocked BEFORE INSERT ON baseline_task
WHEN NOT EXISTS (SELECT 1 FROM baseline
                 WHERE project_id = NEW.project_id AND number = NEW.number AND locked_at IS NULL)
BEGIN SELECT RAISE(ABORT, 'baseline_task: its baseline is missing or locked'); END;

CREATE TRIGGER baseline_task_insert_for_task BEFORE INSERT ON baseline_task
WHEN NOT EXISTS (SELECT 1 FROM task WHERE project_id = NEW.project_id AND uid = NEW.task_uid)
BEGIN SELECT RAISE(ABORT, 'baseline_task: its task does not exist'); END;

CREATE TRIGGER baseline_task_no_update BEFORE UPDATE ON baseline_task
BEGIN SELECT RAISE(ABORT, 'baseline_task: rows are read-only'); END;

CREATE TRIGGER baseline_task_no_delete BEFORE DELETE ON baseline_task
BEGIN SELECT RAISE(ABORT, 'baseline_task: rows are never deleted'); END;

-- One row per task. Import seeds it with the file's progress values at
-- version 0; each applied update writes the progress fields and adds 1.
CREATE TABLE task_progress (
    project_id       INTEGER NOT NULL,
    task_uid         INTEGER NOT NULL,
    version          INTEGER NOT NULL,
    percent_complete INTEGER,
    actual_start     TEXT,                      -- MSPDI date-time, like 2026-07-16T08:00:00
    actual_finish    TEXT,
    remaining_work   TEXT,                      -- MSPDI duration, like PT96H0M0S
    notes            TEXT,
    last_update_id   INTEGER,                   -- the update applied last (M4); NULL as imported
    PRIMARY KEY (project_id, task_uid)
);

-- Sync (M4). Each item a source gives is stored once per idempotency key
-- (source, source_item_id, item_version); a run records what it captured
-- in run_item, and each key gets one outcome. A duplicate is no write.
CREATE TABLE source_update (
    id                   INTEGER PRIMARY KEY,
    source               TEXT NOT NULL,         -- the source's name, such as file
    source_item_id       TEXT NOT NULL,
    item_version         TEXT NOT NULL,
    task_uid             INTEGER,               -- these five are copies from the payload, NULL if absent or
    made_against_version INTEGER,               -- of the wrong type, kept for the report; the guard reads
    fields_json          TEXT,                  -- raw_json
    submitted_at         TEXT,
    submitted_by         TEXT,
    raw_json             TEXT NOT NULL,         -- the item as its source gave it
    problem              TEXT,                  -- why it can't be decided (not JSON, no usable key), or NULL
    first_seen_run_id    TEXT NOT NULL,
    UNIQUE (source, source_item_id, item_version)
);

CREATE TABLE outcome (
    update_id   INTEGER PRIMARY KEY,            -- source_update.id: one outcome per idempotency key
    status      TEXT NOT NULL CHECK (status IN ('applied', 'conflict')),
    reason_code TEXT,
    detail      TEXT,
    run_id      TEXT NOT NULL,
    decided_at  TEXT NOT NULL,
    before_json TEXT,                           -- an applied update's task progress, before and after, without
    after_json  TEXT                            -- the notes; after_json's notes_entry is the entry it appended
);

CREATE TABLE conflict (
    id          INTEGER PRIMARY KEY,
    update_id   INTEGER NOT NULL UNIQUE,
    reason_code TEXT NOT NULL,
    detail      TEXT,
    status      TEXT NOT NULL CHECK (status IN ('open', 'dismissed')),
    reviewed_at TEXT,
    note        TEXT                            -- the reviewer's note on dismissing it
);

CREATE TABLE sync_run (
    id          TEXT PRIMARY KEY,               -- UUID4
    trigger     TEXT NOT NULL CHECK (trigger IN ('now', 'daily', 'rerun')),
    rerun_of    TEXT,
    source      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL CHECK (status IN ('running', 'ok', 'failed', 'interrupted')),
    seen        INTEGER NOT NULL DEFAULT 0,
    applied     INTEGER NOT NULL DEFAULT 0,
    conflicts   INTEGER NOT NULL DEFAULT 0,
    duplicates  INTEGER NOT NULL DEFAULT 0,
    error       TEXT
);

CREATE TABLE run_item (                         -- what a run captured, so rerun can process it again
    run_id    TEXT NOT NULL,
    update_id INTEGER NOT NULL,
    PRIMARY KEY (run_id, update_id)
);
