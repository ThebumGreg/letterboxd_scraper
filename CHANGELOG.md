# Changelog

## Unreleased

**Download pipeline (off by default).** A file Radarr or Sonarr imports can now be checked and, where the rules call for it, replaced in place. Switch it on under Library, Settings, *New Radarr/Sonarr downloads*: *Ask me first* lists each import on the Library page with a **Replace it** button, *Replace when safe* converts files whose plan is plain (tracks removed, lossless remux, English audio confirmed) and still asks about the rest.
- **Flow.** The import webhook (Radarr `Download`, Sonarr `Download` and `ImportComplete`) leaves a row; a worker scans the file's folder (the Library's analysis and track rules), SyncHub decides, and a `download` job converts the file on the worker and swaps the result in under the same base name (an `.avi` becomes `.mkv` beside it, the old file is removed). The file's modified date is kept so Jellyfin's "date added" does not move. Then Radarr/Sonarr is asked to rescan the title and Jellyfin to scan.
- **Originals are kept on the worker** in `work/originals/` for *Keep originals (days)* (default 30; 0 drops the original once the replacement is verified), with a `meta.json` saying where each came from. The worker removes them only after that time. The Workers page shows how many are held.
- **Safe by construction.** The worker checks the file on the share is still the one that was analysed before and again just before the swap; the new file is written as `.synchub-partial`, verified, and renamed in. Nothing is replaced if the file changed.
- **Needs setup.** Each share on the Storage page needs the path Radarr/Sonarr see for it. An import no share covers is listed as blocked with that reason.
- Agent: `replace` conversions, originals folder and sweep. Rebuild the worker (`docker compose up -d --build`). Database migration 014. Not done yet: the size policy for 30 GB+ movies and preview clips (next PRs).

Notifications: each person chooses what they hear about and when.

