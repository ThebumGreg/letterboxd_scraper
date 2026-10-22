# SyncHub

Keeps Letterboxd/Serializd watchlists, Jellyfin, Radarr and Sonarr in one place, shows what you have, what is on its way and what is missing, and can add wanted titles to Radarr/Sonarr.

Start it with `python -m synchub.web` (port 5001, set `SYNCHUB_PORT` to change). The database is `data/synchub.db`, created on first start. The design is in [docs/DESIGN_V2.md](docs/DESIGN_V2.md).

The original app and its `data/letterboxd.db` are no longer used; nothing is read from that file, so it can be deleted or kept as a backup.

## Running the app

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt     # includes Flask-WTF: forms and buttons are protected against forged requests (CSRF)
python -m synchub.web
```

Then open http://localhost:5001 and:

1. **Settings**: enter your TMDB API key (needed for titles, posters and episode lists), the Jellyfin/Radarr/Sonarr URLs and API keys (use *Test connection*), and create your profiles. Each Letterboxd, Serializd or Jellyfin identity can belong to only one profile.
2. **Tools & Operations**: press **Run all**. It reads Jellyfin, scrapes the watchlists, resolves IDs, matches everything to a movie or show, fetches every episode from TMDB, then reads Radarr and Sonarr and works out each item's status.
3. **Resolution Center**: approve or dismiss the matches the app was not sure about, or link an item by TMDB ID. **Approve all** (top of the page) approves the strongest suggestion for every title listed and leaves titles whose best suggestions are tied for you to decide.

## Running it in Docker (Unraid for the app, Ubuntu for the workers)

The repository has a `Dockerfile` and `docker-compose.yml` for the app (one `/data` volume holds the database, logs, poster cache and backups) and `agent/Dockerfile` for the worker. **[docs/DEPLOY.md](docs/DEPLOY.md) is the step-by-step guide**: building the image, moving your existing database, running on Unraid, setting up the shares and the workers, updating, restoring, and what to do when something is wrong.

Two commands worth knowing, which also work from a checkout: `python -m synchub.backup create FILE` makes a **consistent copy of the database while the app runs** (copying `synchub.db` by hand can miss recent changes that still sit in the `-wal` file next to it) and `python -m synchub.backup check FILE` says what is in a copy, whether it is sound, and which service addresses will stop working after a move (a `localhost` address means the container itself inside Docker). SyncHub also saves a copy every night at 03:30 (the newest 7 are kept; Settings, *Database backups*) and before any upgrade changes the database. `GET /healthz` answers while the app and its database are up.

After that the scheduler repeats the work on the intervals set in Settings (0 turns a job off). Radarr/Sonarr webhooks (`/api/webhook/radarr`, `/api/webhook/sonarr`) refresh the status within seconds of a download starting or finishing.

## What the statuses mean

Missing, Queued (monitored in Radarr/Sonarr, nothing yet), Downloading (in the Radarr/Sonarr queue), Downloaded (the file exists but Jellyfin has not picked it up), In Library. **Transferring** and **Encoding** are defined but unused until your own scripts report them. A show is **Partial** while only some of its aired episodes are in the library. Episodes that have not aired show as *Not aired* and are not counted as missing. Specials (Season 0) are listed last and are not counted in a show's totals.

New states need no code or schema change: add a row to `state_def` and have a script write `processing_event` rows.

## Workers (control plane)

**Workers** (top bar) is where the PCs that do the heavy work are added and watched. **Add worker** gives a name, tags, a slot count and a one-time token, together with ready-made setup files for that PC (a Docker compose file, an `.env`, an agent settings file and `/etc/fstab` lines). A worker can be paused, drained (finish what it has, take nothing new), disabled, given a new token or have its token revoked. The page also lists each worker's shares ("ready", "not mounted", "wrong share"...), recent jobs with their stage, progress and attempts, lets you cancel, retry or decide jobs that are waiting for you, fetch a job's full log from its worker, and queue a **Test copy** (a verified copy of one file between two shares) to prove a worker end to end.

**Storage** (top bar) defines the Unraid shares the workers use: a short name, SMB or NFS, the server address, the share, and the folder every worker mounts it at. SyncHub cannot mount anything on a worker PC itself, so it generates the files that do (Docker mounts the shares, and a worker refuses any share whose `.synchub-root` marker file is missing or names a different share). Share logins never enter SyncHub; they go in the worker's own `.env`.

The worker side is a small API (`/api/worker/v1/...`, token per worker, documented in `docs/DESIGN_ENCODING.md`, Appendix A) and an agent for Ubuntu in `agent/` (one Python file; `agent/README.md` has the install steps). Workers ask for jobs; a job whose worker goes silent returns to the queue and resumes where its files are. Verified copies and the Library scans work today; processing approved files and the encoding itself are the next phases of that plan.

## Library

**Library** (top bar) is the original media analysis and migration tool as a page. It looks at every video file on your shares, says in plain words what it would do with each one (copy as it is, a clean name, remove tracks you do not need, convert an old format) and how sure it is, and you approve what is right. Everything is meant to end up in one share (**Media** by default, as `Movies/<name>/<name>` and `TV Shows/<show>/Season NN/<name>`); files that are already in the right place are left alone.

- **Look at a share** starts a scan on a worker, so it needs a worker with the share mounted (see Workers and Storage). Files it has already looked at and that have not changed are skipped, so running it again is quick. A scan can be stopped, and one that was interrupted carries on with what is left. Looking at files never changes anything on a share.
- **The queues** (Ready to approve, Suggested changes, Worth a look, Format conversion, Needs you, Done) show how many files wait with high, medium and low confidence. Open a list to search and filter, or **Review one by one**: it shows the suggestion, the file name (editable), the tracks and why, and has Approve, Reject and Skip (keys `a`, `r`, `s`; `e` edits the name).
- **Approve together** approves everything in a list that qualifies, after showing what it will approve. Low-confidence files are never included, and duplicates are decided one at a time.
- **Find duplicates** compares files of the same size across all shares (reading only what it must) and suggests keeping the copy already in place.
- **Export** gives the same CSV report as the original tool.
- **Approving records a decision and nothing else.** Copying and converting the approved files is the next phase.

## Search and add

**Search** (top bar) looks a title up on TMDB and can send it straight to Radarr or Sonarr. Type a title, optionally with a year (`Heat 1995`); if a title simply ends in a number-like year (`Blade Runner 2049`) the whole text is searched when the year finds nothing. Results show movies, then shows, with where each already stands (In Library, Queued, Downloading ...). Nothing is saved until you add something.

- **Movies**: **Add to Radarr**. A movie already in Radarr is shown as such instead of offering the button.
- **Shows**: **Add to Sonarr...** opens a season picker (every regular season ticked, Specials last and unticked). SyncHub reads every episode from TMDB first, then sends the ticked seasons to Sonarr. A show can be asked for more seasons later with **Request seasons...**. Sonarr needs a TVDB ID; if TMDB has none the picker says so.
- **Who asked**: the profile chosen on the Who-is-watching page is credited ("Wanted by") and counts as demand, even if they have watched the title. A guest can add too, but nobody is credited. Requests stay when a profile is deleted (they just stop counting).
- A failed add (for example a Radarr root folder that does not exist) says why and leaves nothing behind in the catalog.

## The menu

**Dashboard, Search, Resolution Center, Dynamic DNS** and **Settings** are in the top bar. **Media** is a dropdown with Library, Workers and Storage; **System** is a dropdown with Tools & Operations, Logs, Payloads, Jellyfin events and Access. A person only sees the entries their profile may open (see Access below), and a dropdown with nothing in it is hidden.

## Who is watching, PINs and permissions

The first visit from a browser shows **Who is watching?**: pick your profile, or "Just looking" to continue as a guest. The choice is remembered on that device for a year, the top bar shows who you are with a **switch** link, and the profile's Jellyfin user is the one the Jellyfin app links open as. With no profiles nothing is asked.

**Access** (System menu) gives each profile an optional **PIN** (4 to 8 digits) and a list of the pages it may open: Dashboard, Search, Requests (the buttons that request or add titles), Resolution Center, Dynamic DNS, Library, Workers, Storage, Tools & Operations, Logs, Payloads, Settings and Access itself. Guests have their own list. Quick picks (Everything, Viewer, Nothing) fill in the boxes.

- **Nothing changes until you use it.** Every profile and every guest can open everything, as before.
- **A profile with a PIN** asks for it when chosen, then remembers that device for 30 days (changeable on the Access page). Five wrong PINs in a row lock that profile for 30 seconds, doubling each time up to 15 minutes; **Unlock** on the Access page clears it. Changing a PIN, or **Sign out everywhere**, makes every device ask again.
- **A profile without a PIN** can still be chosen by anyone on your network, so its permissions only mean something once the profiles that can do more than a guest have PINs. The Access page lists every profile for which that is not true.
- A page you may not open shows a short explanation (a script gets a 403 answer). Webhooks, workers, images and the sign-in page are never behind a permission; each worker and webhook has its own way in.
- Somebody must always keep the Access permission: removing it from the last person (or deleting that profile) is refused.
- **Lost every PIN?** On the machine that runs SyncHub: `python -m synchub.access reset-pin "Alice"` takes one profile's PIN off and gives it every permission back; `python -m synchub.access reset-all` does that for everyone, guests included; `python -m synchub.access list` shows where things stand. In Docker: `docker exec <container> python -m synchub.access reset-all`.

This is a household lock, not a login system: the PIN is stored hashed, the sign-in is a signed cookie over plain HTTP on your own network, and the app is not meant to face the internet.

## Live updates: webhooks and download progress

Without these, Radarr and Sonarr are read every 6 hours. With them, a grab or import shows up within seconds and a download's percentage moves while you watch.

- **Webhook** (Settings, Live updates): enter SyncHub's address as Radarr/Sonarr reach it (not `localhost`), then press **Set up in Radarr** / **Set up in Sonarr**. SyncHub creates a Webhook connection in that service from the service's own template, switched on for grab, import, upgrade, rename, add and delete. Pressing it again changes nothing. The panel shows when the last event arrived, so you can tell it works. A grab only triggers a look at the download queue; imports, deletes and adds read the library. Health and test events are ignored. If the service says it could not send a test message, it cannot reach SyncHub at that address from where it runs (a Docker network name or the machine's LAN address usually fixes that).
- **Download progress**: every 30 seconds (Settings, "Download progress check"; 0 turns it off) SyncHub reads just the download queue, but only while something is downloading. Otherwise it looks at most every 5 minutes, which also catches downloads started directly in Radarr/Sonarr when no webhook is set up. A download that leaves the queue triggers a library read, so "Downloaded" appears without waiting for the schedule. Open dashboards update live without a reload.
- **Jellyfin**: press **Set up in Jellyfin** (same panel; needs Jellyfin's Webhook plugin installed). SyncHub adds a Generic destination to the plugin that sends **every one of its 24 notification types** (item added and deleted, playback start, progress and stop, sessions, sign-ins, tasks, plugin and user changes and more), for every kind of item and every user, with all properties as JSON. It leaves the plugin's other destinations alone; pressing it again changes nothing. Only *Item added*, *Item deleted* and *Task completed* make SyncHub read the library again. Everything else is kept (below).

## Jellyfin events (what Jellyfin told us, kept)

Every event Jellyfin's Webhook plugin sends is stored (**System, Jellyfin events**) with its type, who, what (title, series, season and episode), device, client, playback position and the whole body as it arrived, so it can be used later. The page shows counts by type, filters by type, user and text, opens any event in full, and exports everything as JSON lines (oldest first). Playback progress arrives every few seconds per player and is stored at most every 30 seconds per player and title. Events are kept for 365 days by default (Settings, Live updates, "Keep Jellyfin events for"; 0 keeps them for ever) and older ones are deleted once a day. The plugin sends plain text unless told otherwise; SyncHub reads the body as JSON either way.

## Opening items in the Jellyfin app (iPhone and iPad)

"Watch on Jellyfin" opens the Jellyfin web page everywhere. On an iPhone or iPad it opens the item in the Jellyfin (Swiftfin) app instead, using `swiftfin://<server id>/<user id>/item/<item id>`, and falls back to the web page after about two seconds if the app does not take over.

