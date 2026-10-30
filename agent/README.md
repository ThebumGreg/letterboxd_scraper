# SyncHub worker agent

The agent runs on a worker PC. It asks SyncHub for jobs over HTTP, does them, and reports back. SyncHub never connects to the PC, so nothing needs to be opened or forwarded on it. Two Python files, standard library only: `synchub_agent.py` (the agent) and `media_analysis.py` (what it knows about media files; without it the agent still copies, it just cannot scan).

What it does today: **verified copies** between your shares (SyncHub's *Test copy* button), **library scans** (it lists the video files on a share, looks at the ones SyncHub does not know yet or that changed with ffprobe and ffmpeg, and reports what it found) and **duplicate searches** (it reads files of the same size to see whether they are the same). It also runs **conversions** for approved Library files (copy in, ffmpeg remux or re-encode, check, copy out to the destination share); this needs ffmpeg and ffprobe and `media_analysis.py` next to the agent.

## What it tells SyncHub

- **Its own log, on request.** The Workers page's **Logs** button asks the agent for the tail of `worker.jsonl` (and its rotated files, newest last, at most about 1 MB); the agent sends it with its next poll, a few seconds later. The page can filter by level and search it, and the log can be copied or downloaded. Only the latest one is kept.
- **The machine.** At start the agent reads the CPU (model, cores, threads, the instruction sets that matter for encoding), memory, graphics cards (`nvidia-smi`, `lspci`, `/dev/dri`), whether it runs in a container, what ffmpeg and ffprobe it found, ffmpeg's hardware decoders and which encoders are built in. It then tests each of the main ones (x264, x265, SVT-AV1 and the NVENC, Quick Sync and VAAPI ones) by encoding a test frame, so SyncHub can tell an encoder that **works** from one that is built into ffmpeg but **failed** (usually no GPU, driver or `/dev/dri` in the container). With every poll it sends the figures that change: CPU use, load, free memory, how long the PC has been up. All of it is in the **Machines** section of the Workers tab. A GPU inside the Docker image needs the device passed in (`devices: ["/dev/dri:/dev/dri"]`) and the matching ffmpeg driver packages; the tab shows what the agent can actually see.

## What it promises

- **Nothing is overwritten.** A copy goes to `<name>.synchub-partial` and is renamed only when it has been read back and its SHA-256 matches the source. An existing, different file at the destination fails the job.
- **Nothing is lost on a crash.** Each job has its own folder with a `manifest.json` (written atomically), a log, and a verified local copy of the source. After a power cut or `kill -9` the agent finds those folders, tells SyncHub what it holds, and continues from the last checkpoint (every 256 MB by default), without copying a finished step again.
- **A result is never lost.** A finished job's report is saved first and re-sent until SyncHub accepts it.
- **An unmounted share is never written to.** Each share has a `.synchub-root` marker file holding its name. The agent only uses a share whose marker matches, so an empty folder from a failed mount, or `Media` mounted where `TFiles` belongs, is refused. A hung mount (stale NFS handle, dead SMB server) is reported as "not responding" instead of freezing the agent.
- **Nothing is deleted before it is verified.** Local files are removed only after SyncHub has the result; a failed job's folder is kept (14 days by default) with its log, and SyncHub's **Fetch full log** button asks the agent for it.

## Encoding: GPU, chunks, quality scores and kept originals

- **GPU encoding.** The agent test-encodes `hevc_nvenc`, `hevc_qsv` and `hevc_vaapi` at start (`--check` lists the ones that work). A plan uses one only when SyncHub's *Library settings > GPU encoding* is on and the picture is not HDR (HDR stays on the CPU encoder, which writes the mastering data correctly). `video_encoder` (`SYNCHUB_VIDEO_ENCODER`) picks which: `auto` tries nvenc, qsv, vaapi in that order, `cpu` never uses a GPU. If the GPU encode fails or comes out over the size limit, the job is encoded again on the CPU and the job says so. Expect GPU files to be 20 to 40 percent larger than x265 at the same look. For an Intel GPU in Docker, add `examples/docker-compose.gpu.yml` (it passes `/dev/dri` through); the image already has the Intel media driver.
- **Chunked, resumable encodes.** A video longer than twice `chunk_minutes` (default 10; 0 turns it off) is encoded as video-only pieces in the job folder, then joined without re-encoding together with the source's audio, subtitles and chapters. A finished piece is never encoded again, so a reboot loses at most one. Chunks need about one extra output's worth of free space on the work disk until the join.
- **Dolby Vision 7 to 8.1 and smaller extra versions.** With `dovi_tool` and `mkvmerge` the worker converts a profile 7 film without re-encoding the picture (the video is streamed through `dovi_tool -m 2 convert --discard`, joined with the other tracks by `mkvmerge` and checked), and a plan can scale the picture down (`max_height`) to make a smaller copy beside the original (see `docs/DESIGN_ENCODING.md` 11.4).
- **Quality scores.** When the plan asks for it and ffmpeg has libvmaf (the distribution's does not: build the image with `FFMPEG_URL`, see `agent/Dockerfile`; without it the score is skipped and the job says so), `vmaf_samples` windows of 20 seconds are scored against the source (scaled to 1080p). HDR pictures are skipped. In *hold* mode a mean under 93 (95 for 2160p) pauses the job for review: approve to use the file anyway, skip or reject to keep the original.
- **Kept originals.** `work/originals/` holds each replaced original with a `meta.json`. SyncHub's Originals tab lists them (the agent sends the list with its poll) and can queue *restore*, *keep longer* and *delete now* as jobs for this worker. A restore copies the original back under its old name, checks its checksum, and moves the converted file aside into `<folder>-converted` (kept 7 days) rather than deleting it.

## Install with one script (Ubuntu or Debian, recommended)

`setup-worker.sh` does everything below for you, and does it again, safely, whenever you run it later:

```
git clone https://github.com/ThebumGreg/letterboxd_scraper.git && cd letterboxd_scraper
sudo ./agent/setup-worker.sh --url http://192.168.1.20:5001 --token sw_...
```

The address is SyncHub's, as this PC reaches it (not `localhost`); the token is the one SyncHub's *Add worker* dialog shows once (that dialog's first tab, `setup-worker.sh`, holds this exact command). The script:

1. **Installs what is missing**: `git`, `curl`, Docker with Compose, and `cifs-utils` / `nfs-common` when SyncHub's shares need them.
2. **Asks SyncHub for this worker's settings and shares** with the token, so the shares you defined on the Storage page are the ones that get mounted. A wrong token or address stops it here, before anything on the PC is changed.
3. **Pulls the latest code from GitHub** (`git pull --ff-only` in the checkout it was started from, or a fresh clone in `/opt/synchub-worker/repo`). A checkout with local changes is used as it is, with a warning. A private repository needs `export GITHUB_TOKEN=...` (read access to Contents) and `sudo -E`.
4. **Updates the shared file systems**: one marked block in `/etc/fstab` (a timestamped backup is kept; your own lines are never touched; a mount point you already have a line for is skipped with a warning), and the Unraid login in `/etc/synchub-smb.cred` (mode 600, never in `.env` or `fstab`). It then creates the mount folders and mounts the shares.
5. **Creates `.env`** (mode 600) and `docker-compose.yml` in `/opt/synchub-worker`, builds the image and starts the worker. The compose file bind-mounts the PC's mounts with `rslave`, so a share that mounts late at boot, or is remounted, shows up inside the container.
6. **Marks the shares** (`--init-roots`, only when every share is really mounted) and runs `--check`.

Options: `--mode native` (python3, ffmpeg and a systemd service instead of Docker), `--dir`, `--work-dir` (a big local disk), `--branch`, `--repo`, `--smb-user` / `--smb-password` (or `SMB_USER` / `SMB_PASSWORD`, or it asks), `--no-mounts` (shares already mounted), `--no-init-roots`, `--no-start`, and `--dry-run` (prints what it would do; still asks SyncHub, which proves the address and token). `--help` lists them all. Exit status: 0 done, 1 failed, 3 done with something to look at (the summary says what).

To update a worker later: run the same script again. It needs no arguments the second time (the address and token are read from the `.env` it wrote).

## Upgrading a worker

If you set the worker up yourself (your own `/etc/fstab` lines, a customised `docker-compose.yml`, bind-mounted host paths), do **not** run `setup-worker.sh` again: it rewrites `/etc/fstab` and the compose file. Use `update-worker.sh`, which only pulls the latest code and rebuilds the container. It never touches `/etc/fstab`, the compose file, `.env` or your mounts.

### Two folders, two jobs

| Folder (worker-1) | What it is | What you run there |
|---|---|---|
| `/opt/synchub-worker` | the **Docker folder**: `docker-compose.yml`, `.env`, `work/`. **Not a git repository.** | `docker compose` commands: `ps`, `logs`, `up -d`, `run --rm worker --check` (with `sudo`) |
| `~/letterboxd_scraper` (`/home/greg/letterboxd_scraper`) | the **git checkout** the compose file builds from (`build: context: ~/letterboxd_scraper/agent`) | `git pull` and `update-worker.sh` (run as yourself, `sudo` only on the script) |

Never run `git` in `/opt/synchub-worker` ("fatal: not a git repository"), never run `git clone` again if `~/letterboxd_scraper` exists, and never use `sudo git`. A second, root-owned clone under `/opt/synchub-worker/letterboxd_scraper` is not used by the worker: ignore it. To check which checkout a worker builds from: `cd /opt/synchub-worker && sudo docker compose config | grep -A2 build`.

### The commands, in order

```
# 1. In the git checkout: look at the worker, then update it
cd ~/letterboxd_scraper
git pull --ff-only origin main
sudo ./agent/update-worker.sh --dir /opt/synchub-worker --report      # read-only snapshot
sudo ./agent/update-worker.sh --dir /opt/synchub-worker --dry-run     # shows what it would do
sudo ./agent/update-worker.sh --dir /opt/synchub-worker               # rebuilds and restarts

# 2. In the Docker folder: check it is healthy
cd /opt/synchub-worker
sudo docker compose ps                                                # want: Up ... (healthy)
sudo docker inspect --format '{{.State.Health.Status}}  restarts: {{.RestartCount}}' synchub-worker
sudo docker compose logs --tail 30
sudo docker compose run --rm worker --check                           # token, SyncHub, ffmpeg, shares
```

If `git pull` complains about local changes, see `git diff` first, then `git stash push -m "worker local edits" <file>`. If the log says "SyncHub does not accept this worker's token", make a new token on SyncHub's Workers page (More, New token) and put it in `SYNCHUB_TOKEN=` in `/opt/synchub-worker/.env`, then `sudo docker compose up -d`. The first-time clone, once, only if the folder is missing: `git clone https://github.com/ThebumGreg/letterboxd_scraper.git ~/letterboxd_scraper`.

Options of `update-worker.sh`: `--dir` (the Docker folder, default `/opt/synchub-worker`), `--branch`, `--no-pull`, `--dry-run`, `--report`. The agent code is baked into the image, so a plain `docker compose restart` does not update it. The full procedure, with a table of what each message means and how to roll back, is in [docs/UPGRADE.md](../docs/UPGRADE.md).

## Install on Ubuntu with Docker, by hand

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
       agent/                 the "agent" folder of the SyncHub repository (Dockerfile, synchub_agent.py, media_analysis.py)
   ```

4. Start it, then mark the shares once:

   ```
   docker compose up -d --build
   docker compose run --rm worker --init-roots     # writes .synchub-root into each share (never creates a folder)
   docker compose run --rm worker --check          # says what works and what does not
   ```

   The Workers page now shows the worker online with each share "ready". Use **Test copy** to prove the path from end to end with a small file.

Useful afterwards: **SyncHub's Workers page** (the **Logs** button fetches this worker's log from SyncHub, and the **Machines** section under the workers list shows its CPU, memory, graphics card and which encoders work), `docker compose logs -f` (live), `work/logs/worker.jsonl` (JSON lines, rotated 10 MB x 5), `work/jobs/<id>-xxxxxxxx/job.log` (one job's log), `docker compose restart` (safe at any moment: jobs resume).

## Install without Docker

`sudo ./agent/setup-worker.sh --mode native ...` does all of this (system user, `/opt/synchub-agent`, `/etc/synchub-agent.ini`, the systemd service, the fstab lines). By hand: copy `synchub-agent.ini` (from SyncHub's dialog) to `/etc/synchub-agent.ini`, mount the shares with the `fstab` lines from the same dialog, and use `synchub-agent.service` (see the comments at its top). `python3 synchub_agent.py --check` and `--init-roots` work the same way.

## Settings

A file in INI style (`--config`, or `./synchub-agent.ini`, or `/etc/synchub-agent.ini`); environment variables override it.

| Setting (`[section]`) | Environment | Default | Meaning |
|---|---|---|---|
| `[synchub] url` | `SYNCHUB_URL` | required | SyncHub's address, as this PC reaches it (not `localhost`) |
| `[synchub] token` | `SYNCHUB_TOKEN` | required | The worker's token (shown once on the Workers page) |
| `[synchub] name` | `SYNCHUB_NAME` | | Informational; SyncHub names a worker by its token |
| `[synchub] use_proxy` | `SYNCHUB_USE_PROXY` | `no` | Use the system HTTP proxy to reach SyncHub (normally wrong on a LAN) |
| `[worker] work_dir` | `SYNCHUB_WORK` | `./work` (`/work` in Docker) | Job folders, logs and `originals/` (files replaced in place, kept for the retention SyncHub sets); use a big local disk |
| `[worker] slots` | `SYNCHUB_SLOTS` | `1` | Jobs at once (SyncHub's own limit for the worker also applies) |
| `[worker] scan_threads` | `SYNCHUB_SCAN_THREADS` | `2` | Files a scan analyses at the same time (each is a short ffprobe and ffmpeg run; most of the time is the share) |
| `[worker] scan_batch` | `SYNCHUB_SCAN_BATCH` | `25` | How many analysed files are reported to SyncHub at a time, so an interrupted scan loses at most this many |
| `[worker] max_copy_mb_s` | `SYNCHUB_MAX_COPY_MB_S` | `0` (no limit) | Cap copy speed so a big copy cannot take the whole network |
| `[worker] verify` | `SYNCHUB_VERIFY` | `hash` | `hash` reads every copy back and compares SHA-256; `size` only compares sizes |
| `[worker] copy_chunk_mb` / `checkpoint_mb` | `SYNCHUB_COPY_CHUNK_MB` / `SYNCHUB_CHECKPOINT_MB` | `8` / `256` | Block size; how often a resumable checkpoint is saved |
| `[worker] done_retention_hours` | `SYNCHUB_DONE_RETENTION_HOURS` | `1` | How long a finished job's folder (log and manifest, no big files) is kept |
| `[worker] failed_retention_days` | `SYNCHUB_FAILED_RETENTION_DAYS` | `14` | How long a failed or cancelled job's folder and files are kept |
| `[worker] log_level`, `log_max_mb`, `log_backups` | `SYNCHUB_LOG_LEVEL`... | `INFO`, `10`, `5` | The worker log |
| `[worker] ffmpeg`, `ffprobe` | `SYNCHUB_FFMPEG`... | `ffmpeg`, `ffprobe` | Where they are, if not on the PATH |
| `[worker] dovi_tool`, `mkvmerge` | `SYNCHUB_DOVI_TOOL`, `SYNCHUB_MKVMERGE` | `dovi_tool`, `mkvmerge` | Where they are, if not on the PATH. When both run, the worker offers Dolby Vision profile 7 to 8.1 jobs (feature `dovi`) |
| `[roots] <name>` | `SYNCHUB_ROOT_<NAME>` | SyncHub's mount point | Only if a share is mounted somewhere else on this PC |

## Surviving a network outage

A worker that boots before the network is up, or whose Unraid server restarts, ends up with empty or stale share folders and reports every share as "no marker file". Three layers keep that from needing you:

1. **Mount on use, with retries.** Add `x-systemd.automount,x-systemd.requires=network-online.target,x-systemd.mount-timeout=30,x-systemd.idle-timeout=0` to the options of each share's `/etc/fstab` line (the lines SyncHub generates do not have them yet), then `sudo systemctl daemon-reload`. A failed mount at boot is tried again the next time the folder is used. Make sure the host waits for the network at boot: `sudo systemctl enable systemd-networkd-wait-online.service` (or `NetworkManager-wait-online.service`, whichever your PC uses).
2. **The mount watchdog** (`agent/mount-watchdog.sh`, a host script, not part of the container). From `~/letterboxd_scraper`: `sudo ./agent/mount-watchdog.sh --install` (add `--dry-run` first to see what it writes). A systemd timer then runs one check a minute: a share whose `.synchub-root` marker cannot be read is lazily unmounted (if it hangs) and mounted again from `/etc/fstab`; once every share is back, a worker container that still cannot see the markers is restarted (at most once in ten minutes), and a container that failed to start because a share was missing is started. It never creates a marker, and a share that is mounted but has no marker (not set up yet, or the wrong share) is only logged, never remounted. Settings, if you do not use `/mnt/tower/Media` and `/mnt/tower/TFiles` or the container is not called `synchub-worker`, go in `/etc/synchub-mount-watchdog.conf` (`SYNCHUB_MOUNT_PATHS`, `SYNCHUB_CONTAINER`). Look: `sudo ./agent/mount-watchdog.sh --status` or `journalctl -t synchub-watchdog`. Remove: `--uninstall`.
3. **The agent itself** already copes: it checks every share every 15 seconds, answers "not responding" instead of freezing on a hung mount, keeps trying to reach SyncHub with a growing pause, and a job that finds a share not ready fails as *transient* and is tried again. Nothing in the agent mounts anything; that is the host's job.

If your compose file bind-mounts host folders (`/mnt/tower/Media:/mnt/tower/Media`), add `:rslave` (`/mnt/tower/Media:/mnt/tower/Media:rslave`) so a share mounted or remounted on the host after the container started shows up inside it without a restart. The watchdog's restart is the fallback.

Do **not** make Docker depend on the mounts (`RequiresMountsFor=` in a `docker.service` drop-in). It is a hard dependency: with the server down at boot, Docker, and every container on the PC, would not start.

## When something is wrong

| You see | Likely cause and fix |
|---|---|
| `docker compose up` fails mounting a volume | Wrong address, share name, user or password in `.env`; `cifs-utils` / `nfs-common` missing on the PC; for SMB try `vers=3.1.1` or `vers=2.1` in the share's options on SyncHub's Storage page. A password with `$` needs `$$`, with a comma needs two commas. |
| Share "no marker file" | The share is mounted but not set up: `docker compose run --rm worker --init-roots`. Or it is **not mounted** and you are looking at an empty folder: fix the mount first. |
| Share "wrong share" | The marker names a different share: the mounts are mixed up. |
| Shares "no marker file" after a reboot or a network outage | `journalctl -b -u mnt-tower-Media.mount` shows `Network is unreachable`: the mount ran before the network. See "Surviving a network outage" above; right now: `sudo umount -f -l /mnt/tower/Media /mnt/tower/TFiles; sudo mount -a`, then `docker compose restart` in the Docker folder. Do not run `--init-roots` on a folder that is not mounted. |
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
