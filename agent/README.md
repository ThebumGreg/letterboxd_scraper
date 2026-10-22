# SyncHub worker agent

The agent runs on a worker PC. It asks SyncHub for jobs over HTTP, does them, and reports back. SyncHub never connects to the PC, so nothing needs to be opened or forwarded on it. One Python file (`synchub_agent.py`), standard library only.

What it does today: **verified copies** between your shares (SyncHub's *Test copy* button, and later the library and download jobs). Encoding jobs arrive in a later phase; the agent already detects ffmpeg and the encoders that work.

## What it promises

- **Nothing is overwritten.** A copy goes to `<name>.synchub-partial` and is renamed only when it has been read back and its SHA-256 matches the source. An existing, different file at the destination fails the job.
- **Nothing is lost on a crash.** Each job has its own folder with a `manifest.json` (written atomically), a log, and a verified local copy of the source. After a power cut or `kill -9` the agent finds those folders, tells SyncHub what it holds, and continues from the last checkpoint (every 256 MB by default), without copying a finished step again.
- **A result is never lost.** A finished job's report is saved first and re-sent until SyncHub accepts it.
- **An unmounted share is never written to.** Each share has a `.synchub-root` marker file holding its name. The agent only uses a share whose marker matches, so an empty folder from a failed mount, or `Media` mounted where `TFiles` belongs, is refused. A hung mount (stale NFS handle, dead SMB server) is reported as "not responding" instead of freezing the agent.
- **Nothing is deleted before it is verified.** Local files are removed only after SyncHub has the result; a failed job's folder is kept (14 days by default) with its log, and SyncHub's **Fetch full log** button asks the agent for it.

## Install on Ubuntu with Docker (recommended)

1. On the worker PC:

   ```
   sudo apt install docker.io docker-compose-v2 cifs-utils      # use nfs-common instead of cifs-utils for NFS shares
   ```

   (`cifs-utils` / `nfs-common` belong on the PC itself: Docker's volume driver calls the PC's mount command.)

2. In SyncHub: **Storage** page → add each Unraid share (SMB is the one to start with). **Workers** page → *Add worker*. The *Setup files* dialog shows the worker's token once, together with the files below.

3. On the worker PC make a folder and put these in it:

   ```
   synchub-worker/
       docker-compose.yml     from SyncHub's dialog
       .env                   from SyncHub's dialog; fill in SMB_USER and SMB_PASSWORD, then:  chmod 600 .env
       agent/                 the "agent" folder of the SyncHub repository (Dockerfile + synchub_agent.py)
   ```

4. Start it, then mark the shares once:

   ```
   docker compose up -d --build
   docker compose run --rm worker --init-roots     # writes .synchub-root into each share (never creates a folder)
   docker compose run --rm worker --check          # says what works and what does not
   ```

   The Workers page now shows the worker online with each share "ready". Use **Test copy** to prove the path from end to end with a small file.

Useful afterwards: `docker compose logs -f` (live), `work/logs/worker.jsonl` (JSON lines, rotated 10 MB x 5), `work/jobs/<id>-xxxxxxxx/job.log` (one job's log), `docker compose restart` (safe at any moment: jobs resume).

## Install without Docker

Copy `synchub-agent.ini` (from SyncHub's dialog) to `/etc/synchub-agent.ini`, mount the shares with the `fstab` lines from the same dialog, and use `synchub-agent.service` (see the comments at its top). `python3 synchub_agent.py --check` and `--init-roots` work the same way.

## Settings

A file in INI style (`--config`, or `./synchub-agent.ini`, or `/etc/synchub-agent.ini`); environment variables override it.

| Setting (`[section]`) | Environment | Default | Meaning |
|---|---|---|---|
| `[synchub] url` | `SYNCHUB_URL` | required | SyncHub's address, as this PC reaches it (not `localhost`) |
| `[synchub] token` | `SYNCHUB_TOKEN` | required | The worker's token (shown once on the Workers page) |
| `[synchub] name` | `SYNCHUB_NAME` | | Informational; SyncHub names a worker by its token |
| `[synchub] use_proxy` | `SYNCHUB_USE_PROXY` | `no` | Use the system HTTP proxy to reach SyncHub (normally wrong on a LAN) |
| `[worker] work_dir` | `SYNCHUB_WORK` | `./work` (`/work` in Docker) | Job folders, logs; use a big local disk |
| `[worker] slots` | `SYNCHUB_SLOTS` | `1` | Jobs at once (SyncHub's own limit for the worker also applies) |
| `[worker] max_copy_mb_s` | `SYNCHUB_MAX_COPY_MB_S` | `0` (no limit) | Cap copy speed so a big copy cannot take the whole network |
| `[worker] verify` | `SYNCHUB_VERIFY` | `hash` | `hash` reads every copy back and compares SHA-256; `size` only compares sizes |
| `[worker] copy_chunk_mb` / `checkpoint_mb` | `SYNCHUB_COPY_CHUNK_MB` / `SYNCHUB_CHECKPOINT_MB` | `8` / `256` | Block size; how often a resumable checkpoint is saved |
| `[worker] done_retention_hours` | `SYNCHUB_DONE_RETENTION_HOURS` | `1` | How long a finished job's folder (log and manifest, no big files) is kept |
| `[worker] failed_retention_days` | `SYNCHUB_FAILED_RETENTION_DAYS` | `14` | How long a failed or cancelled job's folder and files are kept |
| `[worker] log_level`, `log_max_mb`, `log_backups` | `SYNCHUB_LOG_LEVEL`... | `INFO`, `10`, `5` | The worker log |
| `[worker] ffmpeg`, `ffprobe` | `SYNCHUB_FFMPEG`... | `ffmpeg`, `ffprobe` | Where they are, if not on the PATH |
| `[roots] <name>` | `SYNCHUB_ROOT_<NAME>` | SyncHub's mount point | Only if a share is mounted somewhere else on this PC |

## When something is wrong

| You see | Likely cause and fix |
|---|---|
| `docker compose up` fails mounting a volume | Wrong address, share name, user or password in `.env`; `cifs-utils` / `nfs-common` missing on the PC; for SMB try `vers=3.1.1` or `vers=2.1` in the share's options on SyncHub's Storage page. A password with `$` needs `$$`, with a comma needs two commas. |
| Share "no marker file" | The share is mounted but not set up: `docker compose run --rm worker --init-roots`. Or it is **not mounted** and you are looking at an empty folder: fix the mount first. |
| Share "wrong share" | The marker names a different share: the mounts are mixed up. |
| Share "not responding" | A stale or hung mount. For NFS on Unraid, "stale file handle" reports are linked to the mover; use NFSv4 or SMB. |
| Worker online but jobs wait | The job's shares are not "ready" on this worker (the Jobs list says which). |
| 401 in the log | The token was replaced or revoked: use the current one and restart. |

## Folder layout under the work folder

```
work/jobs/<job-id>-<hash>/    manifest.json  job.log  source/ (the verified local copy, removed once SyncHub has the result)
work/logs/worker.jsonl        (+ .1 .. .5)
work/alive                    touched every loop, for the Docker health check
```

`examples/` holds sample setup files generated by SyncHub for a made-up worker (`env.example` is the sample `.env`). The real ones, with your token and shares, come from the Workers page.
