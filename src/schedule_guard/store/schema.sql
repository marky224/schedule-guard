-- schedule-guard SQLite schema, version 1 (PLAN.md section 4).
--
-- project, task, baseline and baseline_task hold the imported file, the
-- master schedule and the baselines. Only import, baseline lock and
-- rebaseline write them; the triggers refuse every other write, on any
-- connection that leaves triggers enabled (the default).
--
-- With SQLite's default recursive_triggers = OFF, a REPLACE deletes the row
-- it conflicts with without firing that row's DELETE trigger. So every
-- BEFORE INSERT trigger here refuses a primary key that already exists, and
-- the four tables are WITHOUT ROWID with no unique index besides their
-- primary key, leaving no other key to conflict on. Never add a UNIQUE
-- constraint or a CREATE UNIQUE INDEX to them.

CREATE TABLE project (
    id                 INTEGER NOT NULL PRIMARY KEY,
    name               TEXT,
    source_sha256      TEXT NOT NULL,
    source_xml         BLOB NOT NULL,           -- the imported file, byte for byte
    imported_at        TEXT NOT NULL,
    import_finished_at TEXT                     -- set once, as the last step of import
) WITHOUT ROWID;

CREATE TRIGGER project_insert_new_key BEFORE INSERT ON project
WHEN EXISTS (SELECT 1 FROM project WHERE id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'project: id already exists'); END;

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
