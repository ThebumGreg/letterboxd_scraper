# Upgrading SyncHub and its workers

Two things get upgraded, in this order: the **SyncHub app** (Unraid) first, then every **worker**. SyncHub runs database migrations when it starts, and a new SyncHub can talk to an older worker, but a new feature (a new job type, a new rule) only works once the worker has the matching agent code. The Workers page shows each worker's agent version and what it can do; a job a worker cannot do fails with "Update the agent".

The code only ever comes from GitHub. SyncHub never sends code or settings to a worker, so a worker does not update itself: you rebuild it.

## 1. Update SyncHub (Unraid)

1. Note the current version (footer of any page, or *Settings*), and make sure a recent backup exists (SyncHub makes one before each migration; `/data/backups`).
2. Unraid, Docker tab: *Check for updates*, then *apply update* on `synchub` (it follows `ghcr.io/thebumgreg/synchub:latest`). If you build the container from a checkout instead: `git pull --ff-only` and `docker compose up -d --build`.
3. Open SyncHub. A healthy container, a loading Library page and no red banner on the Workers page mean it worked. The container log (`docker logs synchub`) names any migration that ran.

## 2. Update a worker

Do this per worker. Worker-1 (`/opt/synchub-worker`) uses a hand-made setup, so it uses `update-worker.sh`, never `setup-worker.sh` (which rewrites `/etc/fstab` and the compose file).

### Worker-1: the paths, exactly

Two folders matter, and they are not the same one:

| Folder | What it is | Run `git` here? |
|---|---|---|
| `/opt/synchub-worker` | the worker's compose file (`docker-compose.yml`), `.env` and `work/` | **No.** It is not a git repository ("fatal: not a git repository"). It is only passed to the script with `--dir`. |
| `/home/greg/letterboxd_scraper` (`~/letterboxd_scraper`) | the git checkout the compose file builds from (`build: context: /home/greg/letterboxd_scraper/agent`) | **Yes.** Pull and run the script from here. |

Everything on worker-1 is therefore run from `~/letterboxd_scraper`:

```
cd ~/letterboxd_scraper
git pull --ff-only origin main
sudo ./agent/update-worker.sh --dir /opt/synchub-worker --dry-run
sudo ./agent/update-worker.sh --dir /opt/synchub-worker
```

Do not run `git clone` again (the folder exists) and do not use `sudo git`; the checkout belongs to your own user. If you ever find a second clone under `/opt/synchub-worker/letterboxd_scraper` (owned by root, "dubious ownership" errors from git), it is not used by the worker: leave it alone, or move it out of the way with `sudo mv /opt/synchub-worker/letterboxd_scraper /opt/synchub-worker/letterboxd_scraper.old`.

On another worker, find the checkout the same way:

```
cd /path/to/the/worker-folder && sudo docker compose config | grep -A2 build     # "context: <checkout>/agent"
```

### Step 1: look at the current state first

```
cd ~/letterboxd_scraper
sudo ./agent/update-worker.sh --dir /opt/synchub-worker --report
```

`--report` changes nothing. It prints the checkout's commit and local changes, whether it is behind GitHub, the agent version, the compose file's build folder and mounts, the container's health and restart count, the mounts under `/mnt/tower`, free space and the last 40 log lines. Read it for these:

| What you see | What it means | What to do |
|---|---|---|
| `M agent/synchub_agent.py` (or any ` M` line) | hand edits in the checkout; the update will refuse | `git diff <file>` to see them; if unwanted, `git stash push -m "worker local edits" <file>` |
| `N commit(s) behind upstream` | there is something to pull | continue |
| `(none)` under mounts | the shares are not mounted | fix the mount first (`sudo mount -a`; for "stale file handle": `sudo umount -f -l /mnt/tower/media`, then `sudo mount -a`) |
| health `unhealthy`, or many restarts | the worker is already unwell | send the report before changing anything |
| the build `context:` is not `~/letterboxd_scraper/agent` | the compose file builds from another folder | the script copies the agent files there; check that is what you want |

