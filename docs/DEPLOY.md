# Deploying SyncHub with Docker: the app on Unraid, workers on Ubuntu

```
 Your PC (dev)          Unraid                                   Ubuntu worker PC(s)
 data/synchub.db  ──►   [ synchub container ]  ◄── HTTP :5001 ──  [ worker container ]
 (moved once)           /data (database, logs,                     /work (big local disk)
                        posters, backups)                          mounts the Unraid shares over SMB
                        Jellyfin, Radarr, Sonarr (already there)   and does the copying, scanning, encoding
                                  ▲
                        shares Media and TFiles ──────────────────► mounted on every worker
```

- The **app** needs only its `/data` folder. It does not mount your media shares.
- A **worker** mounts both shares (`Media`, `TFiles`), asks the app for jobs over HTTP and does the heavy work. The app never connects to a worker, so nothing is opened or forwarded on the worker PCs.

## What has been checked, and what only your hardware can show

Checked on a real Docker engine with the real Dockerfiles: both images build; the app starts as an unprivileged user from an empty, root-owned data folder and reports healthy; an older database (schema 6, recent changes still in its `-wal` file) was snapshotted, checked, moved in and upgraded to schema 8 with its data intact and a safety copy kept; the worker image talks to the app container, refuses unmarked shares, writes markers, goes online with both shares ready and completes a copy job whose hash matches; both containers survive restarts of the other and stop cleanly; every page and button works in a real browser with CSRF protection on.

**Not checked** (it needs your machines): mounting your Unraid shares through Docker's SMB driver; the Unraid web UI route (the `docker run` command below is the verified one); installing ffmpeg inside the worker image (that step is one `apt-get install ffmpeg` line; the network used for testing blocked it, so the test image had no ffmpeg and library scans in the container were not run, though scans with real ffmpeg are covered by the test suite); talking to your real Jellyfin, Radarr and Sonarr. The guide says where to look if one of these misbehaves.

---

## 0. Before you start

- **Unraid**: its IP address (give it a fixed one or a DHCP reservation), and the shares `Media` and `TFiles` exported over **SMB**. Make a user for the workers (Unraid: *Users → Add user*, for example `synchub`) with **read/write** on both shares.
- **Ubuntu worker PC**: Docker, and a big local disk for the work folder (job copies and the originals kept for 30 days).
- The workers must reach the Unraid address on port **5001** (same network, no firewall in between).
- The code: the repository, on the branch that has these changes (`claude/epic-babbage-qhfxnj` until it is merged, then `main`).

## 1. Build the app image

The app is built from the repository, so the repository has to be somewhere Docker can read. Either way you end up with an image called `synchub:latest` on Unraid.

**A. Build on Unraid** (Unraid terminal, or SSH):

```bash
mkdir -p /mnt/user/appdata/synchub-src && cd /mnt/user/appdata/synchub-src
git clone --branch claude/epic-babbage-qhfxnj https://github.com/ThebumGreg/letterboxd_scraper.git .
# a private repository asks for a GitHub username and a personal access token (read access to the repository) as the password
docker build -t synchub:latest .
```

**B. Build on your PC and send it** (no GitHub access needed on Unraid):

```bash
docker build -t synchub:latest .
docker save synchub:latest | gzip | ssh root@UNRAID_IP 'gunzip | docker load'
```

The source folder is not the data folder: keep them apart (`synchub-src` for the code, `synchub` for the data).

## 2. Move your existing database

Do not copy `synchub.db` by hand. SQLite keeps recent changes in a `-wal` file next to it, and a copy of the main file alone is missing them (in the rehearsal the copied file did not even contain the profile table). Use the snapshot tool, which is correct while the app is running.

On the PC where the data is now (in the repository folder, with your virtual environment active; the old app may keep running):

```bash
python -m synchub.backup create ~/synchub-transfer.db
python -m synchub.backup check  ~/synchub-transfer.db
```

`check` prints what is in the file and what to fix afterwards, for example:

```
  integrity: ok
  database version: 6  (this SyncHub: 8)
  holds: 2 profiles, 2 titles, 0 episodes, 0 workers, 0 library files
  note: it will be upgraded to version 8 the first time SyncHub opens it (a copy is kept first)
  FIX AFTER MOVING: jellyfin_url is http://localhost:8096, which means 'this machine'. ...
```

Then put it where the container will look, **before the first start**:

```bash
ssh root@UNRAID_IP 'mkdir -p /mnt/cache/appdata/synchub'
scp ~/synchub-transfer.db root@UNRAID_IP:/mnt/cache/appdata/synchub/synchub.db
# optional, saves re-downloading posters (Tools > Refresh posters does it otherwise):
scp -r static/posters root@UNRAID_IP:/mnt/cache/appdata/synchub/
```

