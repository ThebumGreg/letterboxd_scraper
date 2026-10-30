-- "Run benchmark" on the Workers page: the web UI sets benchmark_requested, the worker runs it with its next idle poll and uploads the report.
ALTER TABLE worker ADD COLUMN benchmark_requested INTEGER NOT NULL DEFAULT 0;

-- The latest results per worker (the newest ten are kept). `report` is the worker's JSON report; `ok` and `summary` are what the Workers page shows at a glance.
CREATE TABLE worker_benchmark (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id   INTEGER NOT NULL REFERENCES worker (id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL,
    ok          INTEGER NOT NULL DEFAULT 0,
    summary     TEXT,
    report      TEXT NOT NULL
);
CREATE INDEX idx_worker_benchmark_worker ON worker_benchmark (worker_id, id);
