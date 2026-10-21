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

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Tests use a temporary database and never touch the network (a guard fails any test that tries).

## Layout

`synchub/` is the new app: `db.py` + `migrations/` (schema), `clients/` (Jellyfin, Radarr, Sonarr, TMDB), `ingest/` (one module per source, writing the `src_*` tables), `matching.py` and `episodes.py` (linking), `states.py` (statuses), `catalog.py` (what the pages read), `automation.py` (requests and auto-add), `tasks.py` and `scheduler.py` (jobs), `web/` (pages and API). The top-level `app.py`, `database.py` and friends are the old app and will be removed once the new one has replaced it.