What travels in the database: settings and API keys, profiles, PINs and permissions, matches and decisions, the Library, workers and storage definitions, kept Jellyfin events. What does not: the log files, and `data/letterboxd.db` (the old app's file; nothing reads it any more).

If you have no data worth keeping, skip this section: the app creates an empty database on first start.

**Which folder?** Use a path on a cache drive or pool directly (`/mnt/cache/appdata/synchub`, or `/mnt/<your pool>/appdata/synchub`) rather than `/mnt/user/appdata/synchub`. SQLite relies on file locking and shared memory that Unraid's `/mnt/user` layer is not designed for, which is why the database tool and the log both look out for it (`docker logs synchub | grep -i wal` shows a warning if SQLite could not switch to WAL mode). If you have no pool, `/mnt/user/appdata` can work, but the direct path is safer.

## 3. Run the app

```bash
docker run -d --name synchub --init --restart unless-stopped \
  -p 5001:5001 \
  -e PUID=99 -e PGID=100 -e TZ=Europe/London \
  -v /mnt/cache/appdata/synchub:/data \
  synchub:latest
```

- `PUID=99 PGID=100` is Unraid's `nobody:users`. On start the container makes `/data` belong to that user and then runs as it, so a folder Docker created as root is fine.
- `TZ` sets the clock the nightly backup (03:30) and the logs use.
- Open `http://UNRAID_IP:5001`. `docker ps` should show `(healthy)` within about a minute; `docker logs synchub` shows the start.

**With the Unraid web UI instead** (*Docker → Add Container*; not verified on Unraid itself): Name `synchub`, Repository `synchub:latest`, Network `bridge`, port **5001** to **5001**, path **/data** to **/mnt/cache/appdata/synchub** (read/write), variables `PUID=99`, `PGID=100`, `TZ=Europe/London`, Extra Parameters `--init`.

**With Docker Compose** (if your Unraid has it): the repository has `docker-compose.yml` and `.env.example`. Copy the example to `.env`, edit it, then `docker compose up -d --build`.

Run **one** copy of the app. The scheduler runs inside it; a second copy against the same `/data` would run every job twice.

### After the first start

1. **If you moved a database:** the log says `The database is older than this version: a copy was saved as /data/backups/premigrate-v6-....db before upgrading it.` That copy is yours to keep until you are happy.
2. **Settings:** a yellow banner appears if any service address still says `localhost` or `127.0.0.1` (inside Docker that means the container itself). Change **Jellyfin**, **Radarr** and **Sonarr** to the address other devices use, for example `http://192.168.1.10:8096`, and use *Test connection*.
3. **Live updates (webhooks):** set *SyncHub's address* to `http://UNRAID_IP:5001`, then press **Set up in Radarr / Sonarr / Jellyfin**. Webhooks left over from the old machine point at it: delete them in Radarr and Sonarr.
4. **Access:** give the profiles that matter a PIN (System → Access). Lost them all? `docker exec synchub python -m synchub.access reset-all`.
5. **Tools:** *Run all* once to refresh everything from the new address.

## 4. Tell SyncHub about the shares (Storage page)

For each share: **Add share**.

| Field | `media` | `tfiles` |
|---|---|---|
| Name | `media` | `tfiles` |
| Protocol | SMB | SMB |
| Server | the Unraid **IP address** (use the IP, not a name: Docker's SMB mounting is not dependable with host names) | the same |
| Share | `Media` | `TFiles` |
| Folder on the workers | `/mnt/tower/Media` | `/mnt/tower/TFiles` |

Then **Library → Settings → Bring everything into** `media` (your clean-up destination). The Storage page generates the mount definitions for the workers from these entries; the share login (the Unraid user's password) is never stored in SyncHub.

## 5. Set up a worker (Ubuntu)

On the worker PC:

```bash
sudo apt update
sudo apt install docker.io docker-compose-v2 cifs-utils      # cifs-utils is for SMB; Docker's SMB mount needs it on the PC itself
sudo usermod -aG docker $USER                                # log out and in again
```

In SyncHub: **Workers → Add worker**. Name it (for example `encoder-1`), and in the *Setup files* dialog set the address to `http://UNRAID_IP:5001`. The token is shown **once**; the dialog gives you `.env`, `docker-compose.yml`, the agent settings and `fstab` lines.

Make a folder on the worker PC:

```
synchub-worker/
    docker-compose.yml     paste from the dialog
    .env                   paste from the dialog; fill in SMB_USER and SMB_PASSWORD (the Unraid user), then:  chmod 600 .env
    agent/                 the "agent" folder of the repository (Dockerfile, synchub_agent.py, media_analysis.py)
```

(From your PC: `scp -r agent user@WORKER_IP:~/synchub-worker/`.) A password with `$` is written `$$` and one with a comma is written as two commas.

Optionally point the work folder at a big disk in `.env`: `WORK_DIR=/data/synchub-work`.

Start it, mark the shares once, and check:

```bash
cd ~/synchub-worker
docker compose up -d --build
docker compose run --rm worker --init-roots    # writes a small .synchub-root file into each share; never creates folders. Once per share, from any worker.
docker compose run --rm worker --check         # says what works and what does not
```

`--check` ends with `[ok]` lines for the SyncHub token and for each share (it will say `[!!] ffmpeg` only if the image was built without it). The **Workers** page now shows the worker **online** with each share **ready**. Press **Test copy** (Workers page) with a small file to prove the path end to end: the copy is read back and compared before it counts.

More workers: repeat this section with another name; each gets its own token. Markers are only written once.

## 6. Day to day

**Update**

```bash
cd /mnt/user/appdata/synchub-src && git pull && docker build -t synchub:latest .
docker rm -f synchub        # the data is in /data, not in the container
# run the same `docker run ...` command from section 3 again
```

A database from an older version is upgraded on start, and a copy of it as it was is saved first (`/data/backups/premigrate-v<N>-....db`). Update the workers the same way: `git pull` or copy the new `agent/` folder, then `docker compose up -d --build` (running jobs resume).

**Backups.** Every night at 03:30 SyncHub saves a consistent copy into `/data/backups/` and keeps the newest 7 (Settings → *Database backups*; Tools has *Back up now*). These files contain your API keys: keep them as private as the database. For a copy outside Unraid: `docker exec synchub python -m synchub.backup create /data/backups/manual.db`, then copy that file off.

**Restore**

```bash
docker stop synchub
docker run --rm -v /mnt/cache/appdata/synchub:/data synchub:latest \
    python -m synchub.backup restore /data/backups/synchub-20261022-033000.db
docker start synchub
```

The database that was there is kept beside it as `synchub.db.before-restore-<time>`. A copy from a *newer* SyncHub than the one running is refused.

**Look at things.** `docker logs synchub` (start-up), the **Logs** page and `/data/logs/app.log` (everything, rotated), **Tools & Operations** and `/diagnostics` when a task looks stuck. On a worker: `docker compose logs -f`, and the **Fetch full log** button on a job in SyncHub.

**Move to another server later:** the same three commands: `backup create`, `backup check`, copy, start.

## 7. When something is wrong

| You see | Likely cause and fix |
|---|---|
| Container `unhealthy`, or restarting | `docker logs synchub`. `cannot write to /data`: start it as root with `PUID`/`PGID` (the command above), or make the folder writable for the user you chose. `database is locked` or a WAL warning: the data folder is on a network or FUSE path; use `/mnt/cache/...`. |
| Banner "These addresses will not work from inside Docker" | A service address says `localhost`/`127.0.0.1`: use the real IP (Settings). |
| Radarr/Sonarr/Jellyfin webhook says it cannot reach SyncHub | The *SyncHub address* is wrong for where that service runs: use `http://UNRAID_IP:5001`, not `localhost`. |
| A page answers "This is not open to ..." | Permissions (System → Access). Lost PINs: `docker exec synchub python -m synchub.access reset-all`. |
| Worker will not mount a share | `cifs-utils` missing on the PC; wrong IP, share name, user or password in `.env`; for SMB try `vers=3.1.1` or `vers=2.1` in the share's options (Storage page), then regenerate the files. |
| Worker online but a share says "no marker file" | Mounted but not set up: `docker compose run --rm worker --init-roots`. Or **not mounted** and you are looking at an empty folder: fix the mount first. |
| Share says "wrong share" | The mounts are mixed up (`Media` mounted where `TFiles` belongs). |
| Worker not online | Wrong address or firewall (`curl http://UNRAID_IP:5001/healthz` from the worker PC should answer `{"ok":true,...}`), or a replaced token (401 in `docker compose logs`). |
| `/healthz` works but pages show an error after a move | `docker logs synchub`; if it mentions a database version newer than this SyncHub, rebuild the image from the newer code. |
| Dates and nightly times are off | Set `TZ`. |

## 8. Security

SyncHub is for your own network. Do not forward port 5001 to the internet. Give the profiles that can change things a **PIN**; profiles without one can be chosen by anyone on the network. Pages and actions are protected against forged requests (CSRF), PINs are stored hashed, and worker tokens are stored hashed. For access from outside, put a reverse proxy with HTTPS in front, or use a VPN.