- The **server ID** is read from Jellyfin during a Jellyfin sync, so run **Tools, Sync movies/TV** once after upgrading.
- The **user** must be a Jellyfin user that is signed in to the app on that device. Give each profile its Jellyfin user in Settings; the profile chosen on the Who-is-watching page then decides. A guest, or a profile without a Jellyfin user, is asked once which Jellyfin user to use (remembered on that device; the link at the bottom of every page changes it).
- If tapping does nothing useful, check that the user chosen is the one signed in to the app, and that the app is installed.
- On a show's page every episode that is in the library has its own **Watch** button, which opens that episode in Jellyfin the same way (that page is one tap from Play; no documented Jellyfin link starts playback by itself, so this is as direct as it gets). Seasons are all closed when the page opens; open the ones you want.

## Troubleshooting

**Dynamic DNS takes 30 to 45 seconds to show the new address:** most of that is usually not SyncHub. **Dynamic DNS, Run the check** times the path to the services, then asks Dynu's own name servers and the big public resolvers what they say about the name, with the time each still has the old answer in its cache, and says in words where the wait sits. Every update reports how long each step took (public address, Dynu), and after a real address change SyncHub watches how long each server took to show it (shown on the same page). Background and findings: [docs/DDNS_DELAY.md](docs/DDNS_DELAY.md).

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

The code is in `synchub/`: `db.py` + `migrations/` (schema), `clients/` (Jellyfin, Radarr, Sonarr, TMDB), `ingest/` (one module per source, writing the `src_*` tables), `matching.py` and `episodes.py` (linking), `library/` (the Library's decision logic, scans and duplicate search), `storage.py` and `jobs.py` (shares and worker jobs), `access.py` (PINs and permissions), `backup.py` (consistent copies, checks and restore of the database), `jellyfin_events.py` (kept Jellyfin events), `ddns.py` and `ddns_check.py` (dynamic DNS and its delay check), `states.py` (statuses), `catalog.py` (what the pages read), `automation.py` (requests and auto-add), `tasks.py` and `scheduler.py` (jobs), `web/` (pages, templates and API). `config.py`, `logger.py` and `settings_validation.py` at the top level are shared helpers; `static/` holds the CSS, JavaScript and cached posters.
