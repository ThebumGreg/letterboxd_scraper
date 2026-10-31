# SyncHub

**Version 1.4.0** ([what changed](CHANGELOG.md)). Keeps Letterboxd/Serializd watchlists, Jellyfin, Radarr and Sonarr in one place, shows what you have, what is on its way and what is missing, and can add wanted titles to Radarr/Sonarr. It is also a whole-house TV remote (Roku, Google TV / Android TV) that plays Jellyfin titles, and has profiles with PINs, page permissions and parental controls.

**Where to look**

| I want to | Read |
|---|---|
| Install SyncHub on Unraid or Docker, move a database, back up and restore | [docs/DEPLOY.md](docs/DEPLOY.md) |
| Set up, update or fix a worker PC (one command, then everything on the Workers page) | [docs/WORKERS.md](docs/WORKERS.md) |
| Update SyncHub and see per-release notes | [docs/UPGRADE.md](docs/UPGRADE.md) and [CHANGELOG.md](CHANGELOG.md) |
| Learn what each part of the app does | [docs/FEATURES.md](docs/FEATURES.md) |
| Use or extend the TV remote, or add camera feeds | [docs/TV_REMOTE.md](docs/TV_REMOTE.md), [docs/CAMERAS.md](docs/CAMERAS.md) |
| Publish a release | [docs/RELEASING.md](docs/RELEASING.md) |
| Understand the design | [docs/DESIGN_V2.md](docs/DESIGN_V2.md) (data model), [docs/DESIGN_ENCODING.md](docs/DESIGN_ENCODING.md) (workers, Library, conversions, worker API), [docs/DESIGN_NOTIFICATIONS.md](docs/DESIGN_NOTIFICATIONS.md) |
| What the worker program does | [agent/README.md](agent/README.md) |
| Old notes | [docs/archive/](docs/archive/) |

## Installing

**On Unraid (recommended):** the image is `ghcr.io/thebumgreg/synchub`. Add the template from `unraid/synchub.xml` once, install it from the Docker tab, and update it later with *Check for Updates* and *apply update*, no command line. [docs/DEPLOY.md](docs/DEPLOY.md) has the steps; workers are in [docs/WORKERS.md](docs/WORKERS.md). Anywhere with Docker: `docker compose up -d` (see `docker-compose.yml` and `.env.example`).

**From a checkout, for trying it out or developing:** the next section. The database is `data/synchub.db`, created on first start; port 5001 (`SYNCHUB_PORT` changes it).