**Notifications**
- **What to tell me about**: an on/off switch per kind of notification on the Notifications page. Administrator alerts show only for the profiles chosen under Settings, Notifications, *Who gets administrator alerts*.
- **Quiet hours** (per person, with a time zone) and **grouping** (every 15 minutes or hour). Ordinary notices wait for the end of quiet hours or the next group time and are then sent as one message. Urgent ones (worker offline, share not mounted) always come through at once.
- **New notices**: a wanted title reaches the library (a movie, or the first episode of a show; a title above the person's rating limit is never named to them), a job fails or needs a decision (or finishes, off by default), a worker goes offline or comes back, a share stops working or recovers, and a Google TV has not accepted SyncHub's ADB key. Conditions notify once when they change, not on every check, and a first look says nothing. More than 50 titles arriving in one pass is treated as an import and is not announced.

Approved Library files are now processed: approving queues a job, a worker converts the file and brings it into the destination share, and Jellyfin is told.

**Library processing**
- **Approving queues a job.** Approving a file (one by one, in bulk or after testing) creates a `library` job: source share and path, destination share and path (the Movies / TV Shows layout) and the approved plan. The next free worker that has both shares ready takes it; if every worker is busy it waits in the queue. Rejecting or undoing takes a waiting job back; a file that changed on disk withdraws its waiting job along with the approval.
- **Migration queue panel** on the Library page: how many files are in the queue, running, waiting for a decision, failed and completed. Files approved before this release show a **Queue them now** button.
- **Completed means done.** A file's migration status moves to *in progress* when a worker takes it and to *completed* only after the converted file has been copied into the destination share and verified. A failed or held job shows as failed or manual review and follows retries, skips and cancels from the Workers page.
- **Agent `convert` operation**: copies the source in, runs ffmpeg for the approved plan (lossless remux to MKV, H.264 re-encode, audio to AAC, tracks removed, English default), checks the result (video present, planned track counts, duration within 0.5 %, clean decode at the end), then copies it out with the usual verification. The source is never changed.
- **Jellyfin** is asked to scan its libraries each time a file arrives (`POST /Library/Refresh`). Best effort: if Jellyfin is unreachable the file is still in place and Jellyfin finds it on its next scheduled scan.

Worker setup, worker logs and a Worker Status tab.

**Workers**
- **`agent/setup-worker.sh`**: one script that sets up (and later updates) a worker PC. It installs the dependencies, asks SyncHub for the worker's settings and shares, pulls the latest code from GitHub, updates `/etc/fstab` (one marked block, with a backup) and a private SMB credentials file, mounts the shares, writes `.env`, builds and starts the worker, marks the shares and checks it. `--mode native` sets up a PC without Docker; `--dry-run` shows what it would do. The Setup dialog on the Workers page has the command as its first tab. New worker API calls for it: `POST /api/worker/v1/setup/<file>`.
- **Logs button** on the Workers page: fetches the tail of a worker's own log (`worker.jsonl` and its rotated files) into SyncHub, with a level filter, search, copy and download. Worker API: the poll answer carries `worker_log`, and the agent answers with `POST /api/worker/v1/log`.
- **Worker Status tab**: per worker, the CPU (model, cores, threads, instruction sets, use, load), memory, graphics cards, system, ffmpeg and ffprobe, hardware decoders, VMAF, and a table of encoders that says which **work** (a test frame was encoded), which are built into ffmpeg but **failed** (usually no GPU or driver) and which are not tested. The agent also tests SVT-AV1 and the AV1 hardware encoders now.
- Agent 0.4.0. An older agent still works; the new tab says its hardware has not been reported, and the Logs button needs 0.4.0.
- Database migration 013 (worker logs).

## 1.1.1

Where each person is in what they watch, with TVs that pick up from there, and the groundwork for notifications (Pushover). The database upgrades itself on start (migrations 011 and 012) and saves a copy first; going back to an older image afterwards is not supported, so restore a backup if you ever need to (docs/DEPLOY.md, section 6).

**Watch progress and resuming**
- Each profile's Jellyfin watch data (what they have watched, and where they stopped) is copied from Jellyfin: a **Watch history** task (with the Jellyfin sync and in *Run all*) and a re-read of one item right after a playback stop event. Database migration 012.
- Movie and show pages show **Your progress**: status, position and percentage, last watched, how many times; for a show, how many episodes watched, what to continue with and what was watched last. Each episode in a season shows its own status.
- **Watch now** says **Resume** (at 42:10, or S2E4 at 12:30) and a TV picks up where the person using the page left off; the menu has **Start from the beginning**. Before, a title always started from the beginning, a show dropped the position of the episode they were in, and the search dialog's *Resume at* showed the Jellyfin administrator's position instead of the viewer's.
- A deep link (Roku) cannot be moved to a position because the official app accepts no remote commands; the progress box says so instead of claiming it. A direct stream cannot start part-way either (a transcoded one can); both say so.

**Notifications: the foundation** (see docs/DESIGN_NOTIFICATIONS.md; nothing is sent about requests or episodes yet)
- A **Notifications** page and permission: each person adds a Pushover user key (checked with Pushover, which also lists their devices), sends a test, switches a method off or removes it, and sees a history of what was sent. Keys are masked, never sent back to a browser and blanked from the log viewer; a profile without a PIN cannot change them once any profile has one. Profiles that have a saved page list need **Notifications** added on People (guests do not get it).
- Settings, Connections, Notifications: the Pushover application token (with a *Test token* that shows the month's remaining allowance), the master switch, the Apprise address (now for administrator alerts) and how long the history is kept.
- A delivery layer that more methods can be added to as one file each: an outbox that sends from a scheduler job (so `SYNCHUB_SCHEDULER=0` silences it), retries temporary failures, never repeats a refusal, switches off a recipient Pushover says is invalid, and holds Pushover when its monthly allowance is used up or its token is rejected.
- Database migration 011 (delivery methods, events, the outbox and history). The old single Apprise notifier is gone.

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
