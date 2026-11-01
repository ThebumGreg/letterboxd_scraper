# Workers: setting up, updating and looking after them

A worker is a PC (Ubuntu or Debian) that does the heavy jobs: copying, scanning, converting. It asks SyncHub for work over HTTP, so nothing on the PC needs to be opened. This is the one guide for it. What the agent program does and promises is in [agent/README.md](../agent/README.md).

**The short version:** you touch the PC once, with one command. After that everything is done on SyncHub's **Workers** page: settings, **Update**, **Restart worker**, **Logs**, **Run benchmark**.

## 1. Before you start

1. In SyncHub, **Storage**: add your Unraid shares (for example `Media` and `TFiles`) and, once, the **Share login (SMB)**: the Unraid user and password. SyncHub keeps it encrypted (key file `synchub.key` in the data folder: keep it with your backups) and gives it only to a worker that proves its token. Skip the login if you prefer to type it on each new PC: the script then asks.
2. **Workers → Add worker.** Give it a name and look through **Settings** in the form (encoder, GPU encodes at once, copy speed limit, update window; the defaults are fine). Copy the **token**: it is shown once.

## 2. Install (one command, one time)

On the worker PC (SSH or a keyboard):

```bash
git clone https://github.com/ThebumGreg/letterboxd_scraper.git && cd letterboxd_scraper
sudo ./agent/setup-worker.sh --url http://UNRAID_IP:5001 --token sw_...
```

The address is SyncHub's as this PC reaches it (not `localhost`). The Add worker dialog's first tab holds this exact command with your token in it. Add `--dry-run` first if you want to see what it will do. The script, in order:

1. installs what is missing: `git`, `curl`, Docker with Compose, `cifs-utils` / `nfs-common`;
2. asks SyncHub for this worker's shares and settings (a wrong token or address stops here, before anything is changed);
3. pulls the code from GitHub;
4. writes one marked block into `/etc/fstab` (a backup is kept, your own lines are never touched) and the share login into `/etc/synchub-smb.cred` (mode 600), then mounts the shares;
5. writes `.env` and `docker-compose.yml` into `/opt/synchub-worker`. **A GPU is found and used:** `/dev/dri` (Intel or AMD) or `nvidia-smi` (NVIDIA; it installs the NVIDIA Container Toolkit) is added to that one compose file. `--no-gpu` leaves it out. The agent still test-encodes a frame with every encoder before offering it, and HDR / Dolby Vision always stays on the CPU;
6. builds the image (it includes ffmpeg with VMAF quality scores) and starts the worker;
7. marks the shares (`.synchub-root`, only when every share is really mounted) and runs `--check`;
8. installs the **mount watchdog** and the **update timer** (section 5 and 6). `--no-watchdog` / `--no-auto-update` skip them.

You never edit `.env`: it holds only the address, the token and the work folder. Everything else is chosen on the Workers page.

Other options: `--mode native` (python3, ffmpeg and a systemd service, no Docker), `--dir`, `--work-dir` (a big local disk for job folders and kept originals), `--branch`, `--repo`, `--smb-user` / `--smb-password`, `--no-mounts` (shares already mounted), `--no-init-roots`, `--no-start`. A private repository needs `export GITHUB_TOKEN=...` (read access to Contents) and `sudo -E`.

**A worker set up by hand** (your own fstab lines, a customised compose file, such as worker-1): run the same command with **`--adopt`**. It copies `.env`, the compose file and fstab to `adopt-backup-<time>/` in the worker folder, shows how the compose file will change, and leaves your fstab lines and mounts alone.

## 3. Day to day, on the Workers page

| Do this | Where | What happens |
|---|---|---|
| Update to the newest code | **Update** | Starts as soon as the worker has no running job (within a minute). Rebuilds; if anything fails the old version is put back. "Already up to date" if nothing is new. |
| Restart | More → **Restart worker** | Restarts the container within a minute; running jobs resume. |
| Look at the log | **Logs** | Fetches the worker's own log. |
| Compare CPU and GPU | **Run benchmark** | Times each working encoder on a 30 second clip and scores the quality; the result stays under *Last benchmark*. |
| Follow an update or benchmark | **Media > Jobs** | Each Update, Run benchmark and automatic update appears as a row (Queued, Running, Completed or Failed) with its log; a request that has not started can be cancelled there. |
| Change settings | More → **Edit name, tags, settings** | Applied by the worker within a minute; no PC login. |

The settings: **video encoder** (automatic, CPU only, or a given GPU type); **GPU encodes at once** (keep 1 for integrated graphics: a job over the limit encodes on the CPU instead of waiting); **copy speed limit**; **files analysed at once in a scan**; **log detail**; **slots** (jobs at once; this is what the worker really runs); **update window** and **update automatically**. A setting left at its default is not sent, so the PC's own value (an old `.env` or ini) still applies.

