# Changelog

## Unreleased

Worker setup, worker logs and a Worker Status tab.

**Workers**
- **`agent/setup-worker.sh`**: one script that sets up (and later updates) a worker PC. It installs the dependencies, asks SyncHub for the worker's settings and shares, pulls the latest code from GitHub, updates `/etc/fstab` (one marked block, with a backup) and a private SMB credentials file, mounts the shares, writes `.env`, builds and starts the worker, marks the shares and checks it. `--mode native` sets up a PC without Docker; `--dry-run` shows what it would do. The Setup dialog on the Workers page has the command as its first tab. New worker API calls for it: `POST /api/worker/v1/setup/<file>`.
- **Logs button** on the Workers page: fetches the tail of a worker's own log (`worker.jsonl` and its rotated files) into SyncHub, with a level filter, search, copy and download. Worker API: the poll answer carries `worker_log`, and the agent answers with `POST /api/worker/v1/log`.
- **Worker Status tab**: per worker, the CPU (model, cores, threads, instruction sets, use, load), memory, graphics cards, system, ffmpeg and ffprobe, hardware decoders, VMAF, and a table of encoders that says which **work** (a test frame was encoded), which are built into ffmpeg but **failed** (usually no GPU or driver) and which are not tested. The agent also tests SVT-AV1 and the AV1 hardware encoders now.
- Agent 0.4.0. An older agent still works; the new tab says its hardware has not been reported, and the Logs button needs 0.4.0.
- Database migration 011 (worker logs).

## 1.1.0

The whole-house TV remote, parental controls, and a published Docker image.

**TV remote** (Roku, Google TV, Android TV)
- Remote page with now playing (a clock that runs smoothly between updates), D-pad, volume, power and sleep, and quick-launch apps; TV apps picks what each remote shows; TVs adds, edits and removes TVs and finds them on the network; TV tools is the testing page.
- Plays Jellyfin titles: wakes the TV, opens the app and plays, going Home first where the TV needs it (screensaver, a Roku already playing something). A show plays its next episode. A Google TV that is on the player screen gets the old title stopped first, and one that ignores the command gets Home, a reopen and one more try.
- **Watch now** (dashboard, movie, show and episode pages) offers This device or any TV, and a TV opens its Remote page with each step shown. The progress box has a Close button when playback worked and stays open, with Try again, when it did not.
- A **Playing now** strip on the dashboard; TV settings are backed up with the database (`.tv.zip`).

**Pages and menu**
- Menu: Dashboard, Remote, Search, Media, System (Run, Monitor, Admin, TV), Settings. Settings in three tabs; People joins profiles, PINs and permissions; What it's doing now is its own page; Workers and Jobs are tabs.
- Dashboard sorts by **Date Added** and shows it; the System menu no longer opens under the search bar; "Match & update" shows correctly; Clear History on Payloads works with CSRF protection on.

**Ratings and parental controls**
- Movie and show pages show the US age rating and the TMDB audience score.
- A most-mature-rating limit for every profile and for guests, applied to the dashboard, pages, search, requests and the Remote. Unrated titles are hidden from anyone with a limit.
- Guests, and any profile without a PIN, get only the Dashboard and Remote and nothing above PG (both changeable). Nothing is restricted until some profile has a PIN, so first-time setup works.

**Deployment**
- GitHub Actions runs the tests and publishes the image to `ghcr.io/thebumgreg/synchub` (`:latest` and `:1.1.0` from main). An Unraid template makes installing and updating a few clicks in the Docker tab. The image includes `adb`, and its key is kept in `/data/tv`. `SYNCHUB_SCHEDULER=0` starts the app without background jobs, for trying a copy of a live database. The version is shown on every page.
- Database migrations 009 (date added) and 010 (ratings).

## 1.0

Docker deployment (app image, database snapshot and restore tools, CSRF protection), workers and the Library, Radarr/Sonarr/Jellyfin live updates, search and add, profiles with PINs and permissions.
