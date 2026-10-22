# SyncHub

Keeps Letterboxd/Serializd watchlists, Jellyfin, Radarr and Sonarr in one place, shows what you have, what is on its way and what is missing, and can add wanted titles to Radarr/Sonarr.

There are two apps in this repository while the rewrite is being finished:

| | Old app | **New app (v2)** |
|---|---|---|
| Start | `python app.py` (port 5000) | `python -m synchub.web` (port 5001, `SYNCHUB_PORT` to change) |
| Database | `data/letterboxd.db` | `data/synchub.db` (created on first start; nothing is copied from the old one) |
| Design | | [docs/DESIGN_V2.md](docs/DESIGN_V2.md) |

The two do not share data, so they can run side by side while you compare them.

## Running the new app

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m synchub.web
```

Then open http://localhost:5001 and:

1. **Settings**: enter your TMDB API key (needed for titles, posters and episode lists), the Jellyfin/Radarr/Sonarr URLs and API keys (use *Test connection*), and create your profiles. Each Letterboxd, Serializd or Jellyfin identity can belong to only one profile.
2. **Tools & Operations**: press **Run all**. It reads Jellyfin, scrapes the watchlists, resolves IDs, matches everything to a movie or show, fetches every episode from TMDB, then reads Radarr and Sonarr and works out each item's status.
3. **Resolution Center**: approve or dismiss the matches the app was not sure about, or link an item by TMDB ID.

After that the scheduler repeats the work on the intervals set in Settings (0 turns a job off). Radarr/Sonarr webhooks (`/api/webhook/radarr`, `/api/webhook/sonarr`) refresh the status within seconds of a download starting or finishing.

## What the statuses mean

Missing, Queued (monitored in Radarr/Sonarr, nothing yet), Downloading (in the Radarr/Sonarr queue), Downloaded (the file exists but Jellyfin has not picked it up), In Library. **Transferring** and **Encoding** are defined but unused until your own scripts report them. A show is **Partial** while only some of its aired episodes are in the library. Episodes that have not aired show as *Not aired* and are not counted as missing. Specials (Season 0) are listed last and are not counted in a show's totals.

New states need no code or schema change: add a row to `state_def` and have a script write `processing_event` rows.

## Troubleshooting

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

`synchub/` is the new app: `db.py` + `migrations/` (schema), `clients/` (Jellyfin, Radarr, Sonarr, TMDB), `ingest/` (one module per source, writing the `src_*` tables), `matching.py` and `episodes.py` (linking), `states.py` (statuses), `catalog.py` (what the pages read), `automation.py` (requests and auto-add), `tasks.py` and `scheduler.py` (jobs), `web/` (pages and API). The top-level `app.py`, `database.py` and friends are the old app and will be removed once the new one has replaced it.
