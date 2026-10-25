-- "Get logs" on the Workers page: the web UI sets log_requested, the worker uploads the tail of its own log with its next poll.
-- (Job logs are separate: see job_log in 005.) Only the latest upload per worker is kept.
ALTER TABLE worker ADD COLUMN log_requested INTEGER NOT NULL DEFAULT 0;

CREATE TABLE worker_log (
    worker_id   INTEGER PRIMARY KEY REFERENCES worker (id) ON DELETE CASCADE,
    received_at TEXT NOT NULL,
    truncated   INTEGER NOT NULL DEFAULT 0,
    content     TEXT NOT NULL
);
