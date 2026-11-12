-- Why a queued job is not being picked up (not enough free space on the worker, a share that is down, a missing feature...). Set by the claim, cleared when a worker takes the job.
ALTER TABLE job ADD COLUMN wait_reason TEXT;
