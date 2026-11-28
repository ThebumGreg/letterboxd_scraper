# Status for Homepage and Uptime Kuma

Two read-only endpoints, `GET` only (anything else gets 405). Neither uses SyncHub's login, cookies or profiles, and neither writes anything.

| Endpoint | Sign-in | Answer |
| --- | --- | --- |
| `/healthz` | none | `200 {"status":"ok"}`, or `503 {"status":"down"}` when the database does not answer. Nothing else in the body. |
| `/api/status` | `Authorization: Bearer <STATUS_TOKEN>` | The report below. `404` while `STATUS_TOKEN` is not set, `401 {"error":"unauthorized"}` for a missing or wrong token. |

## Turning it on

```
openssl rand -hex 32            # copy the result
```

Put it in `.env` next to `docker-compose.yml` as `STATUS_TOKEN=<the result>` (on Unraid, add a variable called `STATUS_TOKEN` to the container) and restart SyncHub. Do not commit it. SyncHub's own `SYNCHUB_ACCESS_TOKEN` is separate and does not apply to these two addresses.

## The report

```json
{
  "app": "synchub",
  "version": "1.9.12 (a1b2c3d)",
  "status": "ok",
  "checked_at": "2026-11-28T10:20:00Z",
  "uptime_seconds": 86400,
  "checks": {
    "database": {"status": "ok", "latency_ms": 3},
    "workers": {"status": "ok"},
    "jellyfin": {"status": "ok", "latency_ms": 41}
  },
  "metrics": {
    "jobs_queued": 2,
    "jobs_running": 1,
    "jobs_failed_24h": 0,
    "workers_online": 2,
    "workers_total": 2,
    "last_job_finished_at": "2026-11-28T09:58:12Z"
  }
}
```

`status` is the worst of the checks. `database` down is `down` (HTTP 503). `workers` is `degraded` when workers are registered but none has polled in the last minute. `jellyfin` (only when a Jellyfin address is set) is `degraded` when its public ping fails; it sends no key. Each check has a 2 second limit, and the whole report is kept for 15 seconds, so polling every 30 seconds costs the database almost nothing. Unknown values are `null`.

Where the numbers come from (the `job` and `worker` tables): `jobs_queued` and `jobs_running` are jobs in that state; `jobs_failed_24h` counts jobs that failed in the last 24 hours; `workers_online` are enabled workers that polled within 60 seconds, `workers_total` all enabled ones; `last_job_finished_at` is the newest finished job that succeeded.

Try it:

```
curl -i http://UNRAID_IP:5001/healthz
curl -i http://UNRAID_IP:5001/api/status                                    # 404 until STATUS_TOKEN is set, then 401
curl -i -H "Authorization: Bearer $STATUS_TOKEN" http://UNRAID_IP:5001/api/status
```

## Homepage

In Homepage's `.env` (or docker-compose) add `HOMEPAGE_VAR_SYNCHUB_STATUS_TOKEN=<the token>`, then in `services.yaml`:

```yaml
- SyncHub:
    icon: mdi-sync
    href: http://UNRAID_IP:5001
    siteMonitor: http://UNRAID_IP:5001/healthz
    widget:
      type: customapi
      url: http://UNRAID_IP:5001/api/status
      refreshInterval: 30000
      headers:
        Authorization: "Bearer {{HOMEPAGE_VAR_SYNCHUB_STATUS_TOKEN}}"
      mappings:
        - field: status
          label: Status
          format: text
        - field: metrics.jobs_queued
          label: Queued
          format: number
        - field: metrics.jobs_running
          label: Running
          format: number
        - field: metrics.jobs_failed_24h
          label: Failed 24h
          format: number
        - field: metrics.workers_online
          label: Workers online
          format: number
        - field: metrics.last_job_finished_at
          label: Last job
          format: relativeDate
        - field: uptime_seconds
          label: Uptime
          format: duration
```

## Uptime Kuma

1. **HTTP(s)** monitor, URL `http://UNRAID_IP:5001/healthz`, accepted status codes `200-299`, interval 60 seconds. This is the one to alert on.
2. Optional **HTTP(s) - Json Query** monitor, URL `http://UNRAID_IP:5001/api/status`, Headers `{"Authorization": "Bearer <STATUS_TOKEN>"}`, Json Query `status`, expected value `ok`. It turns red for `degraded` too (for example when every worker is offline), so it is a good second, quieter alert.