### Step 2: rehearse

```
sudo ./agent/update-worker.sh --dir /opt/synchub-worker --dry-run
```

It prints the pull and rebuild it would do and changes nothing.

### Step 3: update

```
sudo ./agent/update-worker.sh --dir /opt/synchub-worker 2>&1 | tee ~/worker-update.log
```

It fast-forwards the checkout (refusing if there are local changes or diverged commits), finds the build folder from the compose file, runs `docker compose up -d --build`, and waits up to 90 seconds for the container to be healthy. It never touches `/etc/fstab`, the compose file, `.env` or the mounts. It ends with `Worker is healthy` or `ERROR:` with the last log lines.

If a job is running, Docker's stop waits up to 90 seconds for the agent to stop at a safe point; unfinished jobs resume when it starts again.

### Step 4: check

1. Workers page: the worker is online with the new agent version, and lists the job types you expect (for example `convert`, `replace`).
2. `sudo ./agent/update-worker.sh --dir /opt/synchub-worker --report` once more: health `healthy`, mounts present, no restarts.
3. Run a small job or *Check* on the Workers page.

### If the worker does not come online

Collect, in this order, and send it with the `--report` output:

```
cd /opt/synchub-worker
sudo docker compose ps
sudo docker compose logs --tail 80
sudo docker compose run --rm worker --check        # says in plain words what fails
mount | grep /mnt/tower
docker logs --tail 100 synchub                      # on Unraid: migration errors, tracebacks near /api/worker/poll
```

| The log or `--check` says | Cause and fix |
|---|---|
| `SyncHub does not accept this worker's token` / `[!!] SyncHub accepts the token` | SyncHub only keeps a hash of each token, so a replaced, revoked or re-added worker's old token is refused. On the Workers page use Worker-1, **More, New token** (shown once), put it in `SYNCHUB_TOKEN=` in `/opt/synchub-worker/.env`, then `sudo docker compose up -d` and run `--check` again. This happened on the first upgrade of worker-1. |
| Connection refused / timed out | wrong `SYNCHUB_URL` in `.env`, or SyncHub is down: `curl http://UNRAID_IP:5001/healthz` from the worker should print `{"ok":true,...}` |
| a share is "not mounted" / "stale file handle" | `sudo umount -f -l /mnt/tower/media`, `sudo mount -a`, `sudo docker compose restart` |
| "Update the agent" in a failed job | the worker is older than SyncHub expects: update it (section 2) |

### If something goes wrong after the update

Send the thread the output of `--report` and `~/worker-update.log`. To go back to the previous code:

```
cd ~/letterboxd_scraper && git log --oneline -5      # find the previous commit
git checkout <previous-commit>
sudo ./agent/update-worker.sh --dir /opt/synchub-worker --no-pull
git checkout main                                   # when you want to follow main again
```

## 3. Rules of thumb

- Update SyncHub first, workers second.
- Keep the checkout clean: edit code in the repository, not on the worker. Hand edits make the pull fail (that is the safety check working).
- `docker compose restart` does not update anything: the agent code is baked into the image, so only a rebuild does.
- Do not run `setup-worker.sh` on a worker that has a hand-made `/etc/fstab` or compose file. Use it only for a fresh PC.

## 4. Proposal: automatic worker updates (not built)

Workers already report their agent version to SyncHub. A possible design: SyncHub shows "update available" per worker (its version is behind the repository's), and a worker with an *Auto-update* switch runs a small host-side updater, a systemd timer that calls `update-worker.sh --no-pull` after a `git pull --ff-only`, only when the worker is idle (no running job). A failed health check afterwards would roll back to the previous commit and tell SyncHub. A container cannot safely rebuild itself, which is why the updater would live on the host. Say if you want this built; the manual flow above stays as the fallback.