## Running from source

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt     # includes Flask-WTF: forms and buttons are protected against forged requests (CSRF)
python -m synchub.web
```

Set `SYNCHUB_SCHEDULER=0` to start without the background jobs (no syncs, scrapes, DDNS updates or nightly backups run by themselves; the Run buttons on Tools & Operations still work). Use it to try a copy of your live database without a second set of jobs acting on your real services.

Then open http://localhost:5001 and:

1. **Settings, Connections**: enter your TMDB API key (needed for titles, posters and episode lists) and the Jellyfin/Radarr/Sonarr URLs and API keys (use *Test connection*). Then **System, People**: create your profiles. Each Letterboxd, Serializd or Jellyfin identity can belong to only one profile.
2. **Tools & Operations**: press **Run all**. It reads Jellyfin, scrapes the watchlists, resolves IDs, matches everything to a movie or show, fetches every episode from TMDB, then reads Radarr and Sonarr and works out each item's status.
3. **Resolution Center**: approve or dismiss the matches the app was not sure about, or link an item by TMDB ID. **Approve all** (top of the page) approves the strongest suggestion for every title listed and leaves titles whose best suggestions are tied for you to decide.

After that the scheduler repeats the work on the intervals set in Settings, Automation (0 turns a job off). Radarr/Sonarr webhooks (`/api/webhook/radarr`, `/api/webhook/sonarr`) refresh the status within seconds of a download starting or finishing.

## Running it in Docker (Unraid for the app, Ubuntu for the workers)

The image is published to GitHub by an Actions workflow on every change to `main` (`ghcr.io/thebumgreg/synchub:latest`, or a version such as `:1.2.0`). One `/data` volume holds the database, logs, poster cache, TV settings and backups. Workers (heavy file work on other PCs) are built from `agent/` with its own `Dockerfile`. **[docs/DEPLOY.md](docs/DEPLOY.md) is the step-by-step guide**: getting the image, installing and updating on Unraid from its web UI, moving your existing database, the shares and workers, restoring, and what to do when something is wrong. The TV remote needs the container on your LAN for *Scan network* and Wake-on-LAN (Unraid: Custom br0); see "The TV remote in Docker" there.

Two commands worth knowing, which also work from a checkout (in Docker: `docker exec synchub python -m synchub.backup ...`): `python -m synchub.backup create FILE` makes a **consistent copy of the database while the app runs** (copying `synchub.db` by hand can miss recent changes that still sit in the `-wal` file next to it) and `python -m synchub.backup check FILE` says what is in a copy, whether it is sound, and which service addresses will stop working after a move (a `localhost` address means the container itself inside Docker). SyncHub also saves a copy every night at 03:30 (the newest 7 are kept; Settings, Automation, *Database backups*) and before any upgrade changes the database. The TV pages keep their settings (TVs added, app choices, Google TV pairing) in a `tv` folder beside the database rather than in it, so each copy comes with a second small file, `<name>.tv.zip`, and `backup restore` puts both back (the TV folder it replaces is kept beside it). `backup check` says whether the TV settings are there. `GET /healthz` answers while the app and its database are up.

## Troubleshooting

**Dynamic DNS takes 30 to 45 seconds to show the new address:** most of that is usually not SyncHub. **Dynamic DNS, Run the check** times the path to the services, then asks Dynu's own name servers and the big public resolvers what they say about the name, with the time each still has the old answer in its cache, and says in words where the wait sits. Every update reports how long each step took (public address, Dynu), and after a real address change SyncHub watches how long each server took to show it (shown on the same page). Background and findings: [docs/DDNS_DELAY.md](docs/archive/DDNS_DELAY.md).

**Posters missing:** **Tools, Refresh posters** forgets earlier failures, asks TMDB for the poster of every title that has none, and saves every poster that is not cached yet. The log names the first titles that have no poster anywhere.

**Radarr/Sonarr "root folder does not exist" or a wrong quality profile:** **Settings, Test connection** (Radarr/Sonarr) now checks the chosen profile and root folder against the service itself and lists the real ones; the dropdowns load when the page opens and flag a saved value the service does not have. Nothing is added until both are valid, and automatic adding stops with one message (`BLOCKED, N waiting: ...` in the task status) instead of one failure per title.


A task looks stuck, or fails with "database is locked":

1. **Tools & Operations** shows what each running task is doing right now (for example `Checked 40 of 150`, `Waiting 14s before the next user`, `TMDB: Show Name (3 of 40)`). The Letterboxd resolver and scrapers pause a few seconds between pages on purpose, so a long job is normal; a message that never changes for minutes is not.
2. **What it's doing now** (System, Monitor; it needs the Logs permission, and Tools & Operations links to it) shows every running task with its age, every open database connection (and whether it has unsaved writes), and where each background thread is right now. A thread whose last line is `socket.py ... readinto` is waiting on a website; one waiting in `sqlite3` is waiting on the database.
3. **Logs** has one line per task start and finish (with the duration) and the full traceback for any failure. For every outgoing HTTP call set `SYNCHUB_LOG_LEVEL=DEBUG` before starting the app; calls slower than 5 seconds are always logged as warnings.
4. A "database is locked" message now ends with `(held by: ...)`, naming the code and thread that is holding the database. If it says no connection in this app holds it, check that a second copy of the app is not running against the same `data/synchub.db`.
5. Keep the project on the Linux filesystem (for example `~/projects`), not on `/mnt/c`: SQLite cannot use WAL mode there and the app logs a warning at startup if it cannot.
6. A task stuck as `IN_PROGRESS` cannot be re-run until the app restarts (restarting also marks it failed).

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Tests use a temporary database and never touch the network (a guard fails any test that tries).

## Layout

The code is in `synchub/`: `db.py` + `migrations/` (schema), `clients/` (Jellyfin, Radarr, Sonarr, TMDB), `ingest/` (one module per source, writing the `src_*` tables), `matching.py` and `episodes.py` (linking), `library/` (the Library's decision logic, scans and duplicate search), `storage.py` and `jobs.py` (shares and worker jobs), `access.py` (PINs and permissions) and `ratings.py` (age ratings and the rating limit), `backup.py` (consistent copies, checks and restore of the database), `jellyfin_events.py` (kept Jellyfin events), `watch.py` and `ingest/jellyfin_userdata.py` (each user's watch progress), `notifications/` (delivery methods such as Pushover, the outbox and its sender, see docs/DESIGN_NOTIFICATIONS.md), `ddns.py` and `ddns_check.py` (dynamic DNS and its delay check), `states.py` (statuses), `catalog.py` (what the pages read), `automation.py` (requests and auto-add), `tasks.py` and `scheduler.py` (jobs), `tv/` (the TV remote: device drivers for Roku and Google / Android TV, discovery, wake and play logic), `web/` (pages, templates and API; `web/tv.py` is the TV menu). `config.py`, `logger.py` and `settings_validation.py` at the top level are shared helpers; `static/` holds the CSS, JavaScript and cached posters.

Also in the repository: `Dockerfile`, `docker-compose.yml` and `docker/entrypoint.sh` (the app image), `agent/` (the worker), `unraid/synchub.xml` (the Unraid template), `.github/workflows/` (tests and publishing the image), and `docs/`.
