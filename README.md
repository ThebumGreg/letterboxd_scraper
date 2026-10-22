# SyncHub

Keeps Letterboxd/Serializd watchlists, Jellyfin, Radarr and Sonarr in one place, shows what you have, what is on its way and what is missing, and can add wanted titles to Radarr/Sonarr.

Start it with `python -m synchub.web` (port 5001, set `SYNCHUB_PORT` to change). The database is `data/synchub.db`, created on first start. The design is in [docs/DESIGN_V2.md](docs/DESIGN_V2.md).

The original app and its `data/letterboxd.db` are no longer used; nothing is read from that file, so it can be deleted or kept as a backup.

## Running the app

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m synchub.web
```

Then open http://localhost:5001 and:

1. **Settings**: enter your TMDB API key (needed for titles, posters and episode lists), the Jellyfin/Radarr/Sonarr URLs and API keys (use *Test connection*), and create your profiles. Each Letterboxd, Serializd or Jellyfin identity can belong to only one profile.
2. **Tools & Operations**: press **Run all**. It reads Jellyfin, scrapes the watchlists, resolves IDs, matches everything to a movie or show, fetches every episode from TMDB, then reads Radarr and Sonarr and works out each item's status.
3. **Resolution Center**: approve or dismiss the matches the app was not sure about, or link an item by TMDB ID. **Approve all** (top of the page) approves the strongest suggestion for every title listed and leaves titles whose best suggestions are tied for you to decide.

After that the scheduler repeats the work on the intervals set in Settings (0 turns a job off). Radarr/Sonarr webhooks (`/api/webhook/radarr`, `/api/webhook/sonarr`) refresh the status within seconds of a download starting or finishing.

## What the statuses mean

Missing, Queued (monitored in Radarr/Sonarr, nothing yet), Downloading (in the Radarr/Sonarr queue), Downloaded (the file exists but Jellyfin has not picked it up), In Library. **Transferring** and **Encoding** are defined but unused until your own scripts report them. A show is **Partial** while only some of its aired episodes are in the library. Episodes that have not aired show as *Not aired* and are not counted as missing. Specials (Season 0) are listed last and are not counted in a show's totals.

New states need no code or schema change: add a row to `state_def` and have a script write `processing_event` rows.

## Workers (control plane)

**Workers** (top bar) is where the PCs that will do scanning, copying and encoding are added and watched. **Add worker** gives a name, tags, a slot count and a one-time token with the settings snippet for that PC. A worker can be paused, drained (finish what it has, take nothing new), disabled, given a new token or have its token revoked. The page also lists recent jobs with their stage, progress and attempts, and lets you cancel, retry or decide jobs that are waiting for you (approve, retry, skip, reject).

The worker side is a small API (`/api/worker/v1/...`, token per worker, documented in `docs/DESIGN_ENCODING.md`, Appendix A). Workers ask for jobs; a job whose worker goes silent returns to the queue. The agent script for Ubuntu, the Library page and the encoding itself are the next phases of that plan; nothing queues real jobs yet.

## Search and add

**Search** (top bar) looks a title up on TMDB and can send it straight to Radarr or Sonarr. Type a title, optionally with a year (`Heat 1995`); if a title simply ends in a number-like year (`Blade Runner 2049`) the whole text is searched when the year finds nothing. Results show movies, then shows, with where each already stands (In Library, Queued, Downloading ...). Nothing is saved until you add something.

- **Movies**: **Add to Radarr**. A movie already in Radarr is shown as such instead of offering the button.
- **Shows**: **Add to Sonarr...** opens a season picker (every regular season ticked, Specials last and unticked). SyncHub reads every episode from TMDB first, then sends the ticked seasons to Sonarr. A show can be asked for more seasons later with **Request seasons...**. Sonarr needs a TVDB ID; if TMDB has none the picker says so.
- **Who asked**: the profile chosen on the Who-is-watching page is credited ("Wanted by") and counts as demand, even if they have watched the title. A guest can add too, but nobody is credited. Requests stay when a profile is deleted (they just stop counting).
- A failed add (for example a Radarr root folder that does not exist) says why and leaves nothing behind in the catalog.

## Who is watching

The first visit from a browser shows **Who is watching?**: pick your profile, or "Just looking" to continue as a guest. There is no password. The choice is remembered on that device for a year (a plain cookie holding the profile ID), the top bar shows who you are with a **switch** link, and the profile's Jellyfin user is the one the Jellyfin app links open as. It only says who is using the app; anyone on the network can pick anyone, so it is not security. Scripts, webhooks, images and the live stream are never asked. With no profiles nothing is asked.

## Live updates: webhooks and download progress

Without these, Radarr and Sonarr are read every 6 hours. With them, a grab or import shows up within seconds and a download's percentage moves while you watch.

- **Webhook** (Settings, Live updates): enter SyncHub's address as Radarr/Sonarr reach it (not `localhost`), then press **Set up in Radarr** / **Set up in Sonarr**. SyncHub creates a Webhook connection in that service from the service's own template, switched on for grab, import, upgrade, rename, add and delete. Pressing it again changes nothing. The panel shows when the last event arrived, so you can tell it works. A grab only triggers a look at the download queue; imports, deletes and adds read the library. Health and test events are ignored. If the service says it could not send a test message, it cannot reach SyncHub at that address from where it runs (a Docker network name or the machine's LAN address usually fixes that).
- **Download progress**: every 30 seconds (Settings, "Download progress check"; 0 turns it off) SyncHub reads just the download queue, but only while something is downloading. Otherwise it looks at most every 5 minutes, which also catches downloads started directly in Radarr/Sonarr when no webhook is set up. A download that leaves the queue triggers a library read, so "Downloaded" appears without waiting for the schedule. Open dashboards update live without a reload.
- **Jellyfin**: Jellyfin's Webhook plugin can send "Item Added" to `/api/webhook/jellyfin` so a new file shows as In Library right away. Play, pause and login events are ignored.

## Opening items in the Jellyfin app (iPhone and iPad)

"Watch on Jellyfin" opens the Jellyfin web page everywhere. On an iPhone or iPad it opens the item in the Jellyfin (Swiftfin) app instead, using `swiftfin://<server id>/<user id>/item/<item id>`, and falls back to the web page after about two seconds if the app does not take over.

