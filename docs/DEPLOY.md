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

Checked on a real Docker engine with the real Dockerfiles (earlier releases, and again for V1.1 apart from the image build, see below): the app starts as an unprivileged user from an empty, root-owned data folder and reports healthy; an older database was snapshotted, checked, moved in and upgraded with its data intact and a safety copy kept; the worker image talks to the app container, refuses unmarked shares, writes markers, goes online with both shares ready and completes a copy job whose hash matches; both containers survive restarts of the other and stop cleanly; every page and button works in a real browser with CSRF protection on.

`agent/setup-worker.sh` was run for real against a running SyncHub, a real git repository and real files, with stand-ins for `apt-get`, `docker`, `mount` and `systemctl` (the tests in `tests/v2/test_setup_script.py`), and once in `--mode native` as root on a Linux machine (system user, files, service file, the agent's `--check`). **Not checked**: installing Docker with `apt` on a fresh Ubuntu, mounting your real Unraid shares through `/etc/fstab`, and the container build and start on that PC.

**Not checked** (it needs your machines or the internet): the publishing workflow and the image build on GitHub (the build needs the internet; GitHub's first run is the test, and the same build runs on every pull request); pulling the image and the Unraid web UI route on Unraid itself; mounting your Unraid shares through Docker's SMB driver; `adb` inside the image talking to a real Google TV; ffmpeg inside the worker image; talking to your real Jellyfin, Radarr and Sonarr. The guide says where to look if one of these misbehaves.

---

## 0. Before you start

- **Unraid**: its IP address (give it a fixed one or a DHCP reservation), and the shares `Media` and `TFiles` exported over **SMB**. Make a user for the workers (Unraid: *Users → Add user*, for example `synchub`) with **read/write** on both shares.
- **Ubuntu worker PC**: Docker, and a big local disk for the work folder (job copies and the originals kept for 30 days).
- The workers must reach the Unraid address on port **5001** (same network, no firewall in between).
- A GitHub account that can read this repository's packages (the image is published to GitHub's container registry, section 1).

## 1. Get the app image

GitHub builds the image for you. Every push to `main` runs the tests and then publishes it to GitHub's container registry as

```
ghcr.io/thebumgreg/synchub:latest      the newest main (what Unraid's "apply update" follows)
ghcr.io/thebumgreg/synchub:1.1.1       the newest build of release 1.1.1 (the number in synchub/__init__.py; see docs/RELEASING.md)
ghcr.io/thebumgreg/synchub:sha-abc1234 one exact commit
```

The first publish happens when the V1.1 code reaches `main`. Until then the image does not exist.

**Who can pull it.** A package made from a private repository is private. Choose one:

- **Make the package public** (simplest, and the only choice that survives an Unraid reboot without extra steps): GitHub, your profile, *Packages*, `synchub`, *Package settings*, *Change visibility*, Public. The repository stays private, but the image holds the application code, so anyone who knows the name could pull it. The image contains no settings, keys or data (those live in `/data`).
- **Keep it private and log in once on Unraid**: make a token (GitHub, Settings, Developer settings, *Personal access tokens (classic)*, scope **read:packages**), then in Unraid's terminal `docker login ghcr.io -u ThebumGreg` and paste the token as the password. Unraid keeps Docker's login in memory, so it is lost at a reboot: add the same `docker login` line to a *User Scripts* job set to run at array start (or the update check and "apply update" will fail with "denied" after every reboot).

**Without GitHub's registry** (offline, or to try a branch): build it yourself. On a PC with `git` and Docker: `docker build -t synchub:latest .` and `docker save synchub:latest | gzip | ssh root@UNRAID_IP 'gunzip | docker load'`, then use `synchub:latest` as the repository name below. Unraid's update check cannot see a local image, which is normal.

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
  database version: 6  (this SyncHub: 10)
  holds: 2 profiles, 2 titles, 0 episodes, 0 workers, 0 library files
  note: it will be upgraded to version 10 the first time SyncHub opens it (a copy is kept first)
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

### On Unraid, from its web UI (and update it from there)

1. **Install the template** (once). It fills in the Docker form for you. In Unraid's terminal:

   ```bash
   mkdir -p /boot/config/plugins/dockerMan/templates-user
   curl -fL -H "Authorization: Bearer YOUR_GITHUB_TOKEN" \
     -o /boot/config/plugins/dockerMan/templates-user/synchub.xml \
     https://raw.githubusercontent.com/ThebumGreg/letterboxd_scraper/main/unraid/synchub.xml
   ```

   (A private repository needs the token header, a token with read access to **Contents**; if the repository is public leave the `-H` line out. Or copy the file from your checkout: `scp unraid/synchub.xml root@UNRAID_IP:/boot/config/plugins/dockerMan/templates-user/`.)
2. **Docker tab, Add Container**, choose **SyncHub** in the *Template* list. Check:
   - *Data*: `/mnt/cache/appdata/synchub` (a cache or pool path, not `/mnt/user`).
   - *Time zone*, for example `America/Los_Angeles`.
   - *Network type*: **Custom: br0** with a fixed IP if you want the TV remote's automatic discovery and Wake-on-LAN (they need to be on your LAN, see "The TV remote in Docker" below), otherwise leave **Bridge**.
   - Press **Apply**. Unraid pulls `ghcr.io/thebumgreg/synchub:latest` and starts it. Open the WebUI from the container's menu; `docker ps` shows `(healthy)` within about a minute.
3. **Updates, from the GUI.** Docker tab, **Check for Updates**. A SyncHub row that says *update ready* has a newer `latest` on GitHub: click it and **apply update**. Unraid pulls the new image and replaces the container (the data is in `/data`, so nothing is lost; a database from an older version is upgraded on start and a copy is saved first). For hands-off updates install the *Auto Update Applications* plugin (from Community Applications) and switch SyncHub on there.
4. **Pin a version, or go back.** Edit the container and change *Repository* to `ghcr.io/thebumgreg/synchub:1.1.1`: it then only changes when you change it. Back to `:latest` to follow main again. Going back to an older image after the database was upgraded is not supported: restore a backup from before (section 6).

Run **one** copy of the app. The scheduler runs inside it; a second copy against the same `/data` would run every job twice.

### With a command

```bash
docker run -d --name synchub --init --restart unless-stopped \
  -p 5001:5001 \
  -e PUID=99 -e PGID=100 -e TZ=Europe/London \
  -v /mnt/cache/appdata/synchub:/data \
  ghcr.io/thebumgreg/synchub:latest
```

- `PUID=99 PGID=100` is Unraid's `nobody:users`. On start the container makes `/data` belong to that user and then runs as it, so a folder Docker created as root is fine.
- `TZ` sets the clock the nightly backup (03:30) and the logs use.
- Open `http://UNRAID_IP:5001`. `docker ps` should show `(healthy)` within about a minute; `docker logs synchub` shows the start.

**With Docker Compose** (if your Unraid or PC has it): the repository has `docker-compose.yml` and `.env.example`. Copy the example to `.env`, edit it, then `docker compose up -d` (it pulls the image; `docker compose up -d --build` builds from the checkout instead).

### The TV remote in Docker

Controlling a TV, pairing a Google TV and playing a title only need the container to reach the TV's address, which works on any network type. Two things need the container to be **on your LAN**: *Scan network* (finds TVs by broadcasting) and *Wake-on-LAN* (a broadcast packet). On Unraid's default **Bridge** network they cannot work; give the container its own address (**Custom: br0**, with a fixed IP) or use **Host** (then port 5001 must be free). TVs can always be added by typing their address. The image includes `adb` for Google TV / Android TV; the key the TVs were told to trust is kept in `/data/tv`, so a replaced container is still trusted. Everything the TV pages remember (TVs, app choices, pairing) is in `/data/tv`, and the nightly backup saves it beside the database.

### After the first start

1. **If you moved a database:** the log says `The database is older than this version: a copy was saved as /data/backups/premigrate-v6-....db before upgrading it.` That copy is yours to keep until you are happy.
2. **Settings:** a yellow banner appears if any service address still says `localhost` or `127.0.0.1` (inside Docker that means the container itself). Change **Jellyfin**, **Radarr** and **Sonarr** to the address other devices use, for example `http://192.168.1.10:8096`, and use *Test connection*.
3. **Live updates (webhooks)** (Settings → Automation): set *SyncHub's address* to `http://UNRAID_IP:5001`, then press **Set up in Radarr / Sonarr / Jellyfin**. Webhooks left over from the old machine point at it: delete them in Radarr and Sonarr.
4. **People:** give the profiles that matter a PIN (System → People). Lost them all? `docker exec synchub python -m synchub.access reset-all`.
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

### With the setup script (recommended)

In SyncHub: **Workers → Add worker**, name it (for example `encoder-1`) and copy the token (shown **once**). Then on the worker PC (Ubuntu or Debian):

```bash
git clone https://github.com/ThebumGreg/letterboxd_scraper.git && cd letterboxd_scraper
sudo ./agent/setup-worker.sh --url http://UNRAID_IP:5001 --token sw_...
```

(The dialog's first tab, `setup-worker.sh`, holds this command with your token in it. For a private repository: `export GITHUB_TOKEN=...` with read access to *Contents*, and `sudo -E`.) The script installs Docker and the mount tools, asks SyncHub for this worker's settings and shares, pulls the latest code, adds the shares to `/etc/fstab` (one marked block, with a backup), keeps the Unraid user and password in `/etc/synchub-smb.cred` (mode 600), mounts the shares, writes `.env`, builds and starts the worker, marks the shares and runs `--check`. It asks for the Unraid user and password if you did not pass `--smb-user` and `--smb-password`. The **Workers** page then shows the worker **online** with each share **ready**; press **Test copy** with a small file to prove the path end to end.

To update the worker later, run the same command again (it needs no arguments: the address and token are read from the `.env` it wrote; it pulls the newest code and refreshes the settings from SyncHub). `--dry-run` shows what it would do first; `--help` lists the options, including `--mode native` for a PC without Docker. The full list of what it does and does not touch is in `agent/README.md`.

### By hand

The same thing, step by step. On the worker PC:

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

On Unraid: Docker tab, **Check for Updates**, then **apply update** on SyncHub (section 3). By hand, anywhere:

```bash
docker pull ghcr.io/thebumgreg/synchub:latest
docker rm -f synchub        # the data is in /data, not in the container
# run the same `docker run ...` command from section 3 again
```

A database from an older version is upgraded on start, and a copy of it as it was is saved first (`/data/backups/premigrate-v<N>-....db`). The version is shown at the bottom of every page. Update a worker by running `sudo ./agent/setup-worker.sh` again (section 5), or by hand: copy the new `agent/` folder from the repository (or `git pull`), then `docker compose up -d --build` (running jobs resume). The workers are built from the `agent/` folder; only the app image is published.

**Backups.** Every night at 03:30 SyncHub saves a consistent copy into `/data/backups/` and keeps the newest 7 (Settings → Automation → *Database backups*; Tools has *Back up now*). Each copy comes with a `.tv.zip` beside it holding the TV pages' settings (TVs, app choices, Google TV pairing); `restore` puts both back. These files contain your API keys and the Google TV pairing key: keep them as private as the database. For a copy outside Unraid: `docker exec synchub python -m synchub.backup create /data/backups/manual.db`, then copy that file off.

**Restore**

```bash
docker stop synchub
docker run --rm -v /mnt/cache/appdata/synchub:/data ghcr.io/thebumgreg/synchub:latest \
    python -m synchub.backup restore /data/backups/synchub-20261022-033000.db
docker start synchub
```

The database that was there is kept beside it as `synchub.db.before-restore-<time>`. A copy from a *newer* SyncHub than the one running is refused.

**Look at things.** `docker logs synchub` (start-up), the **Logs** page and `/data/logs/app.log` (everything, rotated), **Tools & Operations** and **System → Monitor → What it's doing now** (`/activity`) when a task looks stuck. For a worker: the **Logs** button on the Workers page fetches the worker's own log into SyncHub (filter, search, download), **Worker Status** shows its CPU, memory, graphics card and which encoders work, and a job's **Fetch full log** button gets that job's log; on the PC itself, `docker compose logs -f`.

**Move to another server later:** the same three commands: `backup create`, `backup check`, copy, start.

## 7. When something is wrong

| You see | Likely cause and fix |
|---|---|
| `pull access denied` / `denied` / `unauthorized` for `ghcr.io/thebumgreg/synchub` | The package is private and Unraid is not logged in (or the login was lost at a reboot): make the package public or log in again (section 1). `manifest unknown` means nothing has been published yet: the V1.1 code has to reach `main` first, or check the *Actions* tab for a failed run. |
| Update check says nothing, or *not available* | The repository field is not the registry name (`ghcr.io/thebumgreg/synchub:latest`), or the image is a local build. |
| *Scan network* finds nothing, Wake-on-LAN does nothing | The container is on Bridge; see "The TV remote in Docker" (section 3). |
| Container `unhealthy`, or restarting | `docker logs synchub`. `cannot write to /data`: start it as root with `PUID`/`PGID` (the command above), or make the folder writable for the user you chose. `database is locked` or a WAL warning: the data folder is on a network or FUSE path; use `/mnt/cache/...`. |
| Banner "These addresses will not work from inside Docker" | A service address says `localhost`/`127.0.0.1`: use the real IP (Settings). |
| Radarr/Sonarr/Jellyfin webhook says it cannot reach SyncHub | The *SyncHub address* is wrong for where that service runs: use `http://UNRAID_IP:5001`, not `localhost`. |
| A page answers "This is not open to ..." | Permissions (System → People). Lost PINs: `docker exec synchub python -m synchub.access reset-all`. |
| `setup-worker.sh` says "SyncHub does not accept this token" or "Cannot reach SyncHub" | The token was replaced (Workers page, More, *New token*), or the address is wrong or blocked: `curl http://UNRAID_IP:5001/healthz` from the worker PC should answer. Nothing on the PC was changed. |
| `setup-worker.sh` ends with "needs your attention" (exit 3) | A line marked `WARNING` says what: a share that would not mount (the reason from `mount` is printed), an `/etc/fstab` line of yours for the same folder, a checkout with local changes. Fix it and run the script again. |
| **Logs** says the worker has not answered | The worker is offline, or its agent is older than 0.4.0 (it cannot send its log): run the setup script again on it. |
| Worker Status says "has not reported its hardware" | The same: an agent older than 0.4.0. |
| Worker will not mount a share | `cifs-utils` missing on the PC; wrong IP, share name, user or password in `.env`; for SMB try `vers=3.1.1` or `vers=2.1` in the share's options (Storage page), then regenerate the files. |
| Worker online but a share says "no marker file" | Mounted but not set up: `docker compose run --rm worker --init-roots`. Or **not mounted** and you are looking at an empty folder: fix the mount first. |
| Share says "wrong share" | The mounts are mixed up (`Media` mounted where `TFiles` belongs). |
| Worker not online | Wrong address or firewall (`curl http://UNRAID_IP:5001/healthz` from the worker PC should answer `{"ok":true,...}`), or a replaced token (401 in `docker compose logs`). |
| `/healthz` works but pages show an error after a move | `docker logs synchub`; if it mentions a database version newer than this SyncHub, rebuild the image from the newer code. |
| Dates and nightly times are off | Set `TZ`. |

## 8. Security

SyncHub is for your own network. Do not forward port 5001 to the internet. Give the profiles that can change things a **PIN**; profiles without one can be chosen by anyone on the network. Pages and actions are protected against forged requests (CSRF), PINs are stored hashed, and worker tokens are stored hashed. For access from outside, put a reverse proxy with HTTPS in front, or use a VPN.

## Access token (optional login)

SyncHub listens on every interface and, by default, has no login. To put one in front of it, set `SYNCHUB_ACCESS_TOKEN` in `.env` (any long random text, for example `openssl rand -hex 24`) and restart the container. Every page then asks for the token once per device and remembers it for a year. Changing the token signs every device out. Scripts can send `Authorization: Bearer <token>`.

These stay open on purpose, because they have their own ways in: worker calls (`/api/worker/...`, each worker's own token), the Jellyfin, Radarr and Sonarr webhooks (`/api/webhook/...`), and `/healthz`. The profile picker and PINs still work on top of the token. Ten wrong tries from one address make it wait five minutes.
