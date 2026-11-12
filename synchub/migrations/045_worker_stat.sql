-- A short history of each worker's health (kept 14 days): CPU, temperature, free work space, what it was running. One row every five minutes while the worker checks in.
CREATE TABLE IF NOT EXISTS worker_stat (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id    INTEGER NOT NULL,
    at           TEXT    NOT NULL,
    cpu_percent  REAL,
    temp_cpu     REAL,
    temp_max     REAL,
    mem_available INTEGER,
    work_free    INTEGER,
    running      INTEGER,
    gpu_encodes  INTEGER,
    gpu_mhz      INTEGER
);
CREATE INDEX IF NOT EXISTS ix_worker_stat_worker_at ON worker_stat (worker_id, at);