The Update and Restart buttons need the update timer on the PC (installed by the script). If a button says the timer has not checked in, run `sudo ./agent/auto-update.sh --install --dir /opt/synchub-worker` on it once.

## 4. Updating

- **From the Workers page:** the **Update** button above. Nothing else to do.
- **Automatically:** the timer checks for new code once an hour and updates when the worker is idle, or inside the **update window** if you set one (inside the window a running job is cut off; SyncHub gives it to a worker again). Switch it off per worker with *Update automatically*. A failed update puts the old version back, is not tried again until newer code arrives (or you press Update), and sends a notification with the error. A successful one sends *A worker updated itself* with the version. Both can be switched off per person under *What to tell me about*.
- **Update SyncHub first** (Unraid: Docker tab → Check for Updates), then the workers: a worker may use API calls an older SyncHub does not have.
- **By hand (only if you must):** on the PC, `cd ~/letterboxd_scraper && git pull --ff-only origin main && sudo ./agent/update-worker.sh --dir /opt/synchub-worker` (add `--dry-run` first, `--report` to see the worker's state). `docker compose restart` does not update anything: the agent code is baked into the image.

How the update timer works (`agent/auto-update.sh`): every minute it asks SyncHub for this worker's settings and for a request from the Update / Restart buttons; once an hour (or on request) it runs `git fetch`; with new code and an idle worker it runs `git merge --ff-only` and `update-worker.sh --no-pull`. A failed *build* leaves the running container alone; any failure resets the checkout to the last good commit and rebuilds it. If SyncHub cannot be reached the report is kept and sent later. The Workers page shows the last result, and a problem (for example a failed `git fetch`) with an "Update log" of the last lines. On the PC: `sudo ./agent/auto-update.sh --status` or `journalctl -t synchub-auto-update`. Update right now even if busy: `sudo synchub-auto-update --now`. Remove: `--uninstall`. Settings files (rarely needed): `/etc/synchub-auto-update.conf`.

## 5. Shares and network outages

A worker that boots before the network is up, or whose Unraid server restarts, ends up with empty or stale share folders and reports every share as "no marker file". Three layers keep that from needing you:

1. **Mount on use, with retries.** Add `x-systemd.automount,x-systemd.requires=network-online.target,x-systemd.mount-timeout=30,x-systemd.idle-timeout=0` to the options of each share's `/etc/fstab` line (the lines SyncHub generates do not have them yet), then `sudo systemctl daemon-reload`. A failed mount at boot is tried again the next time the folder is used. Make sure the host waits for the network at boot: `sudo systemctl enable systemd-networkd-wait-online.service` (or `NetworkManager-wait-online.service`, whichever your PC uses).
2. **The mount watchdog** (`agent/mount-watchdog.sh`, a host script, not part of the container; `setup-worker.sh` installs it. By hand, from `~/letterboxd_scraper`: `sudo ./agent/mount-watchdog.sh --install`, add `--dry-run` first to see what it writes). A systemd timer then runs one check a minute: a share whose `.synchub-root` marker cannot be read is lazily unmounted (if it hangs) and mounted again from `/etc/fstab`; once every share is back, a worker container that still cannot see the markers is restarted (at most once in ten minutes), and a container that failed to start because a share was missing is started. It never creates a marker, and a share that is mounted but has no marker (not set up yet, or the wrong share) is only logged, never remounted. Settings, if you do not use `/mnt/tower/Media` and `/mnt/tower/TFiles` or the container is not called `synchub-worker`, go in `/etc/synchub-mount-watchdog.conf` (`SYNCHUB_MOUNT_PATHS`, `SYNCHUB_CONTAINER`). Look: `sudo ./agent/mount-watchdog.sh --status` or `journalctl -t synchub-watchdog`. Remove: `--uninstall`.
3. **The agent itself** already copes: it checks every share every 15 seconds, answers "not responding" instead of freezing on a hung mount, keeps trying to reach SyncHub with a growing pause, and a job that finds a share not ready fails as *transient* and is tried again. Nothing in the agent mounts anything; that is the host's job.

If your compose file bind-mounts host folders (`/mnt/tower/Media:/mnt/tower/Media`), add `:rslave` (`/mnt/tower/Media:/mnt/tower/Media:rslave`) so a share mounted or remounted on the host after the container started shows up inside it without a restart. The watchdog's restart is the fallback.

Do **not** make Docker depend on the mounts (`RequiresMountsFor=` in a `docker.service` drop-in). It is a hard dependency: with the server down at boot, Docker, and every container on the PC, would not start.


## 6. When something is wrong

| You see | Likely cause and fix |
|---|---|
| `docker compose up` fails mounting a volume | Wrong address, share name, user or password in `.env`; `cifs-utils` / `nfs-common` missing on the PC; for SMB try `vers=3.1.1` or `vers=2.1` in the share's options on SyncHub's Storage page. A password with `$` needs `$$`, with a comma needs two commas. |
| Share "no marker file" | The share is mounted but not set up: `docker compose run --rm worker --init-roots`. Or it is **not mounted** and you are looking at an empty folder: fix the mount first. |
| Share "wrong share" | The marker names a different share: the mounts are mixed up. |
| Shares "no marker file" after a reboot or a network outage | `journalctl -b -u mnt-tower-Media.mount` shows `Network is unreachable`: the mount ran before the network. See section 5; right now: `sudo umount -f -l /mnt/tower/Media /mnt/tower/TFiles; sudo mount -a`, then `docker compose restart` in the Docker folder. Do not run `--init-roots` on a folder that is not mounted. |
| Share "not responding" | A stale or hung mount. For NFS on Unraid, "stale file handle" reports are linked to the mover; use NFSv4 or SMB. |
| Worker online but jobs wait | The job's shares are not "ready" on this worker (the Jobs list says which). |
| 401 in the log | The token was replaced or revoked: use the current one and restart. |
| `setup-worker.sh` says "SyncHub does not accept this token" or "Cannot reach SyncHub" | The token was replaced (Workers page, More, *New token*), or the address is wrong or blocked: `curl http://UNRAID_IP:5001/healthz` from the worker PC should answer. Nothing on the PC was changed. |
| `setup-worker.sh` ends with "needs your attention" (exit 3) | A line marked `WARNING` says what: a share that would not mount (the reason from `mount` is printed), an `/etc/fstab` line of yours for the same folder, a checkout with local changes. Fix it and run the script again. |
| **Logs** says the worker has not answered | The worker is offline, or its agent is older than 0.4.0 (it cannot send its log): run the setup script again on it. |
| The Workers tab says "has not reported its hardware" | The same: an agent older than 0.4.0. |
| Worker will not mount a share | `cifs-utils` missing on the PC; wrong IP, share name, user or password in `.env`; for SMB try `vers=3.1.1` or `vers=2.1` in the share's options (Storage page), then regenerate the files. |
| Worker online but a share says "no marker file" | Mounted but not set up: `docker compose run --rm worker --init-roots`. Or **not mounted** and you are looking at an empty folder: fix the mount first. |
| Worker stays offline after an upgrade; the worker log says "SyncHub does not accept this worker's token" and `docker compose run --rm worker --check` shows `[!!] SyncHub accepts the token` | SyncHub only keeps a hash of each token, so a token it no longer knows (replaced, revoked, or the worker was re-added) is refused for good. Workers page, Worker-1, **More → New token** (copy it, it is shown once); put it in `SYNCHUB_TOKEN=` in the worker's `.env`, then `docker compose up -d` and `docker compose run --rm worker --check`. See also docs/UPGRADE.md. |
| Worker not online | Wrong address or firewall (`curl http://UNRAID_IP:5001/healthz` from the worker PC should answer `{"ok":true,...}`), or a replaced token (401 in `docker compose logs`). |


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

### Going back to older code by hand

To go back to the previous code:

```
cd ~/letterboxd_scraper && git log --oneline -5      # find the previous commit
git checkout <previous-commit>
sudo ./agent/update-worker.sh --dir /opt/synchub-worker --no-pull
git checkout main                                   # when you want to follow main again
```

More rows for the SyncHub side are in [DEPLOY.md, "When something is wrong"](DEPLOY.md#7-when-something-is-wrong).

## 7. Installing without the script

Only if you cannot use `setup-worker.sh`. The Workers page's **More → Setup files** dialog generates the same files the script writes.

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


### Without Docker

`sudo ./agent/setup-worker.sh --mode native ...` does all of this (system user, `/opt/synchub-agent`, `/etc/synchub-agent.ini`, the systemd service, the fstab lines). By hand: copy `synchub-agent.ini` (from SyncHub's dialog) to `/etc/synchub-agent.ini`, mount the shares with the `fstab` lines from the same dialog, and use `synchub-agent.service` (see the comments at its top). `python3 synchub_agent.py --check` and `--init-roots` work the same way.


## 8. Looking at the worker on the PC

- `cd /opt/synchub-worker && sudo docker compose ps` / `logs -f` / `run --rm worker --check` (the Docker folder is not a git repository).
- `~/letterboxd_scraper` is the git checkout the compose file builds from; run `git` and the scripts there. Keep it clean: edit code in the repository, not on the worker (hand edits make the update refuse, which is the safety check working).
- `sudo ./agent/mount-watchdog.sh --status`, `sudo ./agent/auto-update.sh --status`.
