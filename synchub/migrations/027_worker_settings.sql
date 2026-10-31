-- Settings for a worker that are chosen in SyncHub (JSON), and a request for the worker's PC to update or restart it.
ALTER TABLE worker ADD COLUMN settings TEXT;
ALTER TABLE worker ADD COLUMN host_request TEXT;
