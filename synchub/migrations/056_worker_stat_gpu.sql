-- How busy the worker's GPU was at each history point (percent), for the GPU chart on the worker Stats page. NULL when the worker reports no GPU load.
ALTER TABLE worker_stat ADD COLUMN gpu_percent REAL;