- The **server ID** is read from Jellyfin during a Jellyfin sync, so run **Tools, Sync movies/TV** once after upgrading.
- The **user** must be a Jellyfin user that is signed in to the app on that device. Give each profile its Jellyfin user in Settings; the profile chosen on the Who-is-watching page then decides. A guest, or a profile without a Jellyfin user, is asked once which Jellyfin user to use (remembered on that device; the link at the bottom of every page changes it).
- If tapping does nothing useful, check that the user chosen is the one signed in to the app, and that the app is installed.

## Troubleshooting

**Posters missing:** **Tools, Refresh posters** forgets earlier failures, asks TMDB for the poster of every title that has none, and saves every poster that is not cached yet. The log names the first titles that have no poster anywhere.

**Radarr/Sonarr "root folder does not exist" or a wrong quality profile:** **Settings, Test connection** (Radarr/Sonarr) now checks the chosen profile and root folder against the service itself and lists the real ones; the dropdowns load when the page opens and flag a saved value the service does not have. Nothing is added until both are valid, and automatic adding stops with one message (`BLOCKED, N waiting: ...` in the task status) instead of one failure per title.


A task looks stuck, or fails with "database is locked":

1. **Tools & Operations** shows what each running task is doing right now (for example `Checked 40 of 150`, `Waiting 14s before the next user`, `TMDB: Show Name (3 of 40)`). The Letterboxd resolver and scrapers pause a few seconds between pages on purpose, so a long job is normal; a message that never changes for minutes is not.
2. **Diagnostics** (link on the Tools and Logs pages, or `/diagnostics`) shows every running task with its age, every open database connection (and whether it has unsaved writes), and where each background thread is right now. A thread whose last line is `socket.py ... readinto` is waiting on a website; one waiting in `sqlite3` is waiting on the database.
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

The code is in `synchub/`: `db.py` + `migrations/` (schema), `clients/` (Jellyfin, Radarr, Sonarr, TMDB), `ingest/` (one module per source, writing the `src_*` tables), `matching.py` and `episodes.py` (linking), `states.py` (statuses), `catalog.py` (what the pages read), `automation.py` (requests and auto-add), `tasks.py` and `scheduler.py` (jobs), `web/` (pages, templates and API). `config.py`, `logger.py` and `settings_validation.py` at the top level are shared helpers; `static/` holds the CSS, JavaScript and cached posters.
