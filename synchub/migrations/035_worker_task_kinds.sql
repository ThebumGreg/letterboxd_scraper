-- Worker tasks on the Jobs page now also cover restarting a worker and setting up its camera relay (the PC's helper does both), so the CHECK
-- on `kind` grows. SQLite cannot change a CHECK in place: the table is rebuilt with the same columns and rows.
CREATE TABLE worker_task_new (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id   INTEGER REFERENCES worker (id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('update', 'benchmark', 'restart', 'relay')),
    status      TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
    source      TEXT NOT NULL DEFAULT 'manual',
    requested_by TEXT,
    message     TEXT,
    error       TEXT,
    log         TEXT,
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT,
    updated_at  TEXT NOT NULL
);
INSERT INTO worker_task_new SELECT id, worker_id, kind, status, source, requested_by, message, error, log, created_at, started_at, finished_at, updated_at FROM worker_task;
DROP TABLE worker_task;
ALTER TABLE worker_task_new RENAME TO worker_task;
CREATE INDEX idx_worker_task_worker ON worker_task (worker_id, kind, status);
