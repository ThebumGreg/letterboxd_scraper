-- Worker updates and benchmarks, shown on the Jobs page next to the real jobs. They are kept apart from the `job` table on purpose: the job
-- queue hands work out, leases it and retries it, and none of that applies to "update this PC" or "benchmark this worker".
CREATE TABLE worker_task (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id   INTEGER REFERENCES worker (id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('update', 'benchmark')),
    status      TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
    source      TEXT NOT NULL DEFAULT 'manual',          -- manual (a button), auto (the hourly check)
    requested_by TEXT,
    message     TEXT,                                    -- the one line the Jobs page shows
    error       TEXT,
    log         TEXT,                                    -- the updater's log lines, or the benchmark's results
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT,
    updated_at  TEXT NOT NULL
);
CREATE INDEX idx_worker_task_worker ON worker_task (worker_id, kind, status);
